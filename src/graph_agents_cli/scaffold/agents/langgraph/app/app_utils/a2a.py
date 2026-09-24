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

"""A2A protocol layer: the executor, the agent card, and the routes.

The executor drives the same invocation path as `/chat` (`ChatRuntime`), so
the A2A `contextId` is the chat thread id and the same policy, run records
and thread ownership apply. The card is served at
`/a2a/<agent_directory>/.well-known/agent-card.json` and JSON-RPC at
`/a2a/<agent_directory>`; both are guarded by the policy middleware in
`fast_api_app.py` (`card.read` / `a2a.invoke`), and the card advertises the
resulting security scheme.

A2A tasks belong to the principal that created them: the call context's user
is the authenticated principal, and the task store keys every task by that
principal's id, so ListTasks, GetTask, CancelTask and SubscribeToTask only
ever see the caller's own tasks (another principal's task id reads as "not
found"). The task store is in process memory: it is per replica (a task
created on one pod is not visible on another) and a task is evicted
`A2A_TASK_TTL_S` seconds (default 3600; 0 keeps tasks until restart) after
its last update. The conversation itself is the thread, which is durable
under `CHECKPOINTER=postgres`.
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from collections.abc import Callable
from typing import Any

from a2a.auth.user import User
from a2a.helpers import new_task_from_user_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.server.routes.common import DefaultServerCallContextBuilder
from a2a.server.tasks import InMemoryTaskStore, TaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    APIKeySecurityScheme,
    HTTPAuthSecurityScheme,
    Part,
    SecurityScheme,
    Task,
)
from a2a.types.a2a_pb2 import ListTasksRequest, ListTasksResponse
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH, PROTOCOL_VERSION_1_0
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from {{cookiecutter.agent_directory}}.app_utils.auth import (
    CUSTOM,
    JWT,
    Principal,
    check_startup,
    policy_name,
)
from {{cookiecutter.agent_directory}}.app_utils.chat import (
    EVENT_DELTA,
    EVENT_ERROR,
    RUNTIME,
    ChatRequest,
)
from {{cookiecutter.agent_directory}}.app_utils.limits import SettingsError

logger = logging.getLogger(__name__)

# Mount name for the A2A endpoint: the agent directory, so it matches the
# `graph-agents-cli run --mode a2a` default.
A2A_NAME = os.environ.get("A2A_NAME") or "{{cookiecutter.agent_directory}}"
A2A_RPC_PATH = f"/a2a/{A2A_NAME}"
A2A_CARD_PATH = f"{A2A_RPC_PATH}{AGENT_CARD_WELL_KNOWN_PATH}"
DESCRIPTION = "{{cookiecutter.project_name}}: a LangGraph agent served over the A2A protocol."


def advertised_base_url() -> str:
    """Base URL for the agent card: APP_URL, else the host and port we bind.

    The chart sets APP_URL from `appUrl` or the gateway/ingress hostname; a
    deployment without one (APP_ENV other than dev) is warned about, because
    A2A clients dial the card's URL, not the URL they fetched the card from.
    """
    if app_url := os.environ.get("APP_URL"):
        return app_url.rstrip("/")
    host = os.environ.get("HOST", "127.0.0.1")
    if host in ("0.0.0.0", "::", ""):  # what we bind is not what clients dial
        host = "127.0.0.1"
    fallback = f"http://{host}:{os.environ.get('PORT', '8000')}"
    if (os.environ.get("APP_ENV") or "dev") != "dev":
        logger.warning(
            "APP_URL is not set: the A2A agent card advertises %s (the bind address), which "
            "remote clients cannot reach. Set appUrl (or a gateway/ingress hostname) in the "
            "chart values, or APP_URL in the environment.",
            fallback,
        )
    return fallback


DEFAULT_TASK_TTL_S = 3600
# The same rule as the chat API's `thread_id`: the A2A contextId becomes the thread id.
CONTEXT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def task_ttl_s() -> int:
    """`A2A_TASK_TTL_S`: seconds a task is kept after its last update; 0 = until restart.

    Anything but a whole number >= 0 is a `SettingsError`, which the app's
    startup settings check reports with every other bad setting.
    """
    raw = (os.environ.get("A2A_TASK_TTL_S") or "").strip()
    if not raw:
        return DEFAULT_TASK_TTL_S
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(f"A2A_TASK_TTL_S={raw!r} is not a whole number of seconds.") from None
    if value < 0:
        raise SettingsError(f"A2A_TASK_TTL_S={value} must be >= 0.")
    return value


class PrincipalUser(User):
    """The authenticated principal as an A2A user: its id is the task owner."""

    def __init__(self, principal_id: str) -> None:
        self._principal_id = principal_id

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def user_name(self) -> str:
        return self._principal_id


def task_owner(context: ServerCallContext) -> str:
    """The task store's owner key. Fails closed without an authenticated principal."""
    user = context.user
    if not isinstance(user, PrincipalUser) or not user.user_name:
        raise PermissionError("A2A task access without an authenticated principal")
    return user.user_name


