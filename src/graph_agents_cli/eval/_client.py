# Copyright 2026 graph-agents-cli contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Drive one eval case through ``POST /chat`` (the app's SSE chat API) into a trace.

The SSE transport is ``graph_agents_cli._chat_client.post_chat``; it is imported
lazily so tests can replace it. A trace (one JSON object per case) is derived
from the events: ``message.delta`` text becomes ``response``, ``tool.call`` /
``tool.result`` pairs become ``tool_calls``, ``message.end`` supplies ``usage``,
``latency_ms``, ``thread_id`` and ``run_id``; an ``error`` event or any
exception yields ``status: error``; a stream with no response yields
``status: missing``.

A run that pauses on a gated call (``message.end`` with ``status:
awaiting_approval``) is decided as the case's ``approvals`` instructions say
(the first whose ``match`` names the call), and the resumed run's events are
folded into the same turn. Every gate is recorded under ``approvals`` in the
turn (and the trace); a gate no instruction matches ends the case with
``status: error``: an unattended eval never approves anything on its own.
"""

from __future__ import annotations

import itertools
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import replace
from typing import Any

from graph_agents_cli._approvals import Approval, call_matches, describe_match
from graph_agents_cli._chat_client import (
    DECISION_APPROVE,
    STATUS_AWAITING_APPROVAL,
    ChatHTTPError,
    redact_credentials,
)
from graph_agents_cli.eval.dataset import EvalCase

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_MISSING = "missing"

DEFAULT_TIMEOUT = 300.0

# Gates one turn may pass: a run that keeps pausing is a case error, not a loop.
MAX_APPROVALS_PER_TURN = 20

# What a recorded gate ended as (trace `approvals[].status`).
APPROVAL_APPROVED = "approved"
APPROVAL_REJECTED = "rejected"
APPROVAL_UNEXPECTED = "unexpected"  # no instruction matched: the case errors
# The server refused the decision: HTTP status -> recorded status.
_REFUSED_STATUS = {403: "forbidden", 404: "not_found", 409: "not_pending", 410: "expired"}


def build_case_headers(
    header: tuple[str, ...],
    cookie: tuple[str, ...],
    session_token: str | None,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Headers for the chat calls, via ``graph_agents_cli._remote.build_headers``.

    ``env`` lets the local-server path fall back to the project's ``.env``
    ``API_KEY`` (as ``GRAPH_AGENTS_CLI_API_KEY``) when no bearer was given.
    """
    from graph_agents_cli._remote import build_headers

    return build_headers(header, cookie, session_token, env=env)


def _stream(base_url: str, message: str, **kwargs: Any):
    from graph_agents_cli import _chat_client

    return _chat_client.post_chat(base_url, message, **kwargs)


def _decide(base_url: str, thread_id: str, approval_id: str, decision: str, **kwargs: Any):
    from graph_agents_cli import _chat_client

    return _chat_client.decide_approval(base_url, thread_id, approval_id, decision, **kwargs)


def _opened(events: Iterable[Any]) -> Iterator[Any]:
    """``events`` with the first one already read: an HTTP refusal raises here, not later."""
    iterator = iter(events)
    try:
        first = next(iterator)
    except StopIteration:
        return iter(())
    return itertools.chain([first], iterator)


def find_instruction(
    instructions: Sequence[Mapping[str, Any]], approval: Approval
) -> Mapping[str, Any] | None:
    """The first ``{decision, match}`` instruction whose match names the gated call."""
    return next((i for i in instructions if call_matches(i["match"], approval)), None)


def approval_record(approval: Approval) -> dict[str, Any]:
    """What a trace keeps of one gate: which call, who could decide, and how it ended."""
    return {
        "approval_id": approval.approval_id,
        "api": approval.api,
        "method": approval.method,
        "path": approval.path,
        "operation_id": approval.operation_id,
        "approvers": list(approval.approvers),
        # The instruction that decided it (its match, and approve or reject).
        "match": None,
        "decision": None,
        "status": None,
        "error": None,
    }


def _describe_gate(approval: Approval) -> str:
    call = f"{approval.method or '?'} {approval.path or '?'}"
    extra = [f"api {approval.api}"] if approval.api else []
    if approval.operation_id:
        extra.append(f"operation_id {approval.operation_id}")
    return f"{call} ({', '.join(extra)})" if extra else call


def empty_trace(case_id: str) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "status": STATUS_MISSING,
        "response": None,
        "tool_calls": [],
        "usage": None,
        "latency_ms": None,
        "error": None,
        "thread_id": None,
        "run_id": None,
        "approvals": [],
    }


def _add_usage(total: Any, usage: Any) -> Any:
    """Token usage summed over the runs of one turn (a paused run and its continuations)."""
    if not isinstance(usage, dict):
        return total
    if not isinstance(total, dict):
        return dict(usage)
    summed = dict(total)
    for key, value in usage.items():
        if isinstance(value, int | float) and isinstance(summed.get(key), int | float):
            summed[key] = summed[key] + value
        else:
            summed[key] = value
    return summed


