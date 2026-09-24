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

"""Client side of the human approval gate: the paused call, shown safely, and how to decide it.

An API's ``approval`` block in ``api-policy.yaml`` makes the agent pause
before sending a gated call. The chat API then ends the run's stream with
``message.end`` whose ``status`` is ``awaiting_approval`` and whose
``approval`` describes the call (``approval_id``, ``api``, ``method``,
``path``, ``query``, ``body``, ``operation_id``, ``reason``, ``approvers``,
``expires_at``). ``GET /threads/{thread_id}/approvals`` lists a thread's
approvals and ``POST /threads/{thread_id}/approvals/{approval_id}`` decides
one. ``run``, ``approvals`` and ``eval generate`` share this module.

Everything in an approval except its id came, one way or another, from the
model (the path's ids, the body, the stated reason), and the model may have
read text an attacker wrote. A human decides from what is printed here, so
it is printed in full (never cut) and with every control, format and
separator character escaped: no text can move the cursor, recolour the
screen, reorder characters or fake a line of this output.
"""

from __future__ import annotations

import json
import re
import shlex
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from graph_agents_cli._api_policy import (
    HTTP_METHODS,
    REQUESTER_APPROVER,
    path_matches,
    path_template_problem,
)
from graph_agents_cli._chat_client import DECISION_APPROVE, DECISION_REJECT

# Approval statuses the server reports.
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"

# What an eval case's `approvals` instructions and `expect.approvals` match a gate on.
MATCH_KEYS = ("api", "operation_id", "method", "path")

# The refusals of `POST /threads/{thread_id}/approvals/{approval_id}`.
DECISION_REFUSALS: dict[int, str] = {
    403: (
        "you are not an allowed approver of this call. requester is the principal who "
        "started the run; role:<name> is any other principal holding that role (a requester "
        "decides their own call only when requester is listed)"
    ),
    404: "no such approval on that thread (a wrong id, or the thread was deleted)",
    409: "it is not pending any more: it was already decided (an approval is single-use)",
    410: "it expired, which rejected the call: nothing was sent; ask the agent again",
}

_ESCAPED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"})


def safe_text(value: Any) -> str:
    """``value`` as one line of terminal-safe text.

    Every control (C0, DEL, C1), format (bidi overrides, zero-width marks),
    line/paragraph separator, surrogate, private-use or unassigned character is
    written as its ``\\x``/``\\u``/``\\U`` escape, so what the terminal shows
    is exactly what the value holds.
    """
    text = value if isinstance(value, str) else str(value)
    out: list[str] = []
    for char in text:
        if unicodedata.category(char) in _ESCAPED_CATEGORIES:
            code = ord(char)
            if code <= 0xFF:
                out.append(f"\\x{code:02x}")
            elif code <= 0xFFFF:
                out.append(f"\\u{code:04x}")
            else:
                out.append(f"\\U{code:08x}")
        else:
            out.append(char)
    return "".join(out)


# What streamed agent text may not send to the terminal: C0 controls but tab and
# newline (ESC starts every cursor, colour and conceal sequence), DEL, C1 controls
# (0x9b is a one-byte CSI) and the bidi embedding, override and isolate marks.
_TERMINAL_CONTROLS = re.compile("[\\x00-\\x08\\x0b-\\x1f\\x7f-\\x9f\\u202a-\\u202e\\u2066-\\u2069]")


def terminal_text(text: str) -> str:
    """Streamed agent text with the characters that steer a terminal escaped.

    Line breaks and tabs stay (a reply has paragraphs), so the text reads as
    written; nothing in it can hide, recolour or overwrite the lines around
    it, such as the approval shown before "Approve? [y/N]".
    """
    return _TERMINAL_CONTROLS.sub(lambda m: safe_text(m.group(0)), text)


