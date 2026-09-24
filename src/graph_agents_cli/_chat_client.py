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

"""Synchronous client for the chat API (``POST /chat``, SSE) of a scaffolded agent.

The server side is ``POST /chat`` streaming server-sent events, ``GET /health``,
``GET /threads``, ``GET /threads/{thread_id}/messages`` and the approval routes
(``GET /threads/{thread_id}/approvals``, ``POST
/threads/{thread_id}/approvals/{approval_id}``). This module is the only
client-side implementation of that surface; ``run``, ``approvals`` and
``eval generate`` build on it.

A run that reaches a call its API's policy gates pauses: its stream ends with
``message.end`` whose ``status`` is ``awaiting_approval`` and whose
``approval`` names the call. Deciding it (``decide_approval``) resumes the run
and streams the continuation with the same events as ``POST /chat``.

Only ``httpx`` is imported here: no model SDK, no framework.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Mapping
from typing import Any, NamedTuple
from urllib.parse import quote

import httpx

# Event names the scaffolded app streams from POST /chat.
EVENT_MESSAGE_START = "message.start"
EVENT_MESSAGE_DELTA = "message.delta"
EVENT_TOOL_CALL = "tool.call"
EVENT_TOOL_RESULT = "tool.result"
EVENT_MESSAGE_END = "message.end"
EVENT_ERROR = "error"

KNOWN_EVENTS = frozenset(
    {
        EVENT_MESSAGE_START,
        EVENT_MESSAGE_DELTA,
        EVENT_TOOL_CALL,
        EVENT_TOOL_RESULT,
        EVENT_MESSAGE_END,
        EVENT_ERROR,
    }
)

# `message.end` status of a run paused on a gated call (its payload has `approval`).
STATUS_AWAITING_APPROVAL = "awaiting_approval"
# The decisions `POST /threads/{thread_id}/approvals/{approval_id}` takes.
DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"
DECISIONS = (DECISION_APPROVE, DECISION_REJECT)
# The 409 code of `POST /chat` on a thread whose run waits for an approval.
APPROVAL_PENDING = "approval_pending"

# Scalar timeout kept for callers that pass one number (every phase applies it).
DEFAULT_TIMEOUT = 120.0
# The streaming call: connecting must be quick, but a healthy agent may send no
# event for minutes while a tool or a non-streaming model phase runs. The read
# timeout bounds that gap without treating it as "unreachable"; ``post_chat``
# reads this at call time so a caller (or a test) may replace it.
STREAM_READ_TIMEOUT = 600.0
STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=STREAM_READ_TIMEOUT, write=10.0, pool=10.0)


class SseEvent(NamedTuple):
    """One parsed server-sent event.

    ``data`` is the JSON-decoded payload when the ``data:`` lines form valid
    JSON, otherwise the raw joined string. ``raw`` is always the joined string.
    """

    event: str
    data: Any
    raw: str = ""
    id: str | None = None


# `scheme://user:password@`: the userinfo of any URL in a text.
_URL_USERINFO = re.compile(r"(\b[A-Za-z][A-Za-z0-9+.-]*://)[^/\s@]+@")


def redact_credentials(text: str) -> str:
    """``text`` with the ``user:password@`` of every URL in it replaced by ``***@``."""
    return _URL_USERINFO.sub(r"\1***@", text or "")


class ChatClientError(Exception):
    """Base class for chat client failures."""


class ChatHTTPError(ChatClientError):
    """The server answered with an HTTP error status before streaming.

    ``url`` and the message never carry the URL's credentials (``***@``).
    """

    def __init__(self, status_code: int, body: str, url: str = "") -> None:
        self.status_code = status_code
        self.body = body
        self.url = redact_credentials(url)
        super().__init__(f"HTTP {status_code} from {self.url or 'server'}: {body}")


def _normalise_base(base_url: str) -> str:
    return base_url.rstrip("/")


def _decode_data(raw: str) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw


def iter_sse(lines: Iterable[str]) -> Iterator[SseEvent]:
    """Parse a stream of text lines into :class:`SseEvent` objects.

    Follows the WHATWG event-stream grammar: ``:`` lines are comments
    (keep-alives) and are dropped, ``data:`` lines accumulate joined by
    ``\\n``, ``event:`` names the event (default ``message``), ``id:`` is kept,
    ``retry:`` and unknown fields are ignored, and a blank line dispatches the
    pending event. A pending event at end of stream is dispatched as well, so a
    server that closes without a trailing blank line still delivers its last
    event. Events with no data are dropped, as the spec requires.
    """
    event_name = ""
    data_lines: list[str] = []
    event_id: str | None = None

    def flush() -> SseEvent | None:
        nonlocal event_name, data_lines, event_id
        if not data_lines:
            event_name = ""
            event_id = None
            return None
        raw = "\n".join(data_lines)
        ev = SseEvent(event_name or "message", _decode_data(raw), raw, event_id)
        event_name = ""
        data_lines = []
        event_id = None
        return ev

    for line in lines:
        # Accept both bare and CRLF-terminated lines from any source.
        line = line.rstrip("\r\n")
        if line == "":
            ev = flush()
            if ev is not None:
                yield ev
            continue
        if line.startswith(":"):
            continue  # comment / keep-alive
        field, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if field == "data":
            data_lines.append(value)
        elif field == "event":
            event_name = value
        elif field == "id":
            event_id = value
        # retry: and unknown fields are ignored.

    ev = flush()
    if ev is not None:
        yield ev


def _iter_stream_lines(chunks: Iterable[str]) -> Iterator[str]:
    """Split decoded text chunks into lines on LF, CR or CRLF only.

    ``httpx.Response.iter_lines`` uses ``str.splitlines`` semantics and also
    breaks on U+0085, U+2028, U+2029, VT, FF, FS, GS and RS, which are legal
    inside a ``data:`` payload (the scaffolded server encodes with
    ``ensure_ascii=False``); the event-stream grammar recognises only CR, LF
    and CRLF as line terminators. A trailing CR is held back until the next
    chunk shows whether an LF follows, so a CRLF split across chunks is one
    line break, and a partial last line is flushed at end of stream.
    """
    buf = ""
    for chunk in chunks:
        buf += chunk
        while True:
            i_n = buf.find("\n")
            i_r = buf.find("\r")
            if i_n == -1 and i_r == -1:
                break
            if i_r != -1 and (i_n == -1 or i_r < i_n):
                if i_r == len(buf) - 1:
                    break  # trailing CR: wait to see whether an LF follows
                end = i_r + 2 if buf[i_r + 1] == "\n" else i_r + 1
                yield buf[:i_r]
                buf = buf[end:]
            else:
                yield buf[:i_n]
                buf = buf[i_n + 1 :]
    if buf:
        yield buf.rstrip("\r")


def _stream_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    merged: dict[str, str] = {}
    if headers:
        merged.update(headers)
    # Do not override an explicit Accept from the caller.
    if not any(k.lower() == "accept" for k in merged):
        merged["Accept"] = "text/event-stream"
    return merged


def post_chat(
    base_url: str,
    message: str,
    *,
    thread_id: str | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float | httpx.Timeout | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> Iterator[SseEvent]:
    """POST ``/chat`` and yield the streamed events as they arrive.

    ``timeout`` defaults to :data:`STREAM_TIMEOUT` (looked up at call time): a
    short connect phase and a long per-read gap. A scalar applies to every
    phase. Raises :class:`ChatHTTPError` when the response status is not 2xx
    (the body is read fully so the caller can show it), and lets ``httpx``
    transport errors propagate so callers can distinguish "unreachable"
    (``ConnectError``) from a stalled stream (``ReadTimeout``) and from
    "refused".
    """
    base = _normalise_base(base_url)
    body: dict[str, Any] = {"message": message, "metadata": dict(metadata or {})}
    if thread_id:
        body["thread_id"] = thread_id
    yield from _post_stream(f"{base}/chat", body, headers=headers, timeout=timeout)


def _post_stream(
    url: str,
    body: Mapping[str, Any],
    *,
    headers: Mapping[str, str] | None,
    timeout: float | httpx.Timeout | None,
) -> Iterator[SseEvent]:
    """POST ``body`` as JSON and yield the SSE events of the answer (``post_chat``'s rules)."""
    if timeout is None:
        timeout = STREAM_TIMEOUT
    with (
        httpx.Client(timeout=timeout) as client,
        client.stream("POST", url, json=dict(body), headers=_stream_headers(headers)) as resp,
    ):
        if resp.status_code >= 400:
            resp.read()
            raise ChatHTTPError(resp.status_code, resp.text, url)
        yield from iter_sse(_iter_stream_lines(resp.iter_text()))


def path_segment(value: str, what: str) -> str:
    """``value`` as one URL path segment: percent-encoded, so no id can reach another route.

    An approval id or a thread id comes from a server payload or the command
    line; ``../chat`` or ``a/b`` must stay one segment of the approval route.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must be a non-empty string")
    return quote(value, safe="")


def approval_url(base_url: str, thread_id: str, approval_id: str | None = None) -> str:
    """``<base>/threads/<thread_id>/approvals[/<approval_id>]`` with each id one segment."""
    url = f"{_normalise_base(base_url)}/threads/{path_segment(thread_id, 'thread id')}/approvals"
    if approval_id is not None:
        url += f"/{path_segment(approval_id, 'approval id')}"
    return url


def decide_approval(
    base_url: str,
    thread_id: str,
    approval_id: str,
    decision: str,
    *,
    comment: str | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float | httpx.Timeout | None = None,
) -> Iterator[SseEvent]:
    """Approve or reject a pending approval; yield the events of the resumed run.

    ``POST /threads/{thread_id}/approvals/{approval_id}`` with
    ``{"decision": "approve"|"reject", "comment": ...}`` and nothing else: the
    server binds the decision to the call it recorded, so the client never
    sends (or could change) the call itself. The answer is an SSE stream of the
    continuation, with the same events as ``POST /chat``. A refusal is a
    :class:`ChatHTTPError`: 403 (not an allowed approver), 404 (no such
    approval or thread), 409 (already decided), 410 (expired, which rejected
    the call).
    """
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {', '.join(DECISIONS)}")
    body: dict[str, Any] = {"decision": decision}
    if comment:
        body["comment"] = comment
    url = approval_url(base_url, thread_id, approval_id)
    yield from _post_stream(url, body, headers=headers, timeout=timeout)


def _json_list(resp: httpx.Response, url: str, key: str) -> list[dict[str, Any]]:
    if resp.status_code >= 400:
        raise ChatHTTPError(resp.status_code, resp.text, url)
    data = resp.json()
    if isinstance(data, dict):
        data = data.get(key, [])
    if not isinstance(data, list):
        raise ChatClientError(f"unexpected answer from {redact_credentials(url)}: not a list")
    return [item for item in data if isinstance(item, dict)]


def list_approvals(
    base_url: str,
    thread_id: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """GET ``/threads/{thread_id}/approvals``: the thread's approvals, pending or decided.

    The server may wrap the list as ``{"approvals": [...]}``; both shapes are accepted.
    """
    url = approval_url(base_url, thread_id)
    resp = httpx.get(url, headers=dict(headers or {}), timeout=timeout)
    return _json_list(resp, url, "approvals")


def list_threads(
    base_url: str,
    *,
    headers: Mapping[str, str] | None = None,
    limit: int = 100,
    offset: int = 0,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """GET ``/threads``: the caller's own threads (``{thread_id, ...}`` rows), one page."""
    url = f"{_normalise_base(base_url)}/threads"
    resp = httpx.get(
        url,
        params={"limit": limit, "offset": offset},
        headers=dict(headers or {}),
        timeout=timeout,
    )
    return _json_list(resp, url, "threads")


def get_health(
    base_url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """GET ``/health`` and return its JSON body.

    Raises :class:`ChatHTTPError` on a non-2xx status and lets transport
    errors propagate.
    """
    url = f"{_normalise_base(base_url)}/health"
    resp = httpx.get(url, headers=dict(headers or {}), timeout=timeout)
    if resp.status_code >= 400:
        raise ChatHTTPError(resp.status_code, resp.text, url)
    data = resp.json()
    return data if isinstance(data, dict) else {"status": data}


def get_thread_messages(
    base_url: str,
    thread_id: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """GET ``/threads/{thread_id}/messages`` and return the ordered messages.

    The server may wrap the list as ``{"messages": [...]}``; both shapes are
    accepted.
    """
    url = f"{_normalise_base(base_url)}/threads/{thread_id}/messages"
    resp = httpx.get(url, headers=dict(headers or {}), timeout=timeout)
    if resp.status_code >= 400:
        raise ChatHTTPError(resp.status_code, resp.text, url)
    data = resp.json()
    if isinstance(data, dict):
        data = data.get("messages", [])
    return list(data)
