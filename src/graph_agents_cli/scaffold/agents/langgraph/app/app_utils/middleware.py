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

"""HTTP plumbing shared by every route: request ids, metrics, the body cap, SSE responses.

* `RequestContextMiddleware`: takes the caller's `X-Request-ID` (letters,
  digits and `._:-`, at most 128) or makes one, returns it on the response,
  binds it to every log record of the request, and records the request metrics.
* `BodySizeLimitMiddleware`: refuses a body over `MAX_REQUEST_BYTES` with 413,
  from `Content-Length` before reading anything, or while reading a chunked body.
* `RunStreamingResponse`: the `/chat` SSE response. It watches for the client
  going away and cancels the stream right then (the run is recorded as
  `cancelled` and stops spending model tokens), and it always calls
  `on_close` (which releases the thread's run lock), even when the stream
  never started.

Under langgraph-server the server installs this app's middleware for every
route, its own native API included.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from {{cookiecutter.agent_directory}}.app_utils.limits import max_request_bytes
from {{cookiecutter.agent_directory}}.app_utils.metrics import observe_request, route_label
from {{cookiecutter.agent_directory}}.app_utils.telemetry import bind_log_context

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "x-request-id"
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return value.decode("latin-1")
    return None


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = _header(scope, REQUEST_ID_HEADER.encode())
        if incoming and _REQUEST_ID.fullmatch(incoming):
            request_id = incoming
        else:
            request_id = uuid.uuid4().hex
            # Downstream code (LangGraph Server's own request-id middleware
            # included) sees the same id.
            scope["headers"] = [
                (k, v) for k, v in scope.get("headers") or () if k.lower() != b"x-request-id"
            ] + [(b"x-request-id", request_id.encode())]
        scope.setdefault("state", {})["request_id"] = request_id
        bind_log_context(request_id=request_id, run_id=None, thread_id=None, principal_hash=None)
        status = 500
        started = time.perf_counter()

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            observe_request(
                scope.get("method", "GET"),
                route_label(scope),
                status,
                time.perf_counter() - started,
            )


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = max_request_bytes()
        detail = f"Request body exceeds MAX_REQUEST_BYTES ({limit} bytes)."
        declared = _header(scope, b"content-length")
        if declared is not None:
            try:
                too_large = int(declared) > limit
            except ValueError:
                too_large = False  # the server rejects a malformed length itself
            if too_large:
                await JSONResponse({"detail": detail}, status_code=413)(scope, receive, send)
                return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body") or b"")
                if received > limit:
                    # Raised inside the route's body read: FastAPI and Starlette
                    # both turn it into the 413 response.
                    raise HTTPException(status_code=413, detail=detail)
            return message

        await self.app(scope, limited_receive, send)


class RunStreamingResponse(StreamingResponse):
    """A streaming response that stops as soon as the client disconnects."""

    def __init__(
        self,
        content: Any,
        *,
        on_close: Callable[[], Awaitable[None]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(content, **kwargs)
        self._on_close = on_close

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        task = asyncio.current_task()
        disconnected = False

        async def watch_disconnect() -> None:
            nonlocal disconnected
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    disconnected = True
                    if task is not None:
                        task.cancel()
                    return

        watcher = asyncio.ensure_future(watch_disconnect())
        try:
            await self.stream_response(send)
        except asyncio.CancelledError:
            if not disconnected or task is None:
                raise
            task.uncancel()  # the client left: a normal end for this request
        except OSError:
            pass  # the client left while a chunk was being sent
        finally:
            watcher.cancel()
            aclose = getattr(self.body_iterator, "aclose", None)
            try:
                if aclose is not None:
                    await aclose()
            finally:
                if self._on_close is not None:
                    await self._on_close()
