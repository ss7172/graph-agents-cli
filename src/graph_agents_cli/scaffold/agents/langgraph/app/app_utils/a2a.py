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
principal's owner key (its id; for an agent calling for a user, the user's id
and the agent's, `Principal.owner_key`), so ListTasks, GetTask, CancelTask
and SubscribeToTask only ever see the caller's own tasks (another principal's
task id, or another agent's for the same user, reads as "not found"). The one
exception is the person themselves: a direct (non-delegated) caller also
reads (`GetTask`), lists (`ListTasks`) and cancels (`CancelTask`) the tasks
their agents started for them, the owner keys that begin with their id
(`SUBJECT_TASKS`). Continuing such a task (a message naming its `taskId`)
and subscribing to it stay with the agent that started it, and an agent never
sees another agent's tasks. Where the chat runtime has a Postgres database (`CHECKPOINTER=postgres`,
or a Postgres `DATABASE_URI` under langgraph-server) the tasks are kept there
(`PostgresTaskStore`): every replica sees them and they survive restarts and
rollouts, so `GetTask`, `ListTasks`, `CancelTask` and a message naming a
`taskId` (an approval decision) work on any replica. Otherwise
(`CHECKPOINTER=memory`) they are in process memory, per process. A task is
dropped `A2A_TASK_TTL_S` seconds (default 3600) after its last update; 0 keeps
it until its thread is deleted (in memory: until restart). A task whose run
ended with its process (a crash, an OOM kill) is failed rather than left
`working`. Live streams stay with the replica running the task: while its run
goes on, `SubscribeToTask` and `CancelTask` on another replica are refused
(-32004 and -32002: the caller may retry, or follow the task with `GetTask`). The
conversation itself is the thread, which is durable under
`CHECKPOINTER=postgres`. Deleting the thread (the owner's
`DELETE /threads/{id}`, or the retention purge under fastapi) drops the tasks
of that conversation too.

Requests: a message needs the user role, text, no empty text part, and at
most `MAX_MESSAGE_CHARS` characters in all (the `/chat` limit); anything else
is a JSON-RPC invalid-params error (-32602) before a task is created. A2A 0.3
requests get the same error codes as 1.0 (an unknown task is -32001, logged
at INFO; see `LegacyJsonRpcAdapter`). The reply is one `response` artifact:
`SendMessage` returns it as one text part; `SendStreamingMessage` streams it
in chunks (the last with `lastChunk`), and the stored task keeps the chunks
joined into one part.

Approvals: a run that pauses before a gated API call moves the task to
`input-required`; its status message holds a text part saying what waits
for approval (each call, its body as JSON up to 2,000 characters, and for a
relayed decision the call that will happen) and a data part
`{"type": "approval_request", "approval": {...}, "approvals": [...],
"approval_json": "..."}` (the approvals as `/chat`'s `message.end` has them;
a `Struct` holds numbers as doubles, so `approval_json` repeats them as exact
JSON text). The client answers with a message whose data part is
`{"approval_id": "...", "decision": "approve" | "reject", "comment": "..."}`
(no text needed; an agent relaying the person's decision adds the approval's
`digest`), on the same task or on its context alone (a new task, naming the
waiting one in `referenceTaskIds`): the decision goes through the same checks
as `POST /threads/{thread_id}/approvals/{approval_id}`, the auth policy's
`approval.decide` action included (the task's principal is the requester,
so this works when `requester` is an approver), and the resumed
run completes the task or pauses it again. A text message while an approval
is pending, or a decision refused (not an approver, expired, decided
already), leaves the task `input-required` with the pending approvals, or
fails it when none is pending any more. However an approval ends (a decision
over A2A or HTTP and the run it resumed, or its expiry), the requester's
`input-required` tasks on that thread that wait on it follow
(`follow_approval`, an `A2A_TASK_LISTENERS` entry): they take the run's
outcome and say where it continued. A failed task, and a refused decision,
carry a data part `{"type": "error", "code": ...}` (`thread_busy`: send the
message again).

The origin extension (`A2A_ORIGIN_EXTENSION`, declared in the card): an agent
calling for a user may send the user's own words in the message metadata under
its URI (`{"origin": {"text", "truncated", "hops"}}`; in a decision it relays,
`approving` too). For a delegated principal only, they go to the run's private
credentials (`@origin`), capped at `A2A_ORIGIN_MAX_CHARS`; more `hops` than
`AUTH_MAX_DELEGATION_DEPTH` fails the task. A run a decision resumes acts on
the words of the request that paused it, which its approval keeps until it is
decided (`approvals.resume_principal`), not on the words the decision carries.
`RuntimeTaskStore.save` takes them out of every message before a task is stored.

The card's description (and its one skill's) is `A2A_DESCRIPTION`, its
version `AGENT_VERSION`, its name the mount name `A2A_NAME`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from datetime import UTC, datetime
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
    AgentExtension,
    AgentInterface,
    AgentSkill,
    APIKeySecurityScheme,
    HTTPAuthSecurityScheme,
    Message,
    Part,
    Role,
    SecurityScheme,
    Task,
    TaskState,
)
from a2a.types.a2a_pb2 import (
    CancelTaskRequest,
    GetTaskRequest,
    ListTasksRequest,
    ListTasksResponse,
    SendMessageRequest,
    SubscribeToTaskRequest,
)
from a2a.utils.constants import (
    AGENT_CARD_WELL_KNOWN_PATH,
    DEFAULT_LIST_TASKS_PAGE_SIZE,
    PROTOCOL_VERSION_1_0,
)
from a2a.utils.errors import (
    JSON_RPC_ERROR_CODE_MAP,
    A2AError,
    InternalError,
    InvalidParamsError,
    TaskNotCancelableError,
    TaskNotFoundError,
    UnsupportedOperationError,
)
from a2a.utils.task import decode_page_token, encode_page_token
from fastapi import FastAPI, HTTPException
from google.protobuf import json_format, struct_pb2
from starlette.requests import Request
from starlette.responses import JSONResponse

