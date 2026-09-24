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

"""An in-process fake of the scaffolded chat API for the run tests.

Served by ``http.server`` on a free loopback port in a daemon thread: no
network beyond 127.0.0.1, no model, no framework.
"""

from __future__ import annotations

import contextlib
import json
import re
import socket
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from .fake_approvals import ApprovalBook

_APPROVALS_RE = re.compile(r"^/threads/([^/]+)/approvals$")
_DECISION_RE = re.compile(r"^/threads/([^/]+)/approvals/([^/]+)$")
_THREAD_RE = re.compile(r"^/threads/([^/]+)$")

Script = list[tuple[str, Any]] | Callable[[dict[str, Any]], list[tuple[str, Any]]]


def contract_sequence(
    thread_id: str = "thread-1",
    run_id: str = "run-1",
    *,
    deltas: tuple[str, ...] = ("Hello", ", world"),
) -> list[tuple[str, Any]]:
    """The full chat API event sequence for one turn."""
    events: list[tuple[str, Any]] = [
        ("message.start", {"thread_id": thread_id, "run_id": run_id}),
        ("message.delta", {"text": deltas[0]}),
        ("tool.call", {"id": "call-1", "name": "get_weather", "args": {"query": "SF"}}),
        (
            "tool.result",
            {"id": "call-1", "name": "get_weather", "result": "sunny", "is_error": False},
        ),
    ]
    events.extend(("message.delta", {"text": d}) for d in deltas[1:])
    events.append(
        (
            "message.end",
            {
                "thread_id": thread_id,
                "run_id": run_id,
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "latency_ms": 42,
                "status": "ok",
            },
        )
    )
    return events


def render_sse(events: list[tuple[str, Any]], *, keepalive: bool = True) -> bytes:
    """Serialise events as an SSE body, with a leading keep-alive comment."""
    out = [": keep-alive\n\n"] if keepalive else []
    for name, data in events:
        payload = data if isinstance(data, str) else json.dumps(data)
        out.append(f"event: {name}\ndata: {payload}\n\n")
    return "".join(out).encode("utf-8")


class FakeChatServer:
    """State shared between the test and the handler thread."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.script: Script = contract_sequence()
        self.raw_body: bytes | None = None
        self.status = 200
        self.error_body: str = json.dumps({"detail": "Unauthorized"})
        self.health: dict[str, Any] = {
            "status": "ok",
            "runtime": "fastapi",
            "checkpointer": "memory",
        }
        self.threads: dict[str, list[dict[str, Any]]] = {}
        self.card: dict[str, Any] | None = None
        self.card_path = "/a2a/app/.well-known/agent-card.json"
        self.url = ""
        # Seconds to pause after the first chunk (a silent agent turn).
        self.stall_seconds = 0.0
        # Close the connection after this many body bytes of POST /chat (a server
        # that dies mid-stream); 0 closes it before any response is sent.
        self.drop_after_bytes: int | None = None
        # The approval routes (tests/run/fake_approvals.py).
        self.book = ApprovalBook()

    def events_for(self, body: dict[str, Any]) -> list[tuple[str, Any]]:
        if callable(self.script):
            return self.script(body)
        return list(self.script)

    @property
    def chat_requests(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["path"] == "/chat"]


def _make_handler(server: FakeChatServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: Any) -> None:  # silence
            pass

        def _record(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
            rec = {
                "method": self.command,
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            }
            server.requests.append(rec)
            return rec

        def _json(self, status: int, payload: Any) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _headers(self) -> dict[str, str]:
            return {k.lower(): v for k, v in self.headers.items()}

        def _stream(self, payload: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            for i in range(0, len(payload), 64):
                self.wfile.write(payload[i : i + 64])
                self.wfile.flush()

        def do_DELETE(self) -> None:
            self._record()
            match = _THREAD_RE.match(urlsplit(self.path).path)
            if not match:
                self._json(404, {"detail": "not found"})
                return
            status = server.book.delete_thread(unquote(match.group(1)), self._headers())
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:
            self._record()
            parts = urlsplit(self.path)
            if parts.path == "/threads":
                status, rows = server.book.list_threads(self._headers())
                if status == 200:
                    query = parse_qs(parts.query)
                    offset = int(query.get("offset", ["0"])[0])
                    limit = int(query.get("limit", ["20"])[0])
                    rows = rows[offset : offset + limit]
                self._json(status, rows)
                return
            if parts.path == "/approvals":
                query = parse_qs(parts.query)
                self._json(
                    *server.book.visible(
                        self._headers(),
                        query.get("status", [None])[0],
                        int(query.get("limit", ["20"])[0]),
                        int(query.get("offset", ["0"])[0]),
                    )
                )
                return
            match = _APPROVALS_RE.match(parts.path)
            if match:
                self._json(*server.book.list(unquote(match.group(1)), self._headers()))
                return
            if self.path == "/health":
                self._json(200, server.health)
                return
            if self.path.startswith("/threads/") and self.path.endswith("/messages"):
                thread_id = self.path[len("/threads/") : -len("/messages")]
                if thread_id in server.threads:
                    self._json(200, {"messages": server.threads[thread_id]})
                else:
                    self._json(404, {"detail": "thread not found"})
                return
            if self.path == server.card_path and server.card is not None:
                self._json(200, server.card)
                return
            self._json(404, {"detail": "not found"})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                body = {"_raw": raw.decode("utf-8", "replace")}
            self._record(body)
            match = _DECISION_RE.match(urlsplit(self.path).path)
            if match:
                status, answer = server.book.decide(
                    unquote(match.group(1)), unquote(match.group(2)), self._headers(), body
                )
                if status == 200:
                    self._stream(render_sse(answer))
                else:
                    self._json(status, answer)
                return
            if self.path != "/chat":
                self._json(404, {"detail": "not found"})
                return
            if body.get("thread_id") and server.book.pending_on(body["thread_id"]):
                self._json(
                    409,
                    {"code": "approval_pending", "detail": "a call on this thread awaits approval"},
                )
                return
            if server.status != 200:
                data = server.error_body.encode("utf-8")
                self.send_response(server.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            payload = (
                server.raw_body
                if server.raw_body is not None
                else render_sse(server.events_for(body))
            )
            if server.drop_after_bytes == 0:
                self._drop()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            # Write in chunks so the client really streams.
            for i in range(0, len(payload), 64):
                if server.drop_after_bytes is not None and i >= server.drop_after_bytes:
                    self._drop()
                    return
                self.wfile.write(payload[i : i + 64])
                self.wfile.flush()
                if i == 0 and server.stall_seconds:
                    time.sleep(server.stall_seconds)

        def _drop(self) -> None:
            """Close the socket at once, as a crashed server does."""
            self.wfile.flush()
            self.close_connection = True
            with contextlib.suppress(OSError):
                self.connection.shutdown(socket.SHUT_RDWR)

    return Handler


@pytest.fixture
def chat_server() -> Iterator[FakeChatServer]:
    state = FakeChatServer()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state))
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    state.url = f"http://127.0.0.1:{port}"
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
