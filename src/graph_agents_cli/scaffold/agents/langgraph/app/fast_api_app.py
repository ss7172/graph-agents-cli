# Copyright 2026 Google LLC
# Modifications Copyright 2026 graph-agents-cli contributors
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

"""FastAPI application: `app` (the chat API, threads, health, metrics, playground and A2A).

Routes:
  * `POST /chat` (SSE): `message.start`, `message.delta`, `tool.call`,
    `tool.result`, `message.end`, `error`; `thread_id` continues a thread.
    409 `{"code": "thread_busy"}` while the thread has a run in progress.
  * `GET /threads`: the caller's threads, most recent first (`limit`, `offset`).
  * `GET /threads/{thread_id}/messages`: ordered messages, ownership enforced.
  * `DELETE /threads/{thread_id}`: the thread, its checkpoints and run records (owner
    only). Under langgraph-server this is the server's native route (owner-only
    through the auth handler); retention removes the run records of deleted threads.
  * `GET /health`: liveness, process only: `{"status", "runtime", "checkpointer"}`.
  * `GET /ready`: readiness: 200 when the database answers within 2 s, else
    503 `{"status": "not_ready"}`.
  * `GET /metrics`: Prometheus text (`METRICS_ENABLED`, default true).
  * `GET /playground`: dev-only chat page (`APP_ENV=dev`).
  * `GET /openapi.json`, `GET /docs`: dev-only as well (`APP_ENV=dev`); off otherwise.
  * A2A: card at `/a2a/<agent_directory>/.well-known/agent-card.json`, JSON-RPC at `/a2a/<agent_directory>`.

Under the fastapi runtime the lifespan binds the checkpointer chosen by
`CHECKPOINTER` to the graph and opens the app tables. Under langgraph-server
the same file is mounted as the custom app (`langgraph.json` `http.app`),
the server owns persistence, and the routes proxy to it (see `app_utils/chat.py`).
Every route except `/health`, `/ready`, `/metrics`, `/playground` and the
dev-only docs passes through the policy of `app_utils/auth.py`: the routes
through the `require(action)` dependency, the A2A endpoints through the ASGI
middleware below. Under langgraph-server the server's own meta routes
(`/docs`, `/openapi.json`, `/info`, `/metrics`) come ahead of this app's
routes unless they are disabled (the server image sets `disable_meta`).

Request limits: bodies over `MAX_REQUEST_BYTES` get 413 and `/chat` metadata
outside `MAX_METADATA_KEYS` / `MAX_METADATA_VALUE_CHARS` gets 422 (see
`app_utils/limits.py`). `CORS_ALLOW_ORIGINS` (comma list; empty = no CORS)
enables CORS for those origins under fastapi; LangGraph Server reads the same
variable itself. Unhandled errors answer 500 with an `error_id` that names the
logged detail.

No ``from __future__ import annotations`` here: LangGraph Server loads this
file as ``user_router_module`` without registering it in ``sys.modules``, and
pydantic cannot resolve string annotations for a module it cannot find (the
``ChatBody`` schema would then be "not fully defined" and the server's OpenAPI
generation fails at startup). Real annotations need no lookup.
"""

import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing, asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator
from starlette.types import ASGIApp, Receive, Scope, Send

