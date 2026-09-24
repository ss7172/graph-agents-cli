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
    `tool.result`, `message.end`, `error`; `thread_id` continues a thread
    (omit it to start one: the server generates a random id).
    409 `{"code": "thread_busy"}` while the thread has a run in progress.
    A failed tool call's `tool.result` carries an `error_id` and, outside
    `APP_ENV=dev`, a generic `result` (the error text is for the model only).
  * `GET /threads`: the caller's threads, most recent first (`limit`, `offset`);
    `scope=all` lists every principal's (read-across roles only). Each row
    names its owner hashed (`owner`).
  * `GET /threads/{thread_id}/messages`: ordered messages, ownership enforced.
  * `DELETE /threads/{thread_id}`: the thread, its checkpoints and run records (owner
    only), and the A2A tasks of that conversation. Under langgraph-server this
    is the server's native route (owner-only through the auth handler); once it
    succeeds, the app drops the thread's run records and A2A tasks
    (`ThreadDeleteHookMiddleware`).
  * `GET /health`: liveness, process only: `{"status", "runtime", "checkpointer"}`.
  * `GET /ready`: readiness: 200 when the database answers within 2 s, else
    503 `{"status": "not_ready"}`.
  * `GET /metrics`: Prometheus text (`METRICS_ENABLED`, default true); with
    `METRICS_TOKEN` set, only for `Authorization: Bearer <METRICS_TOKEN>`.
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
routes unless they are disabled (the server image sets `disable_meta`), and
the server's native API gets its auth errors from `AuthErrorMiddleware`.

