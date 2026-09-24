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
under `CHECKPOINTER=postgres`. Deleting the thread (the owner's
`DELETE /threads/{id}`, or the retention purge under fastapi) drops the tasks
of that conversation too.

Requests: a message needs the user role, text, no empty text part, and at
most `MAX_MESSAGE_CHARS` characters in all (the `/chat` limit); anything else
is a JSON-RPC invalid-params error (-32602) before a task is created. The
reply is one `response` artifact: `SendMessage` returns it as one text part;
`SendStreamingMessage` streams it in chunks (the last with `lastChunk`), and
the stored task keeps the chunks joined into one part.

The card's description (and its one skill's) is `A2A_DESCRIPTION`, its
version `AGENT_VERSION`, its name the mount name `A2A_NAME`.
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
    Message,
    Part,
    Role,
    SecurityScheme,
    Task,
)
from a2a.types.a2a_pb2 import ListTasksRequest, ListTasksResponse, SendMessageRequest
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH, PROTOCOL_VERSION_1_0
from a2a.utils.errors import InvalidParamsError
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
    LANGGRAPH_SERVER,
    RUNTIME,
    ChatRequest,
    detect_runtime,
)
from {{cookiecutter.agent_directory}}.app_utils.limits import SettingsError
from {{cookiecutter.agent_directory}}.app_utils.middleware import max_message_chars
from {{cookiecutter.agent_directory}}.app_utils.threads import DELETE_LISTENERS

logger = logging.getLogger(__name__)

# Mount name for the A2A endpoint: the agent directory, so it matches the
# `graph-agents-cli run --mode a2a` default.
A2A_NAME = os.environ.get("A2A_NAME") or "{{cookiecutter.agent_directory}}"
A2A_RPC_PATH = f"/a2a/{A2A_NAME}"
A2A_CARD_PATH = f"{A2A_RPC_PATH}{AGENT_CARD_WELL_KNOWN_PATH}"
DEFAULT_DESCRIPTION = "{{cookiecutter.project_name}}: a LangGraph agent served over the A2A protocol."
DEFAULT_SKILL_DESCRIPTION = "Hold a conversation with the agent."
# Set by the request handler for the executor: whether the caller streams the reply.
STREAMING_STATE_KEY = "a2a_streaming"


def card_description() -> str | None:
    """`A2A_DESCRIPTION`: what the agent card (and its chat skill) says the agent does."""
    return (os.environ.get("A2A_DESCRIPTION") or "").strip() or None


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


def context_key(context_id: str) -> str:
    """A contextId as the thread id it names (UUIDs canonical under langgraph-server)."""
    if detect_runtime() == LANGGRAPH_SERVER:
        try:
            return str(uuid.UUID(context_id))
        except ValueError:
            return context_id
    return context_id


def _plain_text(part: Part) -> bool:
    return (
        part.HasField("text")
        and not part.HasField("metadata")
        and not part.media_type
        and not part.filename
    )


def coalesce_text_parts(task: Task) -> Task:
    """`task` with each artifact's runs of plain text parts joined into one part.

    A streamed reply arrives as one part per chunk; a client reading the
    stored task (`GetTask`, `ListTasks`) gets the text in one piece. The text
    is unchanged. Returns `task` itself when nothing needs joining.
    """
    if not any(len(artifact.parts) > 1 for artifact in task.artifacts):
        return task
    joined = Task()
    joined.CopyFrom(task)
    for artifact in joined.artifacts:
        parts: list[Part] = []
        for part in artifact.parts:
            if parts and _plain_text(part) and _plain_text(parts[-1]):
                parts[-1].text += part.text
            else:
                copy = Part()
                copy.CopyFrom(part)
                parts.append(copy)
        del artifact.parts[:]
        artifact.parts.extend(parts)
    return joined