class PolicyContextBuilder(DefaultServerCallContextBuilder):
    """The default call context plus the principal set by the policy middleware.

    The context's user is that principal, so the task store scopes tasks per principal.
    """

    def build(self, request: Request) -> ServerCallContext:
        context = super().build(request)
        context.state["principal"] = getattr(request.state, "principal", None)
        # The credential was checked by the middleware; keep it out of the call
        # context, which the executor and any code it calls can see.
        headers = context.state.get("headers")
        if isinstance(headers, dict):
            for name in ("authorization", "proxy-authorization", "cookie"):
                headers.pop(name, None)
        return context

    def build_user(self, request: Request) -> User:
        principal = getattr(request.state, "principal", None)
        if not isinstance(principal, Principal) or not principal.id:
            # The middleware authenticates every A2A request before it gets here.
            raise PermissionError("A2A request without an authenticated principal")
        return PrincipalUser(principal.id)


class ExpiringTaskStore(TaskStore):
    """The SDK's in-memory task store, scoped by `task_owner`, with TTL eviction.

    A task is evicted `ttl_s` seconds after its last save (`ttl_s <= 0`
    disables eviction); expired tasks are dropped on access and by a sweep
    that runs at most once a minute. Per process: replicas do not share it.
    """

    def __init__(self, ttl_s: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl_s = ttl_s
        self._clock = clock
        self._inner = InMemoryTaskStore(owner_resolver=task_owner)
        self._saved_at: dict[str, dict[str, float]] = {}
        self._sweep_every = max(1.0, min(60.0, ttl_s / 2)) if ttl_s > 0 else 0.0
        self._next_sweep = 0.0

    @staticmethod
    def _context_for(owner: str) -> ServerCallContext:
        return ServerCallContext(user=PrincipalUser(owner))

    def _expired(self, owner: str, task_id: str, now: float) -> bool:
        saved = self._saved_at.get(owner, {}).get(task_id)
        return self.ttl_s > 0 and saved is not None and now - saved >= self.ttl_s

    def _forget(self, owner: str, task_id: str) -> None:
        tasks = self._saved_at.get(owner)
        if tasks is not None:
            tasks.pop(task_id, None)
            if not tasks:
                self._saved_at.pop(owner, None)

    async def _evict_expired(self, owner: str | None = None) -> None:
        """Drop the expired tasks of `owner` (of every owner when None)."""
        if self.ttl_s <= 0:
            return
        now = self._clock()
        for name in [owner] if owner is not None else list(self._saved_at):
            expired = [t for t in list(self._saved_at.get(name, {})) if self._expired(name, t, now)]
            for task_id in expired:
                await self._inner.delete(task_id, self._context_for(name))
                self._forget(name, task_id)

    async def _maybe_sweep(self) -> None:
        now = self._clock()
        if self.ttl_s > 0 and now >= self._next_sweep:
            self._next_sweep = now + self._sweep_every
            await self._evict_expired()

    async def save(self, task: Task, context: ServerCallContext) -> None:
        owner = task_owner(context)
        await self._maybe_sweep()
        await self._inner.save(task, context)
        self._saved_at.setdefault(owner, {})[task.id] = self._clock()

    async def get(self, task_id: str, context: ServerCallContext) -> Task | None:
        owner = task_owner(context)
        await self._maybe_sweep()
        await self._evict_expired(owner)
        return await self._inner.get(task_id, context)

    async def list(self, params: ListTasksRequest, context: ServerCallContext) -> ListTasksResponse:
        owner = task_owner(context)
        await self._maybe_sweep()
        await self._evict_expired(owner)
        return await self._inner.list(params, context)

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        owner = task_owner(context)
        await self._inner.delete(task_id, context)
        self._forget(owner, task_id)


def _client_message(exc: Exception) -> str:
    """What an A2A client is told about a failure: a 4xx detail as is, anything else generic."""
    if isinstance(exc, HTTPException) and exc.status_code < 500:
        return str(exc.detail)
    error_id = uuid.uuid4().hex[:12]
    logger.error("A2A task failed (error id %s)", error_id, exc_info=exc)
    return f"The agent could not process this request (error id {error_id})."


class LangGraphAgentExecutor(AgentExecutor):
    """Bridge the A2A request lifecycle to the chat runtime."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        user_input = context.get_user_input()
        task = context.current_task
        if task is None:
            # First message of a conversation. The Task itself has to reach the
            # queue before any status update, or the server rejects the stream.
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.start_work()

        state = context.call_context.state if context.call_context is not None else {}
        principal = state.get("principal")
        if not isinstance(principal, Principal):
            # Never run as an anonymous principal: the middleware must have authenticated.
            await updater.failed(updater.new_agent_message([Part(text="Not authenticated.")]))
            return
        if task.context_id and not CONTEXT_ID_PATTERN.fullmatch(task.context_id):
            await updater.failed(
                updater.new_agent_message(
                    [Part(text="Invalid contextId: use 1-128 characters from A-Z a-z 0-9 _ . : -")]
                )
            )
            return
        req = ChatRequest(message=user_input, thread_id=task.context_id or None)
        try:
            thread_id = await RUNTIME.resolve_thread(principal, req)
        except Exception as exc:  # ownership or server errors end the task
            await updater.failed(updater.new_agent_message([Part(text=_client_message(exc))]))
            return

        # Stream the reply as task artifacts (A2A clients read the reply from
        # artifacts, not from the final status message).
        artifact_id = uuid.uuid4().hex
        streamed = False
        async for event, data in RUNTIME.stream(principal, req, thread_id):
            if event == EVENT_DELTA and data.get("text"):
                await updater.add_artifact(
                    [Part(text=data["text"])],
                    artifact_id=artifact_id,
                    name="response",
                    append=streamed,
                    last_chunk=False,
                )
                streamed = True
            elif event == EVENT_ERROR:
                await updater.failed(
                    updater.new_agent_message(
                        [Part(text=f"{data.get('code')}: {data.get('message')}")]
                    )
                )
                return
        if not streamed:
            await updater.add_artifact([Part(text="")], artifact_id=artifact_id, name="response")
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # The request handler has already checked that the caller owns the task
        # (the task store is scoped per principal); it then stops the running
        # `execute`, which ends the run, and records the task as canceled.
        logger.info("A2A task %s canceled by its owner", context.task_id)


def _security() -> tuple[dict[str, SecurityScheme], str]:
    name = policy_name()
    if name == JWT:
        return (
            {
                "bearer": SecurityScheme(
                    http_auth_security_scheme=HTTPAuthSecurityScheme(
                        scheme="bearer",
                        bearer_format="JWT",
                        description="OIDC/JWT access token of the calling user.",
                    )
                )
            },
            "bearer",
        )
    if name == CUSTOM:
        return (
            {
                "custom": SecurityScheme(
                    api_key_security_scheme=APIKeySecurityScheme(
                        name="Authorization",
                        location="header",
                        description="Credential defined by the project's custom auth policy.",
                    )
                )
            },
            "custom",
        )
    return (
        {
            "bearer": SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(
                    scheme="bearer", description="Shared bearer key (API_KEY)."
                )
            )
        },
        "bearer",
    )


def agent_card() -> AgentCard:
    rpc_url = f"{advertised_base_url()}{A2A_RPC_PATH}"
    schemes, required = _security()
    card = AgentCard(
        name=A2A_NAME,
        description=DESCRIPTION,
        # One 1.0 interface: advertising 0.3 as well steers 1.0 clients to the
        # wrong version. 0.3 clients are still served on the same URL
        # (enable_v0_3_compat below).
        supported_interfaces=[
            AgentInterface(
                url=rpc_url, protocol_binding="JSONRPC", protocol_version=PROTOCOL_VERSION_1_0
            ),
        ],
        version=os.environ.get("AGENT_VERSION", "0.1.0"),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=True),
        security_schemes=schemes,
        skills=[
            AgentSkill(
                id="chat",
                name="chat",
                description="Hold a conversation with the agent.",
                tags=["chat", "langgraph"],
            )
        ],
    )
    requirement = card.security_requirements.add()
    requirement.schemes[required].SetInParent()
    return card


def add_a2a_routes(app: FastAPI) -> Any:
    """Mount the JSON-RPC endpoint and the agent card on the app.

    The card advertises the selected auth policy, so the policy is built and
    checked here, when the app is assembled: a misconfigured policy stops the
    process at startup (outside `APP_ENV=dev`) instead of failing requests.
    """
    check_startup()
    card = agent_card()
    try:
        ttl = task_ttl_s()
    except SettingsError:
        # The app is assembled at import; its lifespan's settings check then
        # refuses to start and names this variable with every other bad one.
        ttl = DEFAULT_TASK_TTL_S
    request_handler = DefaultRequestHandler(
        agent_executor=LangGraphAgentExecutor(),
        task_store=ExpiringTaskStore(ttl),
        agent_card=card,
    )
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card, card_url=A2A_CARD_PATH),
        # v0.3 compat keeps older A2A clients working against the same endpoint.
        jsonrpc_routes=create_jsonrpc_routes(
            request_handler,
            rpc_url=A2A_RPC_PATH,
            context_builder=PolicyContextBuilder(),
            enable_v0_3_compat=True,
        ),
    )
    return card