Request limits: bodies over `MAX_REQUEST_BYTES` get 413, a message over
`MAX_MESSAGE_CHARS` (the same cap A2A applies) and `/chat` metadata outside
`MAX_METADATA_KEYS` / `MAX_METADATA_VALUE_CHARS` get 422 (see
`app_utils/limits.py`). `CORS_ALLOW_ORIGINS` (comma list; empty = no CORS)
enables CORS for those origins under fastapi; LangGraph Server reads the same
variable itself. Unhandled errors answer 500 with an `error_id` that names the
logged detail (and the request's `X-Request-ID`). A 422 never echoes the
request's values (`input`), so it cannot fail on text that is not valid
UTF-8 (an unpaired surrogate) or on the NaN or Infinity Python's JSON parser
lets through, and never returns a long message back to its sender.

`.env` is read (below the process environment) as this module is imported,
before any setting is: the A2A card, the docs switch, CORS and the auth
policy's startup check are fixed at import. Under the fastapi runtime
logging is configured on import too, so import-time warnings and uvicorn's
startup lines follow `LOG_FORMAT`.

No ``from __future__ import annotations`` here: LangGraph Server loads this
file as ``user_router_module`` without registering it in ``sys.modules``, and
pydantic cannot resolve string annotations for a module it cannot find (the
``ChatBody`` schema would then be "not fully defined" and the server's OpenAPI
generation fails at startup). Real annotations need no lookup.
"""

import contextlib
import json
import logging
import math
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing, asynccontextmanager
from typing import Any

from dotenv import dotenv_values
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator
from starlette.types import ASGIApp, Receive, Scope, Send

# `.env` first, below the process environment (as `load_dotenv()` does): the
# modules imported next, and this one, read settings as they load (A2A_NAME,
# the A2A card's auth scheme, APP_ENV for the docs, CORS_ALLOW_ORIGINS).
# PYTHON_DOTENV_DISABLED switches it off, as it does `load_dotenv()`: the
# project's tests set it so a developer's `.env` never reaches them.
os.environ.update(
    {
        key: value
        for key, value in dotenv_values().items()
        if value is not None and key not in os.environ
    }
    if os.environ.get("PYTHON_DOTENV_DISABLED", "").strip().lower()
    not in {"1", "true", "t", "yes", "y"}
    else {}
)

from {{cookiecutter.agent_directory}}.app_utils.a2a import (
    A2A_RPC_PATH,
    add_a2a_routes,
    forget_context,
    legacy_message_problem,
    task_ttl_s,
)
from {{cookiecutter.agent_directory}}.app_utils.auth import (
    Principal,
    authenticate_and_authorize,
    require,
)
from {{cookiecutter.agent_directory}}.app_utils.chat import (
    EVENT_TOOL_RESULT,
    FASTAPI,
    LANGGRAPH_SERVER,
    RUNTIME,
    ChatRequest,
    detect_runtime,
    dev_mode,
    forward_header_names,
    new_error_id,
    select_forward_headers,
    sse_encode,
    unavailable,
    validate_thread_id,
)
from {{cookiecutter.agent_directory}}.app_utils.checkpointer import pool_sizes
from {{cookiecutter.agent_directory}}.app_utils.content import client_message, client_tool_result
from {{cookiecutter.agent_directory}}.app_utils.limits import (
    THREAD_ID_PATTERN,
    SettingsError,
    check_metadata,
    check_settings,
)
from {{cookiecutter.agent_directory}}.app_utils.metrics import (
    metrics_authorized,
    metrics_enabled,
    render,
)
from {{cookiecutter.agent_directory}}.app_utils.middleware import (
    REQUEST_ID_HEADER,
    AuthErrorMiddleware,
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    RunStreamingResponse,
    ThreadDeleteHookMiddleware,
    max_message_chars,
    read_body,
    replay_body,
)
from {{cookiecutter.agent_directory}}.app_utils.model import model_limits
from {{cookiecutter.agent_directory}}.app_utils.playground import PLAYGROUND_HTML
from {{cookiecutter.agent_directory}}.app_utils.telemetry import (
    bind_log_context,
    log_format,
    log_level,
    setup_logging,
    setup_server_logging,
    setup_telemetry,
    trace_capture,
)
from {{cookiecutter.agent_directory}}.app_utils.threads import (
    SCOPE_ALL,
    SCOPE_OWN,
    THREAD_BUSY,
    ThreadBusy,
    check_scope,
    own_view,
    search_server_threads,
)

logger = logging.getLogger(__name__)

if detect_runtime() == FASTAPI:
    # Now rather than in the lifespan: what is logged before it (the A2A
    # card's APP_URL warning below, uvicorn's startup lines) follows
    # LOG_FORMAT too. A bad LOG_LEVEL or LOG_FORMAT is left to the lifespan's
    # settings check, which reports every bad setting at once.
    with contextlib.suppress(SettingsError):
        setup_logging()
else:
    # LangGraph Server configures logging itself; keep query strings and
    # outbound URLs out of its lines.
    setup_server_logging()


@asynccontextmanager
async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
    # A bad limit, log, metrics, pool, model, trace-capture or A2A setting
    # stops startup instead of being guessed.
    check_settings(
        extra=(
            log_level,
            log_format,
            metrics_enabled,
            pool_sizes,
            model_limits,
            trace_capture,
            task_ttl_s,
            max_message_chars,
        )
    )
    if RUNTIME.runtime == FASTAPI:
        setup_logging()
    else:
        setup_server_logging()  # LangGraph Server configures logging itself
    setup_telemetry()
    await RUNTIME.start()
    try:
        yield
    finally:
        await RUNTIME.stop()


class A2APolicyMiddleware:
    """Authenticate and authorize the A2A endpoints with the selected policy.

    `card.read` for the agent card, `a2a.invoke` for JSON-RPC. The principal
    is stored in the request state for the executor. An A2A 0.3
    `message/send` or `message/stream` whose message cannot run (no text, an
    empty text part, over `MAX_MESSAGE_CHARS`) is answered here with the
    JSON-RPC invalid-params error: past this point the SDK's 0.3 layer would
    turn it into an internal error (1.0 requests are checked by the handler).
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
        if action == "a2a.invoke" and scope.get("method") == "POST":
            try:
                body = await read_body(receive)
            except HTTPException as exc:  # the body cap, while reading a chunked body
                await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(
                    scope, receive, send
                )
                return
            refusal = _legacy_refusal(body)
            if refusal is not None:
                await JSONResponse(refusal)(scope, receive, send)
                return
            receive = replay_body(body, receive)
        await self.app(scope, receive, send)


def _legacy_refusal(body: bytes) -> dict[str, Any] | None:
    """The JSON-RPC invalid-params answer to an A2A 0.3 message that cannot run, or None."""
    if b"message/" not in body:  # no 0.3 send method named: nothing to parse
        return None
    try:
        payload = json.loads(body)
    except ValueError:
        return None  # the SDK answers a parse error itself
    problem = legacy_message_problem(payload)
    if problem is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": payload.get("id"),
        "error": {"code": -32602, "message": problem},
    }


def _dev_mode() -> bool:
    """`APP_ENV` is exactly `dev` (as `auth.dev_mode`)."""
    return os.environ.get("APP_ENV") == "dev"


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
# body cap, then (langgraph-server) the native API's auth errors and thread
# deletes, then CORS (answers preflights before auth), then the A2A policy.
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


async def _server_thread_deleted(thread_id: str) -> None:
    """langgraph-server: after the server deleted a thread, its run records and A2A tasks."""
    try:
        await RUNTIME.forget_thread_runs(thread_id)
    finally:
        await forget_context(thread_id)


if detect_runtime() != FASTAPI:
    app.add_middleware(ThreadDeleteHookMiddleware, on_deleted=_server_thread_deleted)
    app.add_middleware(AuthErrorMiddleware)
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
    # This handler answers from outside RequestContextMiddleware, which adds
    # the header to every other response; it left the request id in the state.
    request_id = getattr(request.state, "request_id", None)
    headers = {REQUEST_ID_HEADER: request_id} if isinstance(request_id, str) else None
    return JSONResponse(
        status_code=status, content={"detail": detail, "error_id": error_id}, headers=headers
    )