def _consume_turn(
    base_url: str,
    message: str,
    *,
    thread_id: str | None,
    headers: Mapping[str, str],
    metadata: Mapping[str, Any],
    timeout: float,
    approvals: Sequence[Mapping[str, Any]] = (),
    decision_headers: Mapping[str, str] | None = None,
    case_id: str | None = None,
) -> dict[str, Any]:
    """Send one user message and fold the stream into a per-turn record.

    A pause on a gated call is decided per ``approvals`` and the
    continuation is folded into the same record; latency and usage add up
    over the runs. A gate that lists ``requester`` is decided as the eval
    identity (``headers``: it started the run); any other with
    ``decision_headers`` (an approver's credential) when given.
    """
    turn: dict[str, Any] = {
        "status": STATUS_MISSING,
        "response": None,
        "tool_calls": [],
        "usage": None,
        "latency_ms": None,
        "error": None,
        "thread_id": thread_id,
        "run_id": None,
        "approvals": [],
    }
    text_parts: list[str] = []
    calls_by_id: dict[str, dict[str, Any]] = {}
    saw_end = False
    started = time.monotonic()
    try:
        events: Iterable[Any] = _stream(
            base_url,
            message,
            thread_id=thread_id,
            headers=dict(headers),
            timeout=timeout,
            metadata=dict(metadata),
        )
        while True:
            end = _fold(events, turn, text_parts, calls_by_id)
            if end is None:
                break
            saw_end = True
            if end.get("status") != STATUS_AWAITING_APPROVAL:
                break
            saw_end = False
            events = _resolve_gate(
                base_url,
                end,
                turn,
                approvals=approvals,
                headers=headers,
                approver_headers=decision_headers,
                timeout=timeout,
                case_id=case_id,
            )
            if events is None:
                break
    except Exception as exc:  # transport, HTTP or protocol failure: record, do not abort
        turn["status"] = STATUS_ERROR
        # Printed and stored in the traces and results: never a URL's credentials.
        turn["error"] = redact_credentials(f"{type(exc).__name__}: {exc}")

    elapsed_ms = round((time.monotonic() - started) * 1000)
    if turn["latency_ms"] is None:
        turn["latency_ms"] = elapsed_ms
    if text_parts:
        turn["response"] = "".join(text_parts)
    if turn["status"] == STATUS_ERROR:
        return turn
    if not saw_end:
        if turn["response"] is None and not turn["tool_calls"]:
            turn["status"] = STATUS_MISSING
            turn["error"] = "stream produced no events"
        else:
            turn["status"] = STATUS_ERROR
            turn["error"] = "stream closed before message.end"
        return turn
    if turn["response"] is None:
        # A completed run with no text: keep it graded (an empty reply is a
        # reply) rather than counting it as missing.
        turn["response"] = ""
    turn["status"] = STATUS_OK
    return turn


def _resolve_gate(
    base_url: str,
    end: Mapping[str, Any],
    turn: dict[str, Any],
    *,
    approvals: Sequence[Mapping[str, Any]],
    headers: Mapping[str, str],
    approver_headers: Mapping[str, str] | None = None,
    timeout: float,
    case_id: str | None,
) -> Iterator[Any] | None:
    """Decide the gate a run paused on; the resumed run's events, or None (turn errored).

    As the eval identity (``headers``) when the gate lists ``requester``;
    otherwise as the approver (``approver_headers``) when one is set.
    """
    approval = Approval.from_payload(end.get("approval"), thread_id=turn["thread_id"])
    if approval is not None and turn["thread_id"]:
        # The decision is about this case's own thread, whatever the payload names.
        approval = replace(approval, thread_id=turn["thread_id"])
    if approval is None or not approval.thread_id:
        turn["status"] = STATUS_ERROR
        turn["error"] = "the run paused for an approval without an approval id or thread id"
        return None
    record = approval_record(approval)
    turn["approvals"].append(record)
    if len(turn["approvals"]) > MAX_APPROVALS_PER_TURN:
        record["status"] = APPROVAL_UNEXPECTED
        turn["status"] = STATUS_ERROR
        turn["error"] = f"the run paused on more than {MAX_APPROVALS_PER_TURN} gated calls"
        return None
    instruction = find_instruction(approvals, approval)
    if instruction is None:
        record["status"] = APPROVAL_UNEXPECTED
        turn["status"] = STATUS_ERROR
        turn["error"] = (
            f"unexpected approval gate on {_describe_gate(approval)}: the case has no "
            "approvals instruction matching it; add one, e.g. "
            '{"decision": "reject", "match": {...}}, saying what a human would decide'
        )
        return None
    decision = str(instruction["decision"])
    record["decision"] = decision
    record["match"] = describe_match(instruction["match"])
    as_approver = approver_headers is not None and not approval.requester_may_decide
    if as_approver:
        headers = approver_headers
    try:
        events = _opened(
            _decide(
                base_url,
                approval.thread_id,
                approval.approval_id,
                decision,
                comment=f"eval case {case_id}" if case_id else "eval",
                headers=dict(headers),
                timeout=timeout,
            )
        )
    except ChatHTTPError as exc:
        record["status"] = _REFUSED_STATUS.get(exc.status_code, "error")
        record["error"] = f"HTTP {exc.status_code}"
        turn["status"] = STATUS_ERROR
        turn["error"] = redact_credentials(
            f"the {decision} decision on {_describe_gate(approval)} was refused "
            f"(HTTP {exc.status_code}: {exc.body[:200]})"
        )
        if exc.status_code == 403:
            turn["error"] += (
                "; the principal of GRAPH_AGENTS_CLI_APPROVER_API_KEY may not decide this gate "
                "(it needs a role the gate lists, and must not be the eval identity)"
                if as_approver
                else "; the eval identity is not an approver of this gate (set "
                "GRAPH_AGENTS_CLI_APPROVER_API_KEY to the credential of a principal holding a "
                "role it lists)"
            )
        return None
    record["status"] = APPROVAL_APPROVED if decision == DECISION_APPROVE else APPROVAL_REJECTED
    return events