from {{cookiecutter.agent_directory}}.app_utils.a2a import A2A_RPC_PATH, add_a2a_routes
from {{cookiecutter.agent_directory}}.app_utils.auth import (
    Principal,
    authenticate_and_authorize,
    require,
)
from {{cookiecutter.agent_directory}}.app_utils.chat import (
    FASTAPI,
    RUNTIME,
    ChatRequest,
    detect_runtime,
    forward_header_names,
    new_error_id,
    select_forward_headers,
    sse_encode,
)
from {{cookiecutter.agent_directory}}.app_utils.checkpointer import pool_sizes
from {{cookiecutter.agent_directory}}.app_utils.limits import (
    THREAD_ID_PATTERN,
    check_metadata,
    check_settings,
)
from {{cookiecutter.agent_directory}}.app_utils.metrics import metrics_enabled, render
from {{cookiecutter.agent_directory}}.app_utils.middleware import (
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    RunStreamingResponse,
)
from {{cookiecutter.agent_directory}}.app_utils.model import model_limits
from {{cookiecutter.agent_directory}}.app_utils.playground import PLAYGROUND_HTML
from {{cookiecutter.agent_directory}}.app_utils.telemetry import (
    bind_log_context,
    log_format,
    log_level,
    setup_logging,
    setup_telemetry,
)
from {{cookiecutter.agent_directory}}.app_utils.threads import THREAD_BUSY, ThreadBusy

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
    # A bad limit, log or pool setting stops startup instead of being guessed.
    check_settings(extra=(log_level, log_format, metrics_enabled, pool_sizes, model_limits))
    if RUNTIME.runtime == FASTAPI:
        setup_logging()  # LangGraph Server configures logging itself
    setup_telemetry()
    await RUNTIME.start()
    try:
        yield
    finally:
        await RUNTIME.stop()


class A2APolicyMiddleware:
    """Authenticate and authorize the A2A endpoints with the selected policy.

    `card.read` for the agent card, `a2a.invoke` for JSON-RPC. The principal
    is stored in the request state for the executor.
    """

    def __init__(self, app: ASGIApp, prefix: str) -> None:
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(self.prefix):
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        action = "card.read" if scope["path"].endswith("agent-card.json") else "a2a.invoke"
        try:
            principal = await authenticate_and_authorize(request, action)
        except HTTPException as exc:
            response = JSONResponse(
                {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
            )
            await response(scope, receive, send)
            return
        scope.setdefault("state", {})["principal"] = principal
        await self.app(scope, receive, send)


def _dev_mode() -> bool:
    return (os.environ.get("APP_ENV") or "").strip().lower() == "dev"


def cors_origins() -> list[str]:
    """`CORS_ALLOW_ORIGINS` as a list; empty means no CORS middleware."""
    return [o.strip() for o in (os.environ.get("CORS_ALLOW_ORIGINS") or "").split(",") if o.strip()]


# The schema and Swagger UI are unauthenticated FastAPI defaults; like
# /playground they exist only under APP_ENV=dev. app.openapi() still works
# (LangGraph Server builds its spec from it).
app = FastAPI(
    title="{{cookiecutter.project_name}}",
    description="LangGraph agent: chat SSE API and A2A protocol",
    version=os.environ.get("AGENT_VERSION", "0.1.0"),
    lifespan=lifespan,
    openapi_url="/openapi.json" if _dev_mode() else None,
    docs_url="/docs" if _dev_mode() else None,
    redoc_url=None,
)
# Starlette runs the last-added middleware first: request context, then the
# body cap, then CORS (answers preflights before auth), then the A2A policy.
app.add_middleware(A2APolicyMiddleware, prefix=A2A_RPC_PATH)
if detect_runtime() == FASTAPI and cors_origins():
    _origins = cors_origins()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        # Cookies (custom policies) only with explicit origins, never with "*".
        allow_credentials="*" not in _origins,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["authorization", "content-type", "x-request-id", *forward_header_names()],
        expose_headers=["x-request-id"],
    )
app.add_middleware(BodySizeLimitMiddleware)
app.add_middleware(RequestContextMiddleware)
add_a2a_routes(app)


@app.exception_handler(ThreadBusy)
async def thread_busy_handler(request: Request, exc: ThreadBusy) -> JSONResponse:
    return JSONResponse(status_code=409, content={"code": THREAD_BUSY, "detail": str(exc)})