def json_lines(value: Any) -> list[str]:
    """``value`` as indented JSON, one terminal-safe line per list item.

    ``json.dumps`` escapes the newlines inside strings, so the line breaks left
    are the ones indentation adds; each line is then passed through ``safe_text``.
    """
    try:
        text = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = repr(value)
    return [safe_text(line) for line in text.split("\n")]


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True)
class Approval:
    """One approval: the paused call and who may decide it."""

    approval_id: str
    thread_id: str | None = None
    api: str | None = None
    method: str | None = None
    path: str | None = None
    query: Any = None
    body: Any = None
    operation_id: str | None = None
    reason: str | None = None
    approvers: tuple[str, ...] = ()
    expires_at: str | None = None
    # pending / approved / rejected / expired (listings); None in a paused run's payload.
    status: str | None = None
    decided_at: str | None = None
    comment: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_payload(cls, payload: Any, *, thread_id: str | None = None) -> Approval | None:
        """An approval from the server's JSON (a paused run's or a listing row); None without an id."""
        if not isinstance(payload, Mapping):
            return None
        approval_id = _opt_str(payload.get("approval_id")) or _opt_str(payload.get("id"))
        if approval_id is None:
            return None
        approvers = payload.get("approvers")
        method = _opt_str(payload.get("method"))
        return cls(
            approval_id=approval_id,
            thread_id=_opt_str(payload.get("thread_id")) or thread_id,
            api=_opt_str(payload.get("api")),
            method=method.upper() if method else None,
            path=_opt_str(payload.get("path")),
            query=payload.get("query"),
            body=payload.get("body"),
            operation_id=_opt_str(payload.get("operation_id")),
            reason=_opt_str(payload.get("reason")),
            approvers=tuple(str(a) for a in approvers) if isinstance(approvers, list) else (),
            expires_at=_opt_str(payload.get("expires_at")),
            status=_opt_str(payload.get("status")),
            decided_at=_opt_str(payload.get("decided_at")),
            comment=_opt_str(payload.get("comment")),
            raw=dict(payload),
        )

    @property
    def pending(self) -> bool:
        """True unless a listing says it was decided or expired."""
        return self.status in (None, STATUS_PENDING)

    @property
    def requester_may_decide(self) -> bool:
        """Whether the principal who started the run is one of the approvers."""
        return REQUESTER_APPROVER in self.approvers

    def call_line(self) -> str:
        """``POST /orders/ORD-1/cancel (api orders, operation cancelOrder)``, terminal-safe."""
        call = f"{safe_text(self.method or '?')} {safe_text(self.path or '?')}"
        details = []
        if self.api:
            details.append(f"api {safe_text(self.api)}")
        if self.operation_id:
            details.append(f"operation {safe_text(self.operation_id)}")
        return f"{call} ({', '.join(details)})" if details else call

    def lines(self) -> list[str]:
        """The approval in full, as the human deciding it must see it."""
        lines = [f"  call:        {self.call_line()}"]
        if self.query not in (None, {}, [], ""):
            query = json_lines(self.query)
            lines.append(f"  query:       {query[0]}")
            lines.extend(f"               {line}" for line in query[1:])
        if self.body is not None:
            body = json_lines(self.body)
            lines.append(f"  body:        {body[0]}")
            lines.extend(f"               {line}" for line in body[1:])
        else:
            lines.append("  body:        (none)")
        if self.reason:
            lines.append(f"  reason:      {safe_text(self.reason)}")
        approvers = ", ".join(safe_text(a) for a in self.approvers) or "(not stated)"
        lines.append(f"  approvers:   {approvers}")
        if self.expires_at:
            lines.append(f"  expires at:  {safe_text(self.expires_at)}")
        if self.status:
            status = safe_text(self.status)
            if self.decided_at:
                status += f" at {safe_text(self.decided_at)}"
            lines.append(f"  status:      {status}")
        if self.comment:
            lines.append(f"  comment:     {safe_text(self.comment)}")
        lines.append(f"  approval id: {safe_text(self.approval_id)}")
        if self.thread_id:
            lines.append(f"  thread:      {safe_text(self.thread_id)}")
        return lines


