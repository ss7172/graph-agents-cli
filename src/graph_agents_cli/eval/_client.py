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
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from graph_agents_cli.eval.dataset import EvalCase

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_MISSING = "missing"

DEFAULT_TIMEOUT = 300.0


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
    }


def _consume_turn(
    base_url: str,
    message: str,
    *,
    thread_id: str | None,
    headers: Mapping[str, str],
    metadata: Mapping[str, Any],
    timeout: float,
) -> dict[str, Any]:
    """Send one user message and fold the stream into a per-turn record."""
    turn: dict[str, Any] = {
        "status": STATUS_MISSING,
        "response": None,
        "tool_calls": [],
        "usage": None,
        "latency_ms": None,
        "error": None,
        "thread_id": thread_id,
        "run_id": None,
    }
    text_parts: list[str] = []
    calls_by_id: dict[str, dict[str, Any]] = {}
    saw_end = False
    started = time.monotonic()
    try:
        for event in _stream(
            base_url,
            message,
            thread_id=thread_id,
            headers=dict(headers),
            timeout=timeout,
            metadata=dict(metadata),
        ):
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
                saw_end = True
                turn["thread_id"] = data.get("thread_id") or turn["thread_id"]
                turn["run_id"] = data.get("run_id") or turn["run_id"]
                turn["usage"] = data.get("usage")
                turn["latency_ms"] = data.get("latency_ms")
                end_status = data.get("status", "ok")
                if end_status != "ok":
                    turn["status"] = STATUS_ERROR
                    turn["error"] = f"message.end status {end_status!r}"
                break
            elif name == "error":
                turn["status"] = STATUS_ERROR
                code = data.get("code")
                msg = data.get("message") or "unknown error"
                turn["error"] = f"{msg} ({code})" if code else str(msg)
                break
    except Exception as exc:  # transport, HTTP or protocol failure: record, do not abort
        turn["status"] = STATUS_ERROR
        turn["error"] = f"{type(exc).__name__}: {exc}"

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


def run_case(
    base_url: str,
    case: EvalCase,
    *,
    headers: Mapping[str, str],
    metadata: Mapping[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Run every user message of ``case`` on one thread and return its trace.

    Multi-turn cases send each user message in order on the thread the first
    ``message.start`` reports; dataset ``assistant``/``system`` messages are
    context for judges only and are not sent. The trace carries the final
    turn's response, tool calls, usage and latency, plus ``turns`` with every
    turn when there was more than one.
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
        }
    )
    if len(turns) > 1:
        trace["turns"] = turns
    return trace
