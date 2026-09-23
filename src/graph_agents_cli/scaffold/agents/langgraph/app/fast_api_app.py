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

"""FastAPI application: `app` (CONTRACTS section 5).

Routes:
  * `POST /chat` (SSE): `message.start`, `message.delta`, `tool.call`,
    `tool.result`, `message.end`, `error`; `thread_id` continues a thread.
  * `GET /health`: `{"status", "runtime", "checkpointer"}`.
  * `GET /threads/{thread_id}/messages`: ordered messages, ownership enforced.
  * `GET /playground`: dev-only chat page (`APP_ENV=dev`).
  * `GET /openapi.json`, `GET /docs`: dev-only as well (`APP_ENV=dev`); off otherwise.
  * A2A: card at `/a2a/<agent_directory>/.well-known/agent-card.json`, JSON-RPC at `/a2a/<agent_directory>`.

Under the fastapi runtime the lifespan binds the checkpointer chosen by
`CHECKPOINTER` to the graph and opens the app tables. Under langgraph-server
the same file is mounted as the custom app (`langgraph.json` `http.app`),
the server owns persistence, and the routes proxy to it (see `app_utils/chat.py`).
Every route except `/health`, `/playground` and the dev-only docs passes
through the policy of `app_utils/auth.py`: the routes through the
`require(action)` dependency, the A2A endpoints through the ASGI middleware
below. Under langgraph-server the server's own `/docs` and `/openapi.json`
meta routes (unauthenticated, ahead of this app's routes) still serve the
merged schema; the ingress may block those paths.

No ``from __future__ import annotations`` here: LangGraph Server loads this
file as ``user_router_module`` without registering it in ``sys.modules``, and
pydantic cannot resolve string annotations for a module it cannot find (the
``ChatBody`` schema would then be "not fully defined" and the server's OpenAPI
generation fails at startup). Real annotations need no lookup.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from {{cookiecutter.agent_directory}}.app_utils.a2a import A2A_RPC_PATH, add_a2a_routes
from {{cookiecutter.agent_directory}}.app_utils.auth import (
    Principal,
    authenticate_and_authorize,
    require,
)
from {{cookiecutter.agent_directory}}.app_utils.chat import (
    FORWARDED_HEADERS,
    RUNTIME,
    ChatRequest,
    sse_encode,
)
from {{cookiecutter.agent_directory}}.app_utils.playground import PLAYGROUND_HTML
from {{cookiecutter.agent_directory}}.app_utils.telemetry import setup_telemetry


@asynccontextmanager
async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
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
app.add_middleware(A2APolicyMiddleware, prefix=A2A_RPC_PATH)
add_a2a_routes(app)


class ChatBody(BaseModel):
    thread_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    message: str = Field(min_length=1, max_length=32_000)
    metadata: dict[str, Any] = Field(default_factory=dict)


def _forward_headers(request: Request) -> dict[str, str]:
    return {k: v for k, v in request.headers.items() if k.lower() in FORWARDED_HEADERS}


@app.post("/chat")
async def chat(
    body: ChatBody,
    request: Request,
    principal: Principal = Depends(require("chat.send")),
) -> StreamingResponse:
    req = ChatRequest(
        message=body.message,
        thread_id=body.thread_id,
        metadata=body.metadata,
        forward_headers=_forward_headers(request),
    )
    thread_id = await RUNTIME.resolve_thread(principal, req)

    async def events() -> AsyncIterator[str]:
        async for event, data in RUNTIME.stream(principal, req, thread_id):
            yield sse_encode(event, data)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "runtime": RUNTIME.runtime, "checkpointer": RUNTIME.checkpointer_kind()}


@app.get("/threads/{thread_id}/messages")
async def thread_messages(
    thread_id: str,
    request: Request,
    principal: Principal = Depends(require("thread.read")),
) -> list[dict[str, Any]]:
    return await RUNTIME.messages(principal, thread_id, _forward_headers(request))


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