def decide_commands(
    approval: Approval, thread_id: str, flags: str = "", *, verbs: Sequence[str] = ("approve",)
) -> list[str]:
    """The ``graph-agents-cli approvals approve|reject ...`` commands that decide ``approval``.

    Ids come from the server: they are shell-quoted so a pasted command runs
    exactly one ``graph-agents-cli`` invocation. ``flags`` (``--url``, headers
    with credentials redacted) starts with a space when not empty.
    """
    ids = f"{shlex.quote(approval.approval_id)} --thread-id {shlex.quote(thread_id)}"
    commands = []
    for verb in verbs:
        commands.append(f"graph-agents-cli approvals {verb} {ids}{flags}")
    return commands


def awaiting_lines(approval: Approval, thread_id: str | None, flags: str = "") -> list[str]:
    """What a paused run prints: who decides, and the exact commands to decide it."""
    who = ", ".join(safe_text(a) for a in approval.approvers) or "an allowed approver"
    lines = [
        f"Awaiting approval by {who}: the call was not sent; the run is paused until it is decided."
    ]
    if thread_id:
        approve, reject = (
            decide_commands(approval, thread_id, flags, verbs=(verb,))[0]
            for verb in (DECISION_APPROVE, DECISION_REJECT)
        )
        lines.append(f"  Approve: {approve}")
        lines.append(f'  Reject:  {reject} --comment "<why>"')
        if not approval.requester_may_decide:
            lines.append(
                "  Only another principal can decide it (requester is not an approver): send "
                "them these commands; they run them with their own credentials."
            )
    if approval.expires_at:
        lines.append(f"  It expires (rejected, nothing sent) at {safe_text(approval.expires_at)}.")
    return lines


# ---------------------------------------------------------------------------
# Eval: matching a gate
# ---------------------------------------------------------------------------


def match_problem(match: Any) -> str | None:
    """Why an eval ``match`` object cannot name a gated call, or None.

    ``{"operation_id": ...}`` or ``{"method": ..., "path": ...}`` (a path
    template: ``{name}`` matches one segment), or all three; ``api`` narrows
    it to one API.
    """
    if not isinstance(match, Mapping) or not match:
        return 'must be an object like {"operation_id": "..."} or {"method": "...", "path": "..."}'
    unknown = sorted(set(match) - set(MATCH_KEYS), key=str)
    if unknown:
        return f"unknown key(s) {', '.join(map(str, unknown))} (known: {', '.join(MATCH_KEYS)})"
    for key in MATCH_KEYS:
        if key in match and not (isinstance(match[key], str) and match[key].strip()):
            return f"{key} must be a non-empty string"
    has_endpoint = "method" in match and "path" in match
    if "operation_id" not in match and not has_endpoint:
        return "needs operation_id, or method and path"
    if ("method" in match) != ("path" in match):
        return "method and path go together"
    if "method" in match and match["method"].upper() not in HTTP_METHODS:
        return f"unknown HTTP method {match['method']!r} (allowed: {', '.join(HTTP_METHODS)})"
    if "path" in match:
        problem = path_template_problem(match["path"])
        if problem:
            return f"path {problem}"
    return None


def call_matches(match: Mapping[str, Any], call: Mapping[str, Any] | Approval) -> bool:
    """Whether a valid ``match`` names the call: every key it gives must agree."""
    if isinstance(call, Approval):
        call = {
            "api": call.api,
            "operation_id": call.operation_id,
            "method": call.method,
            "path": call.path,
        }
    if "api" in match and call.get("api") != match["api"]:
        return False
    if "operation_id" in match and call.get("operation_id") != match["operation_id"]:
        return False
    if "method" in match and str(call.get("method") or "").upper() != match["method"].upper():
        return False
    if "path" in match:
        path = call.get("path")
        if not isinstance(path, str) or not path_matches(match["path"], path):
            return False
    return True


def describe_match(match: Mapping[str, Any]) -> str:
    """``cancelOrder`` / ``POST /orders/{id}/cancel`` / ``orders: cancelOrder POST /...``."""
    parts = []
    if match.get("operation_id"):
        parts.append(str(match["operation_id"]))
    if match.get("method"):
        parts.append(f"{str(match['method']).upper()} {match.get('path')}")
    text = " ".join(parts)
    return f"{match['api']}: {text}" if match.get("api") else text