from {{cookiecutter.agent_directory}}.app_utils import a2a_client
from {{cookiecutter.agent_directory}}.app_utils import chat as chat_runtime
from {{cookiecutter.agent_directory}}.app_utils.api_client import (
    A2A_ERROR_PART_TYPE,
    A2A_ORIGIN_EXTENSION,
    DEFAULT_A2A_ORIGIN_MAX_CHARS,
    ORIGIN_KEY,
    origin_max_chars,
)
from {{cookiecutter.agent_directory}}.app_utils.approvals import (
    CODE_APPROVAL_PENDING,
    COMMENT_MAX_CHARS,
    DECISIONS,
    DIGEST_MAX_CHARS,
)
from {{cookiecutter.agent_directory}}.app_utils.auth import (
    CUSTOM,
    JWT,
    OWNER_KEY_SEPARATOR,
    Principal,
    authorize_action,
    check_startup,
    delegation_settings,
    policy_name,
    with_origin,
)
from {{cookiecutter.agent_directory}}.app_utils.chat import (
    EVENT_DELTA,
    EVENT_END,
    EVENT_ERROR,
    LANGGRAPH_SERVER,
    RUNTIME,
    STATUS_AWAITING_APPROVAL,
    ApprovalError,
    ChatRequest,
    detect_runtime,
    new_error_id,
    unavailable,
)
from {{cookiecutter.agent_directory}}.app_utils.content import valid_text
from {{cookiecutter.agent_directory}}.app_utils.db import (
    Database,
    StorageNotReady,
    is_database_unavailable,
)
from {{cookiecutter.agent_directory}}.app_utils.limits import SettingsError
from {{cookiecutter.agent_directory}}.app_utils.middleware import max_message_chars
from {{cookiecutter.agent_directory}}.app_utils.threads import (
    A2A_TASK_LISTENERS,
    DELETE_LISTENERS,
    OUTCOME_COMPLETED,
    OUTCOME_FAILED,
    OUTCOME_INPUT_REQUIRED,
    THREAD_BUSY,
    ApprovalOutcome,
    ThreadBusy,
)

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
# Whether this agent's A2A client passes the user's own words on to the agents it calls
# (`A2A_FORWARD_ORIGIN`, `a2a_client.py`): `auto` sends them only to a peer whose card
# declares the origin extension, and `off` never does.
DEFAULT_A2A_FORWARD_ORIGIN = a2a_client.DEFAULT_A2A_FORWARD_ORIGIN
# The card's description of the origin extension (`api_client.A2A_ORIGIN_EXTENSION`).
ORIGIN_EXTENSION_DESCRIPTION = (
    "graph-agents-cli origin: an agent calling for a user forwards, in the message metadata "
    "under this URI, the user's own words (origin: text, truncated, hops), and in a decision "
    "it relays the approval it is about (approving). Never stored with the task."
)


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
    """`A2A_TASK_TTL_S`: seconds a task is kept after its last update; 0 = no expiry.

    Without expiry a task is kept until its thread is deleted (in memory: until restart).

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


# The A2A operations through which a person (a direct caller) reaches the tasks their
# agents started for them, besides their own (the owner decision of 2026-09-28).
SUBJECT_TASKS = ("GetTask", "ListTasks", "CancelTask")


class PrincipalUser(User):
    """The authenticated principal as an A2A user: its owner key is the task owner.

    The key is the principal's id for a direct caller, and its subject and
    actor for an agent calling for a user (`Principal.owner_key`): an agent's
    tasks are its own, and another agent acting for the same user never sees
    them. Direct callers' keys are their ids, as before 0.3.

    `subject` is set for a direct caller only (its id): the person, who also
    reads, lists and cancels the tasks their agents started for them
    (`SUBJECT_TASKS`, the owner keys `subject_owns`).
    """

    def __init__(self, principal_id: str, *, subject: str | None = None) -> None:
        self._principal_id = principal_id
        self.subject = subject

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def user_name(self) -> str:
        return self._principal_id


def subject_owns(subject: str, owner: str) -> bool:
    """Whether the task owner key `owner` is `subject`'s own, or one of its agents' for it.

    An agent's key is the subject, the separator (which no id holds) and the
    agent: so a subject never matches another subject's keys, whatever they are.
    """
    return owner == subject or owner.startswith(f"{subject}{OWNER_KEY_SEPARATOR}")


def task_subject(context: ServerCallContext) -> str | None:
    """The person a call context reaches their agents' tasks for, or None (an agent's call)."""
    user = context.user
    if not isinstance(user, PrincipalUser) or not user.subject:
        return None
    return user.subject


def owner_context(context: ServerCallContext, owner: str) -> ServerCallContext:
    """`context` acting on the tasks of the owner key `owner` (the call's state is shared).

    What a person's GetTask and CancelTask run under for a task their agent
    started: every read and write of the store is then that task's own, so a
    canceled task is saved where it is, never copied under the person's key.
    """
    return ServerCallContext(
        state=context.state,
        user=PrincipalUser(owner),
        tenant=context.tenant,
        requested_extensions=context.requested_extensions,
    )


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
        return PrincipalUser(
            principal.owner_key(), subject=None if principal.delegated else principal.id
        )


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
        """The caller's tasks; for a person (a direct caller), their agents' for them too."""
        owner = task_owner(context)
        await self._maybe_sweep()
        subject = task_subject(context)
        if subject is None:
            await self._evict_expired(owner)
            return await self._inner.list(params, context)
        # Every owner key of the subject's, filtered as the SDK's store filters one owner's
        # tasks, then ordered and paged together as it orders and pages them.
        every = ListTasksRequest()
        every.CopyFrom(params)
        every.ClearField("page_token")
        every.page_size = _EVERY_TASK
        tasks: list[Task] = []
        for key in [key for key in list(self._saved_at) if subject_owns(subject, key)]:
            await self._evict_expired(key)
            tasks.extend((await self._inner.list(every, self._context_for(key))).tasks)
        return _page(tasks, params)

    async def owner_for_subject(self, task_id: str, subject: str) -> str | None:
        """The owner key of `task_id` among `subject`'s own and its agents' keys, or None."""
        await self._maybe_sweep()
        keys = sorted(
            (key for key in list(self._saved_at) if subject_owns(subject, key)),
            key=lambda key: key != subject,  # the person's own task first
        )
        for key in keys:
            await self._evict_expired(key)
            if task_id in self._saved_at.get(key, {}):
                return key
        return None

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

    async def follow(self, outcome: ApprovalOutcome) -> int:
        """The conversation's tasks waiting on an approval take its outcome (`follows`)."""
        count = 0
        for owner, task_id in list(self._by_context.get(context_key(outcome.thread_id), ())):
            context = self._context_for(owner)
            task = await self.get(task_id, context)
            if task is not None and follows(task, owner, outcome):
                await self.save(followed(task, outcome), context)
                count += 1
        return count


# A page size no store call reaches: every task of an owner, for `_page` to page them.
_EVERY_TASK = 2**31 - 1


def _task_order(task: Task) -> tuple[bool, str, str]:
    """The SDK in-memory store's ListTasks order key (sorted in reverse: newest status first)."""
    stamped = task.HasField("status") and task.status.HasField("timestamp")
    return (stamped, task.status.timestamp.ToJsonString() if stamped else "", task.id)


def _page(tasks: list[Task], params: ListTasksRequest) -> ListTasksResponse:
    """Filtered `tasks` ordered and paged as the SDK's in-memory store pages one owner's."""
    tasks = sorted(tasks, key=_task_order, reverse=True)
    start = 0
    if params.page_token:
        start_id = decode_page_token(params.page_token)
        for index, task in enumerate(tasks):
            if task.id == start_id:
                start = index
                break
        else:
            raise InvalidParamsError(f"Invalid page token: {params.page_token}")
    page_size = params.page_size or DEFAULT_LIST_TASKS_PAGE_SIZE
    end = start + page_size
    return ListTasksResponse(
        next_page_token=encode_page_token(tasks[end].id) if end < len(tasks) else None,
        tasks=tasks[start:end],
        total_size=len(tasks),
        page_size=page_size,
    )


# Task states whose run is still going (or about to start). A task left in one of
# them by a process that died is failed by the Postgres store's sweep once its
# thread's run lease has expired (`chat.RECONCILE_GRACE_S` after its last save).
RUNNING_STATES = frozenset({TaskState.TASK_STATE_SUBMITTED, TaskState.TASK_STATE_WORKING})
# The Postgres store's sweep (expired tasks, tasks of dead runs): at most this often
# per process, on access.
TASK_SWEEP_INTERVAL_S = 60.0
# A save that changes only the reply (a streamed chunk: the status is the same) is
# written at most this often per task; any status change is written at once, whole.
CHUNK_SAVE_INTERVAL_S = 1.0
_CHUNK_TRACKING_CAP = 10_000
_ORPHANS_PER_SWEEP = 200
INTERRUPTED_TASK_TEXT = (
    "This task stopped: the agent process running it ended before it finished. "
    "Send the message again."
)


def _status_at(task: Task) -> datetime | None:
    if not task.status.HasField("timestamp"):
        return None
    return task.status.timestamp.ToDatetime(tzinfo=UTC)


# Postgres stores no U+0000, in a TEXT column or in jsonb (escaped or not). A
# task can hold one wherever text comes from outside: a user's message, a
# contextId, a tool's output the reply repeats. Such a string is stored with
# U+FFFD in its place; the save must not fail, since the run (and whatever
# its tools did) goes on regardless.
_NUL, _NUL_STORED = "\x00", "\ufffd"


def _text(value: str) -> str:
    """`value` as a TEXT column holds it (a U+0000 becomes U+FFFD)."""
    return value.replace(_NUL, _NUL_STORED) if _NUL in value else value


def _storable(value: Any) -> Any:
    """A parsed JSON value with `_text` applied to every string in it, keys included."""
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, dict):
        return {_text(key): _storable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_storable(item) for item in value]
    return value