class ExpiringTaskStore(TaskStore):
    """The SDK's in-memory task store, scoped by `task_owner`, with TTL eviction.

    A task is evicted `ttl_s` seconds after its last save (`ttl_s <= 0`
    disables eviction); expired tasks are dropped on access and by a sweep
    that runs at most once a minute. Per process: replicas do not share it.
    Tasks are also indexed by conversation (`context_id`), so deleting a
    thread drops its tasks (`delete_context`), whoever created them.
    """

    def __init__(self, ttl_s: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl_s = ttl_s
        self._clock = clock
        self._inner = InMemoryTaskStore(owner_resolver=task_owner)
        self._saved_at: dict[str, dict[str, float]] = {}
        # context key -> {(owner, task id)}, and (owner, task id) -> context key
        self._by_context: dict[str, set[tuple[str, str]]] = {}
        self._context_of: dict[tuple[str, str], str] = {}
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
        key = self._context_of.pop((owner, task_id), None)
        if key is not None:
            members = self._by_context.get(key)
            if members is not None:
                members.discard((owner, task_id))
                if not members:
                    self._by_context.pop(key, None)

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
        await self._inner.save(coalesce_text_parts(task), context)
        self._saved_at.setdefault(owner, {})[task.id] = self._clock()
        if task.context_id and (owner, task.id) not in self._context_of:
            key = context_key(task.context_id)
            self._context_of[(owner, task.id)] = key
            self._by_context.setdefault(key, set()).add((owner, task.id))

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

    async def delete_context(self, thread_id: str) -> int:
        """Drop every task of the conversation `thread_id` (any owner); how many were dropped."""
        members = list(self._by_context.get(context_key(thread_id), ()))
        for owner, task_id in members:
            await self._inner.delete(task_id, self._context_for(owner))
            self._forget(owner, task_id)
        return len(members)


# The stores of the mounted A2A routes (one per app), for `forget_context`.
_STORES: list[ExpiringTaskStore] = []


async def forget_context(thread_id: str) -> None:
    """Drop the A2A tasks of a deleted thread (a `threads.DELETE_LISTENERS` entry)."""
    dropped = 0
    for store in list(_STORES):
        dropped += await store.delete_context(thread_id)
    if dropped:
        logger.info("dropped %d A2A task(s) of a deleted thread", dropped)


def message_problem(from_user: bool, texts: list[str]) -> str | None:
    """Why a message cannot start a run, or None.

    It needs the user role, at least one text part, no empty text part (the
    SDK cannot start a task from one), and at most `MAX_MESSAGE_CHARS`
    characters of text in all (joined as the executor joins them), the limit
    `/chat` applies.
    """
    if not from_user:
        return "The message must have the user role."
    if not texts:
        return "The message needs a text part."
    if any(not text for text in texts):
        return "The message has an empty text part."
    if any(_has_lone_surrogate(text) for text in texts):
        return "The message is not valid Unicode text (an unpaired surrogate)."
    cap = max_message_chars()
    if len("\n".join(texts)) > cap:
        return f"The message is longer than {cap} characters (MAX_MESSAGE_CHARS)."
    return None


def check_user_message(message: Message) -> None:
    """An invalid-params error (-32602) for a message the agent cannot run."""
    texts = [part.text for part in message.parts if part.HasField("text")]
    problem = message_problem(message.role == Role.ROLE_USER, texts)
    if problem:
        raise InvalidParamsError(problem)


def _has_lone_surrogate(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def _strings(value: Any) -> Any:
    """Every string in a parsed JSON value (keys included)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


# A2A 0.3 methods, served by the SDK's compatibility layer. That layer logs a
# request that fails its validation at ERROR with the offending values (the
# message text included), and answers any error raised while handling a
# request as an internal error (-32603, logged with a traceback). So a 0.3
# request is checked before it gets there (`fast_api_app.A2APolicyMiddleware`
# answers the error itself, naming fields, never values).
LEGACY_SEND_METHODS = ("message/send", "message/stream")
try:
    from a2a.compat.v0_3.jsonrpc_adapter import JSONRPC03Adapter

    LEGACY_METHOD_MODELS: dict[str, Any] = dict(JSONRPC03Adapter.METHOD_TO_MODEL)
except ImportError:  # an SDK without the 0.3 layer, or with it elsewhere
    LEGACY_METHOD_MODELS = {}
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_INVALID_PARAMS = -32602


def legacy_request_error(payload: Any) -> dict[str, Any] | None:
    """The JSON-RPC error (`code`, `message`) for an A2A 0.3 request that cannot run, or None.

    None for anything that is not a 0.3 request (1.0 requests are checked by
    the handler, a body that is not JSON by the SDK). A 0.3 request is
    validated with the SDK's own model for its method; the answer names the
    fields that failed and the rule, never the values sent. A string that is
    not valid Unicode (an unpaired surrogate) anywhere in it, and a 0.3
    message that cannot run (`legacy_message_problem`), are invalid params too.
    """
    if not isinstance(payload, dict):
        return None
    method = payload.get("method")
    if not isinstance(method, str) or method not in LEGACY_METHOD_MODELS:
        return None
    if any(_has_lone_surrogate(text) for text in _strings(payload)):
        return {
            "code": JSONRPC_INVALID_PARAMS,
            "message": "The request is not valid Unicode text (an unpaired surrogate).",
        }
    try:
        LEGACY_METHOD_MODELS[method].model_validate(payload)
    except Exception as exc:
        errors = exc.errors() if hasattr(exc, "errors") else []
        places = [".".join(str(p) for p in error.get("loc", ())) for error in errors]
        rules = [
            f"{place}: {error.get('msg')}" for place, error in zip(places, errors, strict=True)
        ]
        in_params = bool(places) and all(p == "params" or p.startswith("params.") for p in places)
        return {
            "code": JSONRPC_INVALID_PARAMS if in_params else JSONRPC_INVALID_REQUEST,
            "message": "Invalid A2A 0.3 request: " + ("; ".join(rules[:3]) or "malformed"),
        }
    problem = legacy_message_problem(payload)
    if problem is not None:
        return {"code": JSONRPC_INVALID_PARAMS, "message": problem}
    return None


def legacy_message_problem(payload: Any) -> str | None:
    """Why an A2A 0.3 `message/send` or `message/stream` request cannot run, or None.

    None as well for anything else (other methods, a malformed request:
    `legacy_request_error` answers those).
    """
    if not isinstance(payload, dict) or payload.get("method") not in LEGACY_SEND_METHODS:
        return None
    params = payload.get("params")
    message = params.get("message") if isinstance(params, dict) else None
    if not isinstance(message, dict) or not isinstance(message.get("parts"), list):
        return None
    texts = [
        part["text"]
        for part in message["parts"]
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]
    return message_problem(message.get("role") == "user", texts)


class PolicyRequestHandler(DefaultRequestHandler):
    """The SDK's request handler, checking each message before a task exists.

    It also records whether the reply is streamed, so the executor returns a
    non-streamed reply as one text part.
    """

    async def on_message_send(  # type: ignore[override]
        self, params: SendMessageRequest, context: ServerCallContext
    ) -> Any:
        check_user_message(params.message)
        context.state[STREAMING_STATE_KEY] = False
        return await super().on_message_send(params, context)

    async def on_message_send_stream(  # type: ignore[override]
        self, params: SendMessageRequest, context: ServerCallContext
    ) -> Any:
        check_user_message(params.message)
        context.state[STREAMING_STATE_KEY] = True
        async for event in super().on_message_send_stream(params, context):
            yield event


def _client_message(exc: Exception) -> str:
    """What an A2A client is told about a failure: a 4xx detail as is, anything else generic."""
    if isinstance(exc, HTTPException) and exc.status_code < 500:
        return str(exc.detail)
    error_id = uuid.uuid4().hex[:12]
    logger.error("A2A task failed (error id %s)", error_id, exc_info=exc)
    return f"The agent could not process this request (error id {error_id})."


class _Reply:
    """The `response` artifact of one task.

    Streamed, the text goes out in chunks with one chunk held back, so the
    last one can carry `last_chunk`; not streamed, it is sent once, whole, as
    one text part.
    """

    def __init__(self, updater: TaskUpdater, artifact_id: str, *, streaming: bool) -> None:
        self._updater = updater
        self._artifact_id = artifact_id
        self._streaming = streaming
        self._held: list[str] = []
        self._sent = False
        self._finished = False

    async def add(self, text: str) -> None:
        if self._streaming and self._held:
            await self._send("".join(self._held), last=False)
            self._held.clear()
        self._held.append(text)

    async def finish(self) -> None:
        """Send what is held as the last chunk (one empty part when the reply was empty)."""
        if self._finished:
            return
        self._finished = True
        if self._held or not self._sent:
            await self._send("".join(self._held), last=True)
            self._held.clear()

    async def _send(self, text: str, *, last: bool) -> None:
        await self._updater.add_artifact(
            [Part(text=text)],
            artifact_id=self._artifact_id,
            name="response",
            append=self._sent,
            last_chunk=last,
        )
        self._sent = True


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
        if not user_input or len(user_input) > max_message_chars():
            # The request handler refuses these first; this guards any other caller.
            await updater.failed(
                updater.new_agent_message([Part(text="The message is empty or too long.")])
            )
            return
        req = ChatRequest(message=user_input, thread_id=task.context_id or None)
        try:
            thread_id = await RUNTIME.resolve_thread(principal, req)
        except Exception as exc:  # ownership or server errors end the task
            await updater.failed(updater.new_agent_message([Part(text=_client_message(exc))]))
            return

        # The reply is the `response` artifact (A2A clients read it from
        # artifacts, not from the final status message).
        reply = _Reply(updater, uuid.uuid4().hex, streaming=bool(state.get(STREAMING_STATE_KEY)))
        async for event, data in RUNTIME.stream(principal, req, thread_id):
            if event == EVENT_DELTA and data.get("text"):
                await reply.add(data["text"])
            elif event == EVENT_ERROR:
                await reply.finish()
                await updater.failed(
                    updater.new_agent_message(
                        [Part(text=f"{data.get('code')}: {data.get('message')}")]
                    )
                )
                return
        await reply.finish()
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
    description = card_description()
    card = AgentCard(
        name=A2A_NAME,
        description=description or DEFAULT_DESCRIPTION,
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
                description=description or DEFAULT_SKILL_DESCRIPTION,
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
    store = ExpiringTaskStore(ttl)
    _STORES.append(store)
    if forget_context not in DELETE_LISTENERS:
        DELETE_LISTENERS.append(forget_context)
    request_handler = PolicyRequestHandler(
        agent_executor=LangGraphAgentExecutor(),
        task_store=store,
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