def _fold(
    events: Iterable[Any],
    turn: dict[str, Any],
    text_parts: list[str],
    calls_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Fold one run's events into ``turn``; its ``message.end`` data, or None (error or no end)."""
    for event in events:
        name = getattr(event, "event", None)
        data = getattr(event, "data", None)
        if name is None and isinstance(event, tuple):
            name, data = event[0], event[1]
        data = data if isinstance(data, dict) else {}
        if name == "message.start":
            turn["thread_id"] = data.get("thread_id") or turn["thread_id"]
            turn["run_id"] = data.get("run_id") or turn["run_id"]
        elif name == "message.delta":
            text_parts.append(str(data.get("text", "")))
        elif name == "tool.call":
            call = {
                "id": data.get("id"),
                "name": data.get("name"),
                "args": data.get("args"),
                "result": None,
                "is_error": False,
            }
            turn["tool_calls"].append(call)
            if call["id"] is not None:
                calls_by_id[str(call["id"])] = call
        elif name == "tool.result":
            # A continuation's result belongs to the call its paused run made.
            call = calls_by_id.get(str(data.get("id")))
            if call is None:
                call = {
                    "id": data.get("id"),
                    "name": data.get("name"),
                    "args": None,
                    "result": None,
                    "is_error": False,
                }
                turn["tool_calls"].append(call)
            call["result"] = data.get("result")
            call["is_error"] = bool(data.get("is_error", False))
        elif name == "message.end":
            turn["thread_id"] = data.get("thread_id") or turn["thread_id"]
            turn["run_id"] = data.get("run_id") or turn["run_id"]
            turn["usage"] = _add_usage(turn["usage"], data.get("usage"))
            latency = data.get("latency_ms")
            if isinstance(latency, int | float):
                previous = turn["latency_ms"]
                turn["latency_ms"] = latency + (
                    previous if isinstance(previous, int | float) else 0
                )
            end_status = data.get("status", "ok")
            if end_status not in ("ok", STATUS_AWAITING_APPROVAL):
                turn["status"] = STATUS_ERROR
                turn["error"] = f"message.end status {end_status!r}"
                return None
            return data
        elif name == "error":
            turn["status"] = STATUS_ERROR
            code = data.get("code")
            msg = data.get("message") or "unknown error"
            turn["error"] = f"{msg} ({code})" if code else str(msg)
            return None
    return None


def run_case(
    base_url: str,
    case: EvalCase,
    *,
    headers: Mapping[str, str],
    metadata: Mapping[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    decision_headers: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Run every user message of ``case`` on one thread and return its trace.

    Multi-turn cases send each user message in order on the thread the first
    ``message.start`` reports; dataset ``assistant``/``system`` messages are
    context for judges only and are not sent. The trace carries the final
    turn's response, tool calls, usage and latency, plus ``turns`` with every
    turn when there was more than one. Gated calls are decided per
    ``case.approvals`` (see ``_consume_turn``) and recorded under ``approvals``.
    """
    trace = empty_trace(case.id)
    turns: list[dict[str, Any]] = []
    thread_id: str | None = None
    base_metadata = {"source": "eval", "case_id": case.id, **(metadata or {})}
    user_messages = case.user_messages()
    for index, message in enumerate(user_messages):
        turn = _consume_turn(
            base_url,
            message,
            thread_id=thread_id,
            headers=headers,
            metadata={**base_metadata, "turn": index},
            timeout=timeout,
            approvals=case.approvals,
            decision_headers=decision_headers,
            case_id=case.id,
        )
        turns.append(turn)
        thread_id = turn.get("thread_id") or thread_id
        if turn["status"] != STATUS_OK:
            break
    last = turns[-1]
    trace.update(
        {
            "status": last["status"],
            "response": last["response"],
            "tool_calls": last["tool_calls"],
            "usage": last["usage"],
            "latency_ms": last["latency_ms"],
            "error": last["error"],
            "thread_id": last["thread_id"] or thread_id,
            "run_id": last["run_id"],
            "approvals": last.get("approvals", []),
        }
    )
    if len(turns) > 1:
        trace["turns"] = turns
    return trace