def _json_safe(value: Any) -> Any:
    """`value` encodable as UTF-8 JSON: NaN and infinite floats spelled as strings (JSON
    has neither), unpaired surrogates as `\\udXXX` escapes (UTF-8 has none)."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, str):
        return value.encode("utf-8", "backslashreplace").decode("utf-8")
    if isinstance(value, dict):
        return {_json_safe(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    return value


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's 422 without the offending values.

    The default handler echoes each value back in `input`: a 32,000-character
    message, text with an unpaired surrogate (which then fails to encode: a
    500, logged with the message in its traceback), or a NaN. Only where
    (`loc`), what (`type`, `msg`) and the rule's parameters (`ctx`) are kept.
    """
    errors = [
        {key: value for key, value in error.items() if key not in ("input", "url")}
        for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": _json_safe(jsonable_encoder(errors))})


def _has_surrogate(value: Any) -> bool:
    """Whether a string (or a key or value of a mapping) holds an unpaired surrogate."""
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return True
        return False
    if isinstance(value, dict):
        return any(_has_surrogate(k) or _has_surrogate(v) for k, v in value.items())
    return False


class ChatBody(BaseModel):
    # Omit thread_id to start a thread: the server generates a random id. Ids
    # are one namespace shared by every caller, so an id a client picks must
    # be unguessable (an id another principal used first is theirs: 403).
    thread_id: str | None = Field(default=None, pattern=THREAD_ID_PATTERN)
    message: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("message")
    @classmethod
    def _cap_message(cls, value: str) -> str:
        cap = max_message_chars()
        if len(value) > cap:
            raise ValueError(f"message is longer than {cap} characters (MAX_MESSAGE_CHARS).")
        if _has_surrogate(value):
            raise ValueError("message is not valid Unicode text (an unpaired surrogate).")
        return value

    @field_validator("metadata")
    @classmethod
    def _cap_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        if _has_surrogate(value):
            raise ValueError("metadata is not valid Unicode text (an unpaired surrogate).")
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
    # ThreadBusy -> 409 before streaming; the owner is checked again under the lock.
    lease = await RUNTIME.acquire_thread(thread_id, principal)

    dev = dev_mode()

    async def events() -> AsyncIterator[str]:
        async with aclosing(RUNTIME.stream(principal, req, thread_id, lease=lease)) as stream:
            async for event, data in stream:
                if event == EVENT_TOOL_RESULT:
                    # A failed call's text (policy rules, limits, upstream
                    # errors) is for the model; the client gets an error id.
                    data = client_tool_result(data, thread_id=thread_id, dev=dev)
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
async def prometheus_metrics(request: Request) -> Response:
    if not metrics_enabled():
        raise HTTPException(status_code=404, detail="Not found")
    if not metrics_authorized(request.headers.get("authorization")):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid metrics token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload, content_type = render()
    return Response(content=payload, media_type=content_type)


@app.get("/threads")
async def list_threads(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100_000),
    scope: str = Query(default=SCOPE_OWN, pattern="^(own|all)$"),
    principal: Principal = Depends(principal_for("thread.list")),
) -> list[dict[str, Any]]:
    """The caller's threads, or with `scope=all` every principal's (read-across roles only).

    Each row is `{thread_id, owner, created_at, updated_at}`, `owner` being the
    owner's hashed principal id.
    """
    check_scope(principal, scope)
    headers = _forward_headers(request)
    if scope == SCOPE_ALL and RUNTIME.runtime == LANGGRAPH_SERVER:
        try:
            return await search_server_threads(
                RUNTIME._sdk_client(headers), principal, scope=scope, limit=limit, offset=offset
            )
        except Exception as exc:
            raise unavailable("LangGraph Server", exc) from exc
    rows = await RUNTIME.list_threads(
        principal if scope == SCOPE_ALL else own_view(principal),
        limit=limit,
        offset=offset,
        forward_headers=headers,
    )
    if scope == SCOPE_OWN:
        owner = principal.hashed_id()
        rows = [{**row, "owner": owner} for row in rows]
    return rows


@app.get("/threads/{thread_id}/messages")
async def thread_messages(
    thread_id: str,
    request: Request,
    principal: Principal = Depends(principal_for("thread.read")),
) -> list[dict[str, Any]]:
    messages = await RUNTIME.messages(principal, thread_id, _forward_headers(request))
    # A failed tool call reads as in the stream: an error id (derived from the
    # thread id in its canonical form, as the stream has it), not the error text.
    canonical = validate_thread_id(thread_id, RUNTIME.runtime)
    dev = dev_mode()
    return [client_message(m, thread_id=canonical, dev=dev) for m in messages]


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
        log_config=None,  # keep the logging configured above (LOG_FORMAT)
    )