def _task_json(task: Task) -> str:
    data = json_format.MessageToDict(task)
    stored = json.dumps(data)
    # json.dumps writes a U+0000 as the escape \u0000, so a task without one is
    # written as is; an escaped backslash followed by "u0000" only costs a walk.
    if "\\u0000" in stored:
        stored = json.dumps(_storable(data))
    return stored


def _task_from(value: Any) -> Task:
    task = Task()
    data = json.loads(value) if isinstance(value, str | bytes) else value
    # A newer SDK on another replica (a rolling upgrade) may write fields this one lacks.
    json_format.ParseDict(data, task, ignore_unknown_fields=True)
    return task


class PostgresTaskStore(TaskStore):
    """A2A tasks in the app's Postgres database, shared by every replica, kept across restarts.

    Table `a2a_tasks` (`agent_a2a_tasks` under langgraph-server), created with
    the app tables (`db.py`). The same contract as `ExpiringTaskStore`: tasks
    are scoped by `task_owner` (the table's key is the owner and the task id,
    so another principal's task reads as not found), a task is invisible
    `ttl_s` seconds after its last save and deleted by a sweep (`ttl_s <= 0`:
    kept until its thread is deleted), and deleting a thread deletes its tasks
    (`delete_context`), whoever created them, for every replica. The TTL runs
    on the database clock, so replicas agree on it.

    The sweep also ends tasks whose run died with its process (a crash, an OOM
    kill, a lost node): a task still `submitted` or `working` whose thread has
    had no live run lease since `chat.RECONCILE_GRACE_S` after its last save is
    failed with a message saying so, instead of looking busy until it expires.

    A U+0000, which Postgres cannot store, is stored (and read back) as U+FFFD,
    in the task and in the ids a request names (`_text`, `_storable`); the
    owner is used as it is, so two principals never share a key.

    Database failures surface as the app's 503 (`unavailable`, logged with an
    error id), never as the database's own message, which the JSON-RPC layer
    would otherwise pass to the caller.
    """

    def __init__(
        self, ttl_s: float, db: Database, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.ttl_s = ttl_s
        self.db = db
        self.table = db.a2a_tasks_table
        self.locks_table = db.locks_table
        self._clock = clock
        self._next_sweep = 0.0
        # (owner, task id) -> (status as saved, when): the last write of a running task.
        self._written: OrderedDict[tuple[str, str], tuple[bytes, float]] = OrderedDict()

    def _visible(self) -> tuple[str, tuple[float, float]]:
        """The SQL condition (and its parameters) of a task that has not expired."""
        ttl = float(self.ttl_s)
        return "(%s <= 0 OR updated_at > now() - make_interval(secs => %s))", (ttl, ttl)

    async def _db(self, call: Awaitable[Any]) -> Any:
        """Run a database call; an unreachable database is a 503, anything else an internal error."""
        try:
            return await call
        except (A2AError, HTTPException):
            raise
        except Exception as exc:
            if is_database_unavailable(exc):
                raise unavailable("Database", exc) from exc
            error_id = new_error_id()
            logger.error("A2A task store failed (error_id=%s)", error_id, exc_info=exc)
            raise InternalError(message=f"The task store failed. Reference: {error_id}.") from None

    async def save(self, task: Task, context: ServerCallContext) -> None:
        owner = task_owner(context)
        await self._maybe_sweep()
        task = coalesce_text_parts(task)
        key = (owner, task.id)
        status = task.status.SerializeToString(deterministic=True)
        now = self._clock()
        last = self._written.get(key)
        if last is not None and last[0] == status and now - last[1] < CHUNK_SAVE_INTERVAL_S:
            # A reply chunk: the next save of this task (its end, at the latest) writes it.
            return
        await self._db(
            self.db.execute(
                f"""
                INSERT INTO {self.table} (owner, task_id, context_id, thread_id, state,
                    status_at, task, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, now())
                ON CONFLICT (owner, task_id) DO UPDATE SET
                    context_id = EXCLUDED.context_id, thread_id = EXCLUDED.thread_id,
                    state = EXCLUDED.state, status_at = EXCLUDED.status_at,
                    task = EXCLUDED.task, updated_at = now()
                """,
                (
                    owner,
                    _text(task.id),
                    _text(task.context_id),
                    _text(context_key(task.context_id)) if task.context_id else "",
                    TaskState.Name(task.status.state),
                    _status_at(task),
                    _task_json(task),
                ),
            )
        )
        if task.status.state in RUNNING_STATES:
            self._written[key] = (status, now)
            self._written.move_to_end(key)
            while len(self._written) > _CHUNK_TRACKING_CAP:
                self._written.popitem(last=False)
        else:
            self._written.pop(key, None)

    async def get(self, task_id: str, context: ServerCallContext) -> Task | None:
        owner = task_owner(context)
        await self._maybe_sweep()
        visible, ttl = self._visible()
        row = await self._db(
            self.db.fetchone(
                f"SELECT task FROM {self.table} WHERE owner = %s AND task_id = %s AND {visible}",
                (owner, _text(task_id), *ttl),
            )
        )
        return _task_from(row["task"]) if row else None

    @staticmethod
    def _owned(owner: str, subject: str | None) -> tuple[str, list[Any]]:
        """The SQL condition (and its parameters) of the tasks a caller lists: its owner key's,
        and for a person (`subject`) also their agents' (the keys that begin with the subject
        and the separator; compared exactly, never with LIKE or a collation's range)."""
        if subject is None:
            return "owner = %s", [owner]
        prefix = f"{subject}{OWNER_KEY_SEPARATOR}"
        return "(owner = %s OR left(owner, %s) = %s)", [subject, len(prefix), prefix]

    async def list(self, params: ListTasksRequest, context: ServerCallContext) -> ListTasksResponse:
        """The owner's tasks, newest status first, as the SDK's stores order and page them.

        For a person (a direct caller), their agents' tasks for them too.
        """
        owner = task_owner(context)
        await self._maybe_sweep()
        visible, ttl = self._visible()
        owned, owned_args = self._owned(owner, task_subject(context))
        where = [owned, visible]
        args: list[Any] = [*owned_args, *ttl]
        if params.context_id:
            where.append("context_id = %s")
            args.append(_text(params.context_id))
        if params.status:
            where.append("state = %s")
            args.append(TaskState.Name(params.status))
        if params.HasField("status_timestamp_after"):
            where.append("status_at >= %s")
            args.append(params.status_timestamp_after.ToDatetime(tzinfo=UTC))
        condition = " AND ".join(where)
        counted = await self._db(
            self.db.fetchone(f"SELECT count(*) AS n FROM {self.table} WHERE {condition}", args)
        )
        total = int(counted["n"]) if counted else 0
        page_where, page_args = list(where), list(args)
        if params.page_token:
            start_id = _text(decode_page_token(params.page_token))
            start = await self._db(
                self.db.fetchone(
                    f"SELECT status_at FROM {self.table} "
                    f"WHERE {owned} AND task_id = %s AND {visible}",
                    (*owned_args, start_id, *ttl),
                )
            )
            if start is None:
                raise InvalidParamsError(f"Invalid page token: {params.page_token}")
            if start["status_at"] is not None:
                page_where.append(
                    "((status_at = %s AND task_id <= %s) OR status_at < %s OR status_at IS NULL)"
                )
                page_args += [start["status_at"], start_id, start["status_at"]]
            else:
                page_where.append("(status_at IS NULL AND task_id <= %s)")
                page_args.append(start_id)
        page_size = params.page_size or DEFAULT_LIST_TASKS_PAGE_SIZE
        rows = await self._db(
            self.db.fetchall(
                f"SELECT task_id, task FROM {self.table} WHERE {' AND '.join(page_where)} "
                "ORDER BY (status_at IS NULL), status_at DESC, task_id DESC LIMIT %s",
                [*page_args, page_size + 1],
            )
        )
        next_token = (
            encode_page_token(rows[page_size]["task_id"]) if len(rows) > page_size else None
        )
        return ListTasksResponse(
            tasks=[_task_from(row["task"]) for row in rows[:page_size]],
            total_size=total,
            next_page_token=next_token,
            page_size=page_size,
        )

    async def owner_for_subject(self, task_id: str, subject: str) -> str | None:
        """The owner key of `task_id` among `subject`'s own and its agents' keys, or None."""
        await self._maybe_sweep()
        visible, ttl = self._visible()
        owned, owned_args = self._owned(subject, subject)
        row = await self._db(
            self.db.fetchone(
                f"SELECT owner FROM {self.table} WHERE task_id = %s AND {owned} AND {visible} "
                "ORDER BY (owner = %s) DESC LIMIT 1",
                (_text(task_id), *owned_args, *ttl, subject),
            )
        )
        return str(row["owner"]) if row else None

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        owner = task_owner(context)
        self._written.pop((owner, task_id), None)
        await self._db(
            self.db.execute(
                f"DELETE FROM {self.table} WHERE owner = %s AND task_id = %s",
                (owner, _text(task_id)),
            )
        )

    async def delete_context(self, thread_id: str) -> int:
        """Drop every task of the conversation `thread_id` (any owner); how many were dropped."""
        rows = await self.db.fetchall(
            f"DELETE FROM {self.table} WHERE thread_id = %s RETURNING owner, task_id",
            (_text(context_key(thread_id)),),
        )
        for row in rows:
            self._written.pop((row["owner"], row["task_id"]), None)
        return len(rows)

    async def follow(self, outcome: ApprovalOutcome) -> int:
        """The conversation's tasks waiting on an approval take its outcome (`follows`).

        The candidates are the conversation's `input-required` tasks that the
        decision named or whose approval request lists the approval (a jsonb
        containment); each is rewritten only while it is still waiting, under
        its own owner key.
        """
        visible, ttl = self._visible()
        listed = json.dumps(
            {
                "status": {
                    "message": {
                        "parts": [{"data": {"approvals": [{"approval_id": outcome.approval_id}]}}]
                    }
                }
            }
        )
        rows = await self.db.fetchall(
            f"""
            SELECT owner, task_id, task FROM {self.table}
             WHERE thread_id = %s AND state = 'TASK_STATE_INPUT_REQUIRED' AND {visible}
               AND (task_id = ANY(%s) OR task @> %s::jsonb)
            """,
            (
                _text(context_key(outcome.thread_id)),
                *ttl,
                [_text(t) for t in outcome.references],
                listed,
            ),
        )
        count = 0
        for row in rows:
            task = _task_from(row["task"])
            if not follows(task, str(row["owner"]), outcome):
                continue
            new = followed(task, outcome)
            updated = await self.db.fetchone(
                f"""
                UPDATE {self.table}
                   SET state = %s, status_at = %s, task = %s::jsonb, updated_at = now()
                 WHERE owner = %s AND task_id = %s AND state = 'TASK_STATE_INPUT_REQUIRED'
                RETURNING task_id
                """,
                (
                    TaskState.Name(new.status.state),
                    _status_at(new),
                    _task_json(new),
                    row["owner"],
                    row["task_id"],
                ),
            )
            count += updated is not None
        return count

    async def running_elsewhere(self, task: Task) -> bool:
        """Whether `task`'s run is going on in another process (its thread's live lease is not ours)."""
        if task.status.state not in RUNNING_STATES or not task.context_id:
            return False
        thread_id = context_key(task.context_id)
        if RUNTIME.locks.lease(thread_id) is not None:
            return False
        row = await self._db(
            self.db.fetchone(
                f"SELECT 1 AS held FROM {self.locks_table} "
                "WHERE thread_id = %s AND expires_at > now()",
                (_text(thread_id),),
            )
        )
        return row is not None

    async def _maybe_sweep(self) -> None:
        now = self._clock()
        if now < self._next_sweep:
            return
        self._next_sweep = now + TASK_SWEEP_INTERVAL_S
        try:
            await self.sweep()
        except Exception as exc:  # the request itself answers for an unreachable database
            logger.warning("A2A task sweep failed (%s); retrying later", type(exc).__name__)

    async def sweep(self) -> tuple[int, int]:
        """Delete expired tasks and fail the tasks of dead runs; (deleted, failed)."""
        deleted = 0
        if self.ttl_s > 0:
            rows = await self.db.fetchall(
                f"DELETE FROM {self.table} "
                "WHERE updated_at <= now() - make_interval(secs => %s) RETURNING task_id",
                (float(self.ttl_s),),
            )
            deleted = len(rows)
        orphans = await self.db.fetchall(
            f"""
            SELECT t.owner, t.task_id, t.task, t.updated_at FROM {self.table} AS t
             WHERE t.state IN ('TASK_STATE_SUBMITTED', 'TASK_STATE_WORKING')
               AND t.updated_at < now() - make_interval(secs => %s)
               AND NOT EXISTS (
                   SELECT 1 FROM {self.locks_table} AS l
                    WHERE l.thread_id = t.thread_id AND l.expires_at > now())
             ORDER BY t.updated_at LIMIT %s
            """,
            (float(chat_runtime.RECONCILE_GRACE_S), _ORPHANS_PER_SWEEP),
        )
        failed = 0
        for row in orphans:
            task = _task_from(row["task"])
            if task.status.HasField("message"):
                task.history.append(task.status.message)
            task.status.state = TaskState.TASK_STATE_FAILED
            task.status.message.CopyFrom(
                Message(
                    message_id=uuid.uuid4().hex,
                    role=Role.ROLE_AGENT,
                    task_id=task.id,
                    context_id=task.context_id,
                    parts=[Part(text=INTERRUPTED_TASK_TEXT)],
                )
            )
            task.status.timestamp.GetCurrentTime()
            # Only if nothing saved the task since it was read (its run came back to it).
            updated = await self.db.fetchone(
                f"""
                UPDATE {self.table}
                   SET state = %s, status_at = %s, task = %s::jsonb, updated_at = now()
                 WHERE owner = %s AND task_id = %s AND updated_at = %s
                RETURNING task_id
                """,
                (
                    TaskState.Name(task.status.state),
                    _status_at(task),
                    _task_json(task),
                    row["owner"],
                    row["task_id"],
                    row["updated_at"],
                ),
            )
            failed += updated is not None
        if failed:
            logger.info("failed %d A2A task(s) whose run ended with its process", failed)
        return deleted, failed


class RuntimeTaskStore(TaskStore):
    """The app's A2A task store: Postgres when the chat runtime's database is Postgres.

    That is `CHECKPOINTER=postgres` (fastapi) or a Postgres `DATABASE_URI`
    (langgraph-server), where run records and approvals live too: the tasks are
    then shared by every replica and survive restarts (`PostgresTaskStore`).
    Otherwise they are in process memory (`ExpiringTaskStore`). The choice is
    made on each call, since the routes are mounted before the runtime starts;
    while the database is not set up yet, calls answer 503.
    """

    def __init__(self, ttl_s: float) -> None:
        self.ttl_s = ttl_s
        self.memory = ExpiringTaskStore(ttl_s)
        self._postgres: PostgresTaskStore | None = None

    def postgres(self) -> PostgresTaskStore | None:
        """The Postgres store of the running runtime's database, or None (memory)."""
        db = RUNTIME.db
        if db is None or not db.is_postgres:
            return None
        if not RUNTIME.started or RUNTIME.initialising:
            raise unavailable("Database", StorageNotReady("the database is not set up yet"))
        if self._postgres is None or self._postgres.db is not db:
            self._postgres = PostgresTaskStore(self.ttl_s, db)
        return self._postgres

    def _store(self) -> TaskStore:
        return self.postgres() or self.memory

    async def save(self, task: Task, context: ServerCallContext) -> None:
        # The one place every task write passes: the words a calling agent forwarded (the
        # origin extension's metadata) are never stored.
        await self._store().save(without_extension(task), context)

    async def get(self, task_id: str, context: ServerCallContext) -> Task | None:
        return await self._store().get(task_id, context)

    async def follow(self, outcome: ApprovalOutcome) -> int:
        """The tasks waiting on an approval follow its outcome, in both stores."""
        count = await self.memory.follow(outcome)
        store = self.postgres()
        if store is not None:
            count += await store.follow(outcome)
        return count

    async def list(self, params: ListTasksRequest, context: ServerCallContext) -> ListTasksResponse:
        return await self._store().list(params, context)

    async def owner_for_subject(self, task_id: str, subject: str) -> str | None:
        return await self._store().owner_for_subject(task_id, subject)

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        await self._store().delete(task_id, context)

    async def delete_context(self, thread_id: str) -> int:
        dropped = await self.memory.delete_context(thread_id)
        store = self.postgres()
        if store is not None:
            dropped += await store.delete_context(thread_id)
        return dropped

    async def running_elsewhere(self, task: Task) -> bool:
        store = self.postgres()
        return store is not None and await store.running_elsewhere(task)


# The stores of the mounted A2A routes (one per app), for `forget_context`.
_STORES: list[RuntimeTaskStore] = []


async def forget_context(thread_id: str) -> None:
    """Drop the A2A tasks of a deleted thread (a `threads.DELETE_LISTENERS` entry)."""
    dropped = 0
    for store in list(_STORES):
        dropped += await store.delete_context(thread_id)
    if dropped:
        logger.info("dropped %d A2A task(s) of a deleted thread", dropped)


APPROVAL_REQUEST_TYPE = "approval_request"
_APPROVAL_ID_MAX_CHARS = 64
# How much of an approval's body the text of an approval request shows.
APPROVAL_TEXT_BODY_MAX_CHARS = 2000


# ---------------------------------------------------------------------------
# The origin extension: the user's own words, forwarded by the agent calling for them
# ---------------------------------------------------------------------------


def _extension_data(message: Message | None) -> dict[str, Any] | None:
    """What a message's metadata holds under the origin extension's URI, or None."""
    if message is None or not message.HasField("metadata"):
        return None
    try:
        metadata = json_format.MessageToDict(message.metadata)
    except Exception:
        return None
    data = metadata.get(A2A_ORIGIN_EXTENSION)
    return data if isinstance(data, dict) else None


def read_origin(message: Message | None, principal: Principal) -> tuple[dict[str, Any] | None, int]:
    """The origin a calling agent forwarded (`{text, truncated, hops}`), and its `hops`.

    Read for a delegated principal only (a person's own message is their own
    words), from `message.metadata[A2A_ORIGIN_EXTENSION]["origin"]`. The text is
    kept at most `A2A_ORIGIN_MAX_CHARS` long (`truncated` then true); a value of
    another shape is ignored (no origin: `require_user_mentioned` refuses).
    `hops` counts the agents the words came through (0 without an origin): the
    caller fails the task past `AUTH_MAX_DELEGATION_DEPTH`.
    """
    if not principal.delegated:
        return None, 0
    data = _extension_data(message)
    origin = data.get(ORIGIN_KEY) if data is not None else None
    if not isinstance(origin, dict) or not isinstance(origin.get("text"), str):
        return None, 0
    hops = origin.get("hops")
    hops = int(hops) if isinstance(hops, int | float) and not isinstance(hops, bool) else 1
    try:
        cap = origin_max_chars()
    except SettingsError:  # the startup check refuses it; the default bound holds meanwhile
        cap = DEFAULT_A2A_ORIGIN_MAX_CHARS
    text = origin["text"]
    truncated = origin.get("truncated") is True or len(text) > cap
    if _has_lone_surrogate(text):
        return None, max(hops, 1)
    return {"text": text[:cap], "truncated": truncated, "hops": max(hops, 1)}, max(hops, 1)


def _max_depth() -> int:
    try:
        return delegation_settings().max_depth
    except SettingsError:  # the startup check refuses it; fail closed
        return 1


def without_extension(task: Task) -> Task:
    """`task` with the origin extension's metadata taken out of every message it holds.

    The user's words a calling agent forwarded (and the `approving` copy of a
    relayed decision) are never stored: `RuntimeTaskStore.save` passes every
    task through this. Returns `task` itself when no message holds any.
    """
    messages = [task.status.message] if task.status.HasField("message") else []
    messages.extend(task.history)
    if not any(
        m.HasField("metadata") and A2A_ORIGIN_EXTENSION in m.metadata.fields for m in messages
    ):
        return task
    stripped = Task()
    stripped.CopyFrom(task)
    held = [stripped.status.message] if stripped.status.HasField("message") else []
    for message in [*held, *stripped.history]:
        if message.HasField("metadata") and A2A_ORIGIN_EXTENSION in message.metadata.fields:
            del message.metadata[A2A_ORIGIN_EXTENSION]
            if not message.metadata.fields:
                message.ClearField("metadata")
    return stripped


# ---------------------------------------------------------------------------
# Status messages: what waits for approval, and why a task failed
# ---------------------------------------------------------------------------

ERROR_CODE_UNAUTHENTICATED = "unauthenticated"
ERROR_CODE_INVALID_CONTEXT = "invalid_context_id"
ERROR_CODE_INVALID_MESSAGE = "invalid_message"
ERROR_CODE_TOO_DEEP = "delegation_too_deep"
ERROR_CODE_FORBIDDEN = "forbidden"
ERROR_CODE_FAILED = "run_failed"


def error_part(code: str) -> Part:
    """The data part a failed (or refused) task carries: `{"type": "error", "code": ...}`.

    A client branches on the code (`thread_busy`: send the message again), never
    on the text.
    """
    data = struct_pb2.Value()
    json_format.ParseDict({"type": A2A_ERROR_PART_TYPE, "code": code}, data)
    return Part(data=data)


def _json_text(value: Any, limit: int) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _effect_text(effect: Any) -> str | None:
    """`orders (via billing) will POST /orders/7/cancel (cancelOrder), as reported by orders`."""
    if not isinstance(effect, dict):
        return None
    via = [str(v) for v in effect.get("via") or [] if v]
    hops = via[:-1] if via and via[-1] == effect.get("agent") else via
    who = str(effect.get("agent") or "the agent") + (f" (via {', '.join(hops)})" if hops else "")
    what = f"{effect.get('method')} {effect.get('path')}"
    if effect.get("operation_id"):
        what += f" ({effect['operation_id']})"
    reported = f", as reported by {via[-1]}" if via else ""
    return f"{who} will {what}{reported}"


def approval_parts(
    approvals: list[Any], note: str | None = None, code: str | None = None
) -> list[Part]:
    """The parts of an input-required status message: what waits for approval, as text and data.

    The data part is `{"type": "approval_request", "approval", "approvals",
    "approval_json"}`: the approvals as `/chat`'s `message.end` has them, and
    `approval_json`, the same list as exact JSON text (a `Struct` holds every
    number as a double: `1` reads `1.0`, and large integers round), which an
    agent relaying the decision reads. The text names each call, its body
    (at most `APPROVAL_TEXT_BODY_MAX_CHARS` characters of JSON) and, for a
    relayed decision, the call that will happen (`effect`). `code`: why a
    decision sent was refused (an error part too).
    """
    approvals = [a for a in approvals if isinstance(a, dict)]
    lines = [note] if note else []
    for approval in approvals:
        what = f"{approval.get('method')} {approval.get('path')}"
        reason = approval.get("reason")
        lines.append(
            f"Waiting for approval {approval.get('approval_id')}: {what}"
            + (f" ({reason})" if reason else "")
            + f", until {approval.get('expires_at')}."
        )
        if approval.get("body") is not None:
            lines.append(f"  body: {_json_text(approval['body'], APPROVAL_TEXT_BODY_MAX_CHARS)}")
        effect = _effect_text(approval.get("effect"))
        if effect:
            lines.append(f"  effect: {effect}.")
    lines.append(
        'Answer with a data part {"approval_id": "...", "decision": "approve"} '
        '(or "reject"; an optional "comment"; an agent relaying the person\'s decision adds '
        'the approval\'s "digest").'
    )
    data = struct_pb2.Value()
    json_format.ParseDict(
        {
            "type": APPROVAL_REQUEST_TYPE,
            "approval": approvals[0] if approvals else None,
            "approvals": approvals,
            "approval_json": json.dumps(approvals, ensure_ascii=False, default=str),
        },
        data,
    )
    parts = [Part(text=valid_text("\n".join(lines))), Part(data=data)]
    if code:
        parts.append(error_part(code))
    return parts


def waits_on(task: Task, approval_id: str) -> bool:
    """Whether a task's status message lists `approval_id` in its approval request."""
    if not task.status.HasField("message"):
        return False
    for part in task.status.message.parts:
        data = _part_data(part)
        if isinstance(data, dict) and data.get("type") == APPROVAL_REQUEST_TYPE:
            for approval in data.get("approvals") or []:
                if isinstance(approval, dict) and approval.get("approval_id") == approval_id:
                    return True
    return False


def owner_is_requester(owner: str, requester_hash: str, requester_actor: str) -> bool:
    """Whether the task owner key `owner` is an approval's requester (its hashed subject and
    its actor, "" for a direct one): the approval's record keeps the subject hashed only."""
    subject, _, actor = owner.partition(OWNER_KEY_SEPARATOR)
    return actor == (requester_actor or "") and Principal(id=subject).hashed_id() == (
        requester_hash
    )


_OUTCOME_STATES = {
    OUTCOME_COMPLETED: TaskState.TASK_STATE_COMPLETED,
    OUTCOME_FAILED: TaskState.TASK_STATE_FAILED,
    OUTCOME_INPUT_REQUIRED: TaskState.TASK_STATE_INPUT_REQUIRED,
}


def follows(task: Task, owner: str, outcome: ApprovalOutcome) -> bool:
    """Whether a stored task takes an approval's outcome (`ApprovalOutcome`).

    It is `input-required`, it is not the task that carried the decision, it
    belongs to the approval's requester, and the decision named it
    (`referenceTaskIds`) or its approval request lists the approval.
    """
    return (
        task.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
        and task.id != outcome.continued_in
        and owner_is_requester(owner, outcome.requester_hash, outcome.requester_actor)
        and (task.id in outcome.references or waits_on(task, outcome.approval_id))
    )


def followed(task: Task, outcome: ApprovalOutcome) -> Task:
    """`task` in the state an approval's outcome gives it (its old status moves to history)."""
    new = Task()
    new.CopyFrom(task)
    if new.status.HasField("message"):
        new.history.append(new.status.message)
    state = _OUTCOME_STATES.get(outcome.state, TaskState.TASK_STATE_FAILED)
    if state == TaskState.TASK_STATE_INPUT_REQUIRED and outcome.approvals:
        parts = approval_parts(list(outcome.approvals), note=outcome.text)
    else:
        parts = [Part(text=valid_text(outcome.text))]
    new.status.state = state
    new.status.message.CopyFrom(
        Message(
            message_id=uuid.uuid4().hex,
            role=Role.ROLE_AGENT,
            task_id=new.id,
            context_id=new.context_id,
            parts=parts,
        )
    )
    new.status.timestamp.GetCurrentTime()
    return new


async def follow_approval(outcome: ApprovalOutcome) -> None:
    """The tasks waiting on an approval follow its outcome (an `A2A_TASK_LISTENERS` entry)."""
    followed_count = 0
    for store in list(_STORES):
        followed_count += await store.follow(outcome)
    if followed_count:
        logger.info(
            "%d A2A task(s) followed an approval's outcome (%s)", followed_count, outcome.state
        )


def decision_problem(data: Any) -> str | None:
    """Why a data part naming an approval is not a valid decision, or None.

    A decision is `{"approval_id": str, "decision": "approve" | "reject",
    "comment": str (optional), "digest": str (optional; an agent relaying the
    person's decision sends the approval's digest)}`.
    """
    if not isinstance(data, dict):
        return "An approval decision must be an object."
    unknown = sorted(set(data) - {"approval_id", "decision", "comment", "digest"})
    if unknown:
        return (
            "An approval decision has only approval_id, decision, comment and digest "
            f"(not {unknown[0]})."
        )
    approval_id = data.get("approval_id")
    if not isinstance(approval_id, str) or not 0 < len(approval_id) <= _APPROVAL_ID_MAX_CHARS:
        return "approval_id must be the approval's id."
    if data.get("decision") not in DECISIONS:
        return "decision must be 'approve' or 'reject'."
    comment = data.get("comment")
    if comment is not None and (not isinstance(comment, str) or len(comment) > COMMENT_MAX_CHARS):
        return f"comment must be text of at most {COMMENT_MAX_CHARS} characters."
    digest = data.get("digest")
    if digest is not None and (not isinstance(digest, str) or len(digest) > DIGEST_MAX_CHARS):
        return f"digest must be the approval's digest (at most {DIGEST_MAX_CHARS} characters)."
    if any(_has_lone_surrogate(text) for text in _strings(data)):
        return "The approval decision is not valid Unicode text (an unpaired surrogate)."
    return None


def _names_approval(data: Any) -> bool:
    """Whether a data part is meant as an approval decision (it names an approval)."""
    return isinstance(data, dict) and ("approval_id" in data or "decision" in data)


def _part_data(part: Part) -> Any:
    if not part.HasField("data"):
        return None
    try:
        return json_format.MessageToDict(part.data)
    except Exception:
        return None


def approval_decision(message: Message | None) -> dict[str, Any] | None:
    """The approval decision a message's data part carries (checked), or None."""
    for part in message.parts if message is not None else []:
        data = _part_data(part)
        if _names_approval(data) and decision_problem(data) is None:
            return data
    return None


def _decision_parts_problem(datas: list[Any]) -> tuple[bool, str | None]:
    """Whether data parts carry an approval decision, and why they cannot when they try."""
    for data in datas:
        if _names_approval(data):
            return True, decision_problem(data)
    return False, None


def message_problem(from_user: bool, texts: list[str], decides: bool = False) -> str | None:
    """Why a message cannot start a run, or None.

    It needs the user role, at least one text part (unless it carries an
    approval decision, `decides`), no empty text part (the SDK cannot start a
    task from one), and at most `MAX_MESSAGE_CHARS` characters of text in all
    (joined as the executor joins them), the limit `/chat` applies.
    """
    if not from_user:
        return "The message must have the user role."
    if not texts and not decides:
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
    decides, problem = _decision_parts_problem([_part_data(part) for part in message.parts])
    problem = problem or message_problem(message.role == Role.ROLE_USER, texts, decides)
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
# answers the error itself, naming fields, never values), and the layer is
# served by `LegacyJsonRpcAdapter`, which answers an A2A error raised while
# handling a request (an unknown task, say) with its own code.
LEGACY_SEND_METHODS = ("message/send", "message/stream")
try:
    from a2a.compat.v0_3 import types as types_v03
    from a2a.compat.v0_3.jsonrpc_adapter import JSONRPC03Adapter
    from a2a.compat.v0_3.request_handler import RequestHandler03

    LEGACY_METHOD_MODELS: dict[str, Any] = dict(JSONRPC03Adapter.METHOD_TO_MODEL)
except ImportError:  # an SDK without the 0.3 layer, or with it elsewhere
    JSONRPC03Adapter = RequestHandler03 = types_v03 = None  # type: ignore[assignment,misc]
    LEGACY_METHOD_MODELS = {}
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603


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
    decides, problem = _decision_parts_problem(
        [part.get("data") for part in message["parts"] if isinstance(part, dict)]
    )
    return problem or message_problem(message.get("role") == "user", texts, decides)


def legacy_error(exc: A2AError) -> dict[str, Any] | None:
    """The JSON-RPC error for an A2A error raised by a 0.3 request, or None for an internal one.

    The code is the one A2A 1.0 answers with (`TaskNotFoundError` -32001,
    `TaskNotCancelableError` -32002, `PushNotificationNotSupportedError`
    -32003, ...); the message is the error's own, which names no value the
    server holds. It is logged at INFO: the caller asked for something that
    is not there, the server did nothing wrong.
    """
    code = JSON_RPC_ERROR_CODE_MAP.get(type(exc), JSONRPC_INTERNAL_ERROR)
    if code == JSONRPC_INTERNAL_ERROR:
        return None
    logger.info("A2A 0.3 request refused: %s (%d)", type(exc).__name__, code)
    return {"code": code, "message": str(exc)}


if RequestHandler03 is not None:

    class LegacyRequestHandler(RequestHandler03):
        """The SDK's 0.3 request handler; a streamed request's A2A error ends its stream."""

        async def on_message_send_stream(self, request: Any, context: ServerCallContext) -> Any:
            try:
                async for event in super().on_message_send_stream(request, context):
                    yield event
            except A2AError as exc:
                yield _legacy_stream_error(request, exc)

        async def on_subscribe_to_task(self, request: Any, context: ServerCallContext) -> Any:
            try:
                async for event in super().on_subscribe_to_task(request, context):
                    yield event
            except A2AError as exc:
                yield _legacy_stream_error(request, exc)

    class LegacyJsonRpcAdapter(JSONRPC03Adapter):
        """The SDK's A2A 0.3 layer, answering each A2A error with its own code.

        The SDK's layer answers any error raised while handling a 0.3 request
        as an internal error (-32603) and logs it at ERROR with a traceback, so
        any caller could write ERROR records with an unknown or deleted task id
        (`tasks/get`, `tasks/cancel`, `tasks/resubscribe`) or a push
        notification request (not supported). Here such a request gets the
        error A2A 1.0 answers with (`legacy_error`); an internal error is still
        answered and logged as the SDK does.
        """

        def __init__(self, http_handler: Any, context_builder: Any = None) -> None:
            super().__init__(http_handler, context_builder)
            self.handler = LegacyRequestHandler(request_handler=http_handler)

        async def _process_non_streaming_request(
            self, request_id: Any, request_obj: Any, context: ServerCallContext
        ) -> Any:
            try:
                return await super()._process_non_streaming_request(
                    request_id, request_obj, context
                )
            except A2AError as exc:
                error = legacy_error(exc)
                if error is None:
                    raise
                return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": error})


def _legacy_stream_error(request: Any, exc: A2AError) -> Any:
    """The stream event that ends a streamed 0.3 request with the A2A error `exc`."""
    error = legacy_error(exc)
    if error is None:
        raise exc
    return types_v03.SendStreamingMessageResponse(
        root=types_v03.JSONRPCErrorResponse(
            id=getattr(request, "id", None), error=types_v03.JSONRPCError(**error)
        )
    )


def _use_legacy_adapter(routes: list[Any], request_handler: Any, context_builder: Any) -> None:
    """Serve A2A 0.3 requests on `routes` with `LegacyJsonRpcAdapter` (see there).

    The SDK builds its 0.3 layer inside the JSON-RPC route's dispatcher; it is
    replaced there. With an SDK that builds it elsewhere the routes keep the
    SDK's own layer (the A2A 0.3 tests in `tests/integration/test_api_surface.py`
    notice).
    """
    if RequestHandler03 is None:
        return
    for route in routes:
        dispatcher = getattr(getattr(route, "endpoint", None), "__self__", None)
        if isinstance(getattr(dispatcher, "_v03_adapter", None), JSONRPC03Adapter):
            dispatcher._v03_adapter = LegacyJsonRpcAdapter(request_handler, context_builder)


class PolicyRequestHandler(DefaultRequestHandler):
    """The SDK's request handler, checking each message before a task exists.

    It also records whether the reply is streamed, so the executor returns a
    non-streamed reply as one text part, and answers a cancel or a
    subscription naming a task the caller does not have (`CancelTask`,
    `SubscribeToTask`, 0.3 `tasks/cancel`, `tasks/resubscribe`) before the SDK
    sets the task up: the SDK starts two event-queue loops first and leaves
    them running when the task is not found, which logs two ERROR records
    ("Task was destroyed but it is pending!") per request. A cancel or a
    subscription that reaches a replica other than the one running the task
    is refused (-32002 not cancelable, -32004 unsupported): the SDK would mark
    the task canceled here while its run goes on there and overwrites that, or
    wait here for events that only happen there.

    A person (a direct caller) reads (`GetTask`) and cancels (`CancelTask`) the
    tasks their agents started for them as well as their own: the request then
    runs under the task's own owner key (`owner_context`), so the canceled task
    is saved where it is. `ListTasks` lists them all (the stores widen a
    person's list). A message naming such a task, and `SubscribeToTask`, stay
    with its owner: "not found" for the person.
    """

    async def _task_context(self, task_id: str, context: ServerCallContext) -> ServerCallContext:
        """The context a GetTask or CancelTask of `task_id` runs under.

        The caller's own, unless the caller is a person and the task is one their
        agent started for them: then that task's owner key's (`owner_context`).
        """
        subject = task_subject(context)
        finder = getattr(self.task_store, "owner_for_subject", None)
        if subject is None or finder is None:
            return context
        owner = await finder(task_id, subject)
        if owner is None or owner == task_owner(context):
            return context
        return owner_context(context, owner)

    async def on_get_task(  # type: ignore[override]
        self, params: GetTaskRequest, context: ServerCallContext
    ) -> Any:
        return await super().on_get_task(params, await self._task_context(params.id, context))

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

    async def on_cancel_task(  # type: ignore[override]
        self, params: CancelTaskRequest, context: ServerCallContext
    ) -> Any:
        context = await self._task_context(params.id, context)
        task = await self.task_store.get(params.id, context)
        if task is None:
            raise TaskNotFoundError
        store = self.task_store
        if isinstance(store, RuntimeTaskStore) and await store.running_elsewhere(task):
            raise TaskNotCancelableError(
                message="The task is running on another replica of this agent: send the cancel "
                "again (it may reach that replica), or wait for the task to end."
            )
        return await super().on_cancel_task(params, context)

    async def on_subscribe_to_task(  # type: ignore[override]
        self, params: SubscribeToTaskRequest, context: ServerCallContext
    ) -> Any:
        task = await self.task_store.get(params.id, context)
        if task is None:
            raise TaskNotFoundError
        store = self.task_store
        if isinstance(store, RuntimeTaskStore) and await store.running_elsewhere(task):
            raise UnsupportedOperationError(
                message="The task is running on another replica of this agent: follow it with "
                "GetTask, or subscribe again (it may reach that replica)."
            )
        async for event in super().on_subscribe_to_task(params, context):
            yield event


def _client_message(exc: Exception) -> str:
    """What an A2A client is told about a failure: a 4xx detail as is, anything else generic."""
    if isinstance(exc, HTTPException) and exc.status_code < 500:
        return str(exc.detail)
    error_id = uuid.uuid4().hex[:12]
    logger.error("A2A task failed (error id %s)", error_id, exc_info=exc)
    return f"The agent could not process this request (error id {error_id})."


def approval_request(
    updater: TaskUpdater, approvals: list[Any], note: str | None = None, code: str | None = None
) -> Message:
    """The input-required status message: what waits for approval, as text and as data."""
    return updater.new_agent_message(approval_parts(approvals, note=note, code=code))


async def _fail(updater: TaskUpdater, text: str, code: str) -> None:
    """Fail the task with `text` and an error part naming `code` (`error_part`)."""
    await updater.failed(updater.new_agent_message([Part(text=valid_text(text)), error_part(code)]))


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
            # A lone surrogate (which protobuf cannot encode) is sent as U+FFFD.
            [Part(text=valid_text(text))],
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
            await _fail(updater, "Not authenticated.", ERROR_CODE_UNAUTHENTICATED)
            return
        if task.context_id and not CONTEXT_ID_PATTERN.fullmatch(task.context_id):
            await _fail(
                updater,
                "Invalid contextId: use 1-128 characters from A-Z a-z 0-9 _ . : -",
                ERROR_CODE_INVALID_CONTEXT,
            )
            return
        # An agent calling for a user may forward the user's own words (the origin
        # extension): kept with this request's credentials only, never stored. The run
        # a decision resumes acts on the words of the request that paused it instead
        # (kept with the approval: `resume_principal`), not on the decision's.
        origin, hops = read_origin(context.message, principal)
        depth = _max_depth()
        if hops > depth:
            await _fail(
                updater,
                f"delegation chain too deep (AUTH_MAX_DELEGATION_DEPTH={depth})",
                ERROR_CODE_TOO_DEEP,
            )
            return
        if origin is not None:
            principal = with_origin(principal, origin)
        decision = approval_decision(context.message)
        if decision is None and (not user_input or len(user_input) > max_message_chars()):
            # The request handler refuses these first; this guards any other caller.
            await _fail(updater, "The message is empty or too long.", ERROR_CODE_INVALID_MESSAGE)
            return
        req = ChatRequest(message=user_input or "", thread_id=task.context_id or None)
        try:
            thread_id = await RUNTIME.resolve_thread(principal, req)
        except Exception as exc:  # ownership or server errors end the task
            code = (
                ERROR_CODE_FORBIDDEN
                if isinstance(exc, HTTPException) and exc.status_code == 403
                else ERROR_CODE_FAILED
            )
            await _fail(updater, _client_message(exc), code)
            return

        lease = None
        if decision is not None:
            try:
                # The endpoint authorized `a2a.invoke`; deciding needs `approval.decide`
                # too, as on the HTTP route.
                await authorize_action(principal, "approval.decide", thread_id)
                lease, resume, acting = await RUNTIME.decide(
                    principal,
                    thread_id,
                    decision["approval_id"],
                    decision["decision"],
                    decision.get("comment"),
                    digest=decision.get("digest"),
                )
            except (ApprovalError, ThreadBusy, HTTPException) as exc:
                await self._decision_refused(updater, thread_id, exc)
                return
            # The tasks that waited on this approval follow the resumed run and point here.
            resume.continued_in = task.id
            resume.references = tuple(context.message.reference_task_ids)
            run = RUNTIME.stream(
                acting,
                ChatRequest(message="", thread_id=thread_id),
                thread_id,
                lease=lease,
                resume=resume,
            )
        else:
            run = RUNTIME.stream(principal, req, thread_id)
        try:
            # Closed here, whatever happens: the run's own cleanup (which waits
            # for the graph to stop, then releases the thread) runs first.
            async with aclosing(run) as events:
                await self._relay(updater, events, streaming=bool(state.get(STREAMING_STATE_KEY)))
        finally:
            if lease is not None:
                # The run released it when it ended; this covers one that never started.
                await lease.release()

    @staticmethod
    async def _relay(updater: TaskUpdater, run: Any, *, streaming: bool) -> None:
        """Turn a run's events into the task's artifact and final state."""
        # The reply is the `response` artifact (A2A clients read it from
        # artifacts, not from the final status message).
        reply = _Reply(updater, uuid.uuid4().hex, streaming=streaming)
        async for event, data in run:
            if event == EVENT_DELTA and data.get("text"):
                await reply.add(data["text"])
            elif event == EVENT_ERROR:
                await reply.finish()
                if data.get("code") == CODE_APPROVAL_PENDING:
                    # A new message while an approval is pending: ask for the decision.
                    await updater.requires_input(
                        approval_request(
                            updater, data.get("approvals") or [], code=CODE_APPROVAL_PENDING
                        )
                    )
                    return
                await _fail(
                    updater,
                    f"{data.get('code')}: {data.get('message')}",
                    str(data.get("code") or ERROR_CODE_FAILED),
                )
                return
            elif event == EVENT_END and data.get("status") == STATUS_AWAITING_APPROVAL:
                await reply.finish()
                await updater.requires_input(
                    approval_request(updater, data.get("approvals") or [data.get("approval")])
                )
                return
        await reply.finish()
        await updater.complete()

    @staticmethod
    async def _decision_refused(updater: TaskUpdater, thread_id: str, exc: Exception) -> None:
        """A decision that could not be taken: still waiting (input-required) or failed.

        Either way the status message carries an error part naming why (the
        approval's error code, `thread_busy`, ...).
        """
        if isinstance(exc, ApprovalError):
            note, code = f"{exc.code}: {exc.detail}", exc.code
        elif isinstance(exc, ThreadBusy):
            note, code = f"{THREAD_BUSY}: {exc}", THREAD_BUSY
        else:
            note = _client_message(exc)
            code = (
                ERROR_CODE_FORBIDDEN
                if isinstance(exc, HTTPException) and exc.status_code == 403
                else ERROR_CODE_FAILED
            )
        try:
            pending = await RUNTIME.pending_approvals(thread_id)
        except Exception:
            pending = []
        if pending:
            await updater.requires_input(approval_request(updater, pending, note=note, code=code))
        else:
            await _fail(updater, note, code)

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
        capabilities=AgentCapabilities(
            streaming=True,
            extensions=[
                AgentExtension(
                    uri=A2A_ORIGIN_EXTENSION,
                    description=ORIGIN_EXTENSION_DESCRIPTION,
                    required=False,
                )
            ],
        ),
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
    store = RuntimeTaskStore(ttl)
    _STORES.append(store)
    if forget_context not in DELETE_LISTENERS:
        DELETE_LISTENERS.append(forget_context)
    if follow_approval not in A2A_TASK_LISTENERS:
        A2A_TASK_LISTENERS.append(follow_approval)
    request_handler = PolicyRequestHandler(
        agent_executor=LangGraphAgentExecutor(),
        task_store=store,
        agent_card=card,
    )
    context_builder = PolicyContextBuilder()
    # v0.3 compat keeps older A2A clients working against the same endpoint.
    jsonrpc_routes = create_jsonrpc_routes(
        request_handler,
        rpc_url=A2A_RPC_PATH,
        context_builder=context_builder,
        enable_v0_3_compat=True,
    )
    _use_legacy_adapter(jsonrpc_routes, request_handler, context_builder)
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card, card_url=A2A_CARD_PATH),
        jsonrpc_routes=jsonrpc_routes,
    )
    return card