def _database_unavailable(exc: Exception) -> bool:
    from psycopg import OperationalError
    from psycopg_pool import PoolTimeout

    return isinstance(exc, OperationalError | PoolTimeout)


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """500 (503 when the database is unreachable) with an id naming the logged detail."""
    error_id = new_error_id()
    logger.error("unhandled error (error_id=%s)", error_id, exc_info=exc)
    if _database_unavailable(exc):
        status, detail = 503, f"Database unavailable. Reference: {error_id}."
    else:
        status, detail = 500, f"Internal server error. Reference: {error_id}."
    return JSONResponse(status_code=status, content={"detail": detail, "error_id": error_id})


class ChatBody(BaseModel):
    thread_id: str | None = Field(default=None, pattern=THREAD_ID_PATTERN)
    message: str = Field(min_length=1, max_length=32_000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def _cap_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        return check_metadata(value)


def _forward_headers(request: Request) -> dict[str, str]:
    return select_forward_headers(request.headers)


def principal_for(action: str) -> Callable[[Request], Awaitable[Principal]]:
    """`require(action)`, plus the caller's hashed id on every log record of the request."""
    check = require(action)

    async def dependency(request: Request) -> Principal:
        principal = await check(request)
        bind_log_context(principal_hash=principal.hashed_id())
        return principal

    dependency.__name__ = f"principal_for_{action.replace('.', '_')}"
    return dependency


@app.post("/chat")
async def chat(
    body: ChatBody,
    request: Request,
    principal: Principal = Depends(principal_for("chat.send")),
) -> RunStreamingResponse:
    req = ChatRequest(
        message=body.message,
        thread_id=body.thread_id,
        metadata=body.metadata,
        forward_headers=_forward_headers(request),
    )
    thread_id = await RUNTIME.resolve_thread(principal, req)
    lease = await RUNTIME.acquire_thread(thread_id)  # ThreadBusy -> 409 before streaming

    async def events() -> AsyncIterator[str]:
        async with aclosing(RUNTIME.stream(principal, req, thread_id, lease=lease)) as stream:
            async for event, data in stream:
                yield sse_encode(event, data)

    return RunStreamingResponse(
        events(),
        on_close=lease.release,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "runtime": RUNTIME.runtime, "checkpointer": RUNTIME.checkpointer_kind()}


@app.get("/ready")
async def ready() -> JSONResponse:
    if await RUNTIME.ready():
        return JSONResponse({"status": "ready"})
    return JSONResponse({"status": "not_ready"}, status_code=503)


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics() -> Response:
    if not metrics_enabled():
        raise HTTPException(status_code=404, detail="Not found")
    payload, content_type = render()
    return Response(content=payload, media_type=content_type)


@app.get("/threads")
async def list_threads(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100_000),
    principal: Principal = Depends(principal_for("thread.list")),
) -> list[dict[str, Any]]:
    return await RUNTIME.list_threads(
        principal, limit=limit, offset=offset, forward_headers=_forward_headers(request)
    )


@app.get("/threads/{thread_id}/messages")
async def thread_messages(
    thread_id: str,
    request: Request,
    principal: Principal = Depends(principal_for("thread.read")),
) -> list[dict[str, Any]]:
    return await RUNTIME.messages(principal, thread_id, _forward_headers(request))


async def delete_thread(
    thread_id: str,
    request: Request,
    principal: Principal = Depends(principal_for("thread.delete")),
) -> Response:
    await RUNTIME.delete_thread(principal, thread_id, _forward_headers(request))
    return Response(status_code=204)


# Under langgraph-server the server owns `DELETE /threads/{thread_id}` (its
# native API, owner-only through the auth handler); a route of this app would
# shadow it, the app's own loopback delete included.
if detect_runtime() == FASTAPI:
    app.delete("/threads/{thread_id}", status_code=204)(delete_thread)


@app.get("/playground", response_class=HTMLResponse, include_in_schema=False)
async def playground() -> HTMLResponse:
    if not _dev_mode():
        raise HTTPException(status_code=404, detail="Not found")
    return HTMLResponse(PLAYGROUND_HTML)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )
