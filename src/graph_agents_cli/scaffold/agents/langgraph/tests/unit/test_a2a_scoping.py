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

"""A2A tasks are scoped per principal, expire after A2A_TASK_TTL_S, and fail closed.

Two principals come from a header test policy swapped in for the selected
one; the app runs in-process with `MODEL_PROVIDER=fake` and `CHECKPOINTER=memory`.
Which store serves the tasks (Postgres under a Postgres runtime database) and how
the Postgres store writes and fails are checked here without a database; the
store against a real Postgres is in `tests/integration/test_postgres.py`, and
across real server processes in `tests/integration/test_resilience_postgres.py`.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

# The environment must be in place before the app (and the graph) is imported.
os.environ.update(
    {
        "MODEL_PROVIDER": "fake",
        "MODEL_NAME": "fake",
        "CHECKPOINTER": "memory",
        "AUTH_POLICY": "shared-bearer",
        "API_KEY": "test-key",
        "APP_ENV": "dev",
        "TRACING_ENABLED": "false",
        "RUNTIME": "fastapi",
        "APP_URL": "http://testserver",
    }
)

import httpx
import pytest
from a2a.client import ClientConfig, create_client
from a2a.server.context import ServerCallContext
from a2a.types import (
    CancelTaskRequest,
    GetTaskRequest,
    ListTasksRequest,
    Message,
    Part,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    Task,
    TaskState,
)
from fastapi import HTTPException
from google.protobuf import json_format
from starlette.requests import Request

from {{cookiecutter.agent_directory}}.app_utils import a2a as a2a_module
from {{cookiecutter.agent_directory}}.app_utils import auth as auth_module
from {{cookiecutter.agent_directory}}.app_utils import chat as chat_module
from {{cookiecutter.agent_directory}}.app_utils import telemetry
from {{cookiecutter.agent_directory}}.app_utils.a2a import (
    DEFAULT_TASK_TTL_S,
    ExpiringTaskStore,
    PolicyContextBuilder,
    PostgresTaskStore,
    PrincipalUser,
    RuntimeTaskStore,
    task_owner,
    task_ttl_s,
)
from {{cookiecutter.agent_directory}}.app_utils.auth import ACTIONS, Actor, Principal
from {{cookiecutter.agent_directory}}.app_utils.checkpointer import MEMORY, POSTGRES
from {{cookiecutter.agent_directory}}.app_utils.db import Database
from {{cookiecutter.agent_directory}}.app_utils.limits import SettingsError
from {{cookiecutter.agent_directory}}.fast_api_app import app

A2A_PATH = "/a2a/{{cookiecutter.agent_directory}}"
A2A_URL = f"http://testserver{A2A_PATH}"


class HeaderPolicy:
    """Test policy: `X-User` is the principal id."""

    async def authenticate(self, request: Request) -> Principal:
        user = request.headers.get("x-user")
        if not user:
            raise HTTPException(401, "no user", headers={"WWW-Authenticate": "Bearer"})
        return Principal(id=user, roles=["user"], permissions=set(ACTIONS))

    async def authorize(self, principal: Principal, action: str, resource: str | None) -> None:
        return None


@pytest.fixture
async def users(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[dict[str, Any]]:
    """A2A clients for alice and bob; messages containing `slow` keep working for 30 s."""
    monkeypatch.setattr(auth_module, "get_policy", lambda: HeaderPolicy())
    original = chat_module.ChatRuntime.stream

    async def stream(self: Any, principal: Any, req: Any, thread_id: str) -> Any:
        if "slow" in req.message:
            yield chat_module.EVENT_DELTA, {"text": "working..."}
            await asyncio.sleep(30)
        async for item in original(self, principal, req, thread_id):
            yield item

    monkeypatch.setattr(chat_module.ChatRuntime, "stream", stream)
    # Principals of their own: the A2A task store lives as long as the app, and
    # other test modules (run first by a plain `pytest`) create tasks for "alice".
    suffix = uuid.uuid4().hex[:8]
    async with app.router.lifespan_context(app):
        https = [
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
                headers={"X-User": f"{name}-{suffix}"},
                timeout=30,
            )
            for name in ("alice", "bob")
        ]
        try:
            clients = {
                name: await create_client(A2A_URL, ClientConfig(streaming=False, httpx_client=h))
                for name, h in zip(("alice", "bob"), https, strict=True)
            }
            clients["http"] = dict(zip(("alice", "bob"), https, strict=True))
            yield clients
        finally:
            for h in https:
                await h.aclose()


def _message(text: str, **fields: Any) -> SendMessageRequest:
    return SendMessageRequest(
        message=Message(
            message_id=f"m-{uuid.uuid4()}", role=Role.ROLE_USER, parts=[Part(text=text)], **fields
        )
    )


async def _send(client: Any, request: SendMessageRequest) -> Task:
    task = None
    async for chunk in client.send_message(request):
        if chunk.HasField("task"):
            task = chunk.task
    assert task is not None
    return task


async def test_other_principals_cannot_list_get_or_cancel_a_task(users: dict[str, Any]) -> None:
    alice, bob = users["alice"], users["bob"]
    task = await _send(alice, _message("my pin is 9876"))
    assert task.status.state == TaskState.TASK_STATE_COMPLETED

    own = await alice.list_tasks(ListTasksRequest(include_artifacts=True))
    assert [t.id for t in own.tasks] == [task.id]
    theirs = await bob.list_tasks(ListTasksRequest(include_artifacts=True))
    assert theirs.total_size == 0 and not theirs.tasks

    assert (await alice.get_task(GetTaskRequest(id=task.id))).id == task.id
    with pytest.raises(Exception, match="not found"):
        await bob.get_task(GetTaskRequest(id=task.id))

    # The pre-1.0 JSON-RPC surface goes through the same store.
    r = await users["http"]["bob"].post(
        A2A_PATH,
        json={"jsonrpc": "2.0", "id": "1", "method": "tasks/get", "params": {"id": task.id}},
        headers={"A2A-Version": "0.3"},
    )
    body = r.json()
    assert "result" not in body and "9876" not in r.text


async def test_cancel_is_owner_only_and_works_for_the_owner(users: dict[str, Any]) -> None:
    alice, bob = users["alice"], users["bob"]
    request = _message("slow job")
    request.configuration.CopyFrom(SendMessageConfiguration(return_immediately=True))
    task = await _send(alice, request)
    await asyncio.sleep(0.2)

    with pytest.raises(Exception, match="not found"):
        await bob.cancel_task(CancelTaskRequest(id=task.id))
    # Nor can bob attach to its live event stream.
    r = await users["http"]["bob"].post(
        A2A_PATH,
        json={"jsonrpc": "2.0", "id": "2", "method": "SubscribeToTask", "params": {"id": task.id}},
        headers={"A2A-Version": "1.0"},
    )
    assert "not found" in r.text.lower() and "working..." not in r.text
    still = await alice.get_task(GetTaskRequest(id=task.id))
    assert still.status.state == TaskState.TASK_STATE_WORKING

    canceled = await alice.cancel_task(CancelTaskRequest(id=task.id))
    assert canceled.status.state == TaskState.TASK_STATE_CANCELED


async def test_another_principals_task_or_context_cannot_be_continued(
    users: dict[str, Any],
) -> None:
    alice, bob = users["alice"], users["bob"]
    task = await _send(alice, _message("hello"))
    # Naming alice's task: not found for bob.
    with pytest.raises(Exception, match="not found"):
        await _send(bob, _message("hi", task_id=task.id))
    # Naming alice's context (her thread): refused by thread ownership.
    refused = await _send(bob, _message("hi", context_id=task.context_id))
    assert refused.status.state == TaskState.TASK_STATE_FAILED
    assert "another principal" in refused.status.message.parts[0].text


async def test_an_invalid_context_id_fails_the_task(users: dict[str, Any]) -> None:
    task = await _send(users["alice"], _message("hi", context_id="../../etc/passwd"))
    assert task.status.state == TaskState.TASK_STATE_FAILED
    assert "Invalid contextId" in task.status.message.parts[0].text


# --- the task store ------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _ctx(owner: str) -> ServerCallContext:
    return ServerCallContext(user=PrincipalUser(owner))


async def test_tasks_expire_after_the_ttl() -> None:
    clock = Clock()
    store = ExpiringTaskStore(60, clock=clock)
    await store.save(Task(id="t1", context_id="c1"), _ctx("alice"))
    await store.save(Task(id="t2", context_id="c2"), _ctx("bob"))
    clock.now += 30
    assert await store.get("t1", _ctx("alice")) is not None
    assert await store.get("t1", _ctx("bob")) is None
    await store.save(Task(id="t1", context_id="c1"), _ctx("alice"))  # an update restarts the TTL
    clock.now += 45
    assert await store.get("t1", _ctx("alice")) is not None
    assert (await store.list(ListTasksRequest(), _ctx("bob"))).total_size == 0  # t2 expired
    clock.now += 60
    assert await store.get("t1", _ctx("alice")) is None
    assert store._saved_at == {}  # nothing left behind in memory


async def test_the_sweep_evicts_tasks_nobody_reads_again() -> None:
    clock = Clock()
    store = ExpiringTaskStore(10, clock=clock)
    for i in range(20):
        await store.save(Task(id=f"t{i}", context_id="c"), _ctx(f"user{i}"))
    clock.now += 11
    await store.save(Task(id="new", context_id="c"), _ctx("carol"))
    assert list(store._saved_at) == ["carol"]


async def test_ttl_zero_keeps_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    store = ExpiringTaskStore(0, clock=clock)
    await store.save(Task(id="t1", context_id="c1"), _ctx("alice"))
    clock.now += 10**9
    assert await store.get("t1", _ctx("alice")) is not None
    monkeypatch.setenv("A2A_TASK_TTL_S", "0")
    assert task_ttl_s() == 0
    for bad in ("-1", "soon", "1.5"):
        # Refused, not replaced by the default: the startup check names it.
        monkeypatch.setenv("A2A_TASK_TTL_S", bad)
        with pytest.raises(SettingsError, match="A2A_TASK_TTL_S"):
            task_ttl_s()
    monkeypatch.delenv("A2A_TASK_TTL_S")
    assert task_ttl_s() == DEFAULT_TASK_TTL_S == 3600


def test_no_principal_means_no_task_access() -> None:
    with pytest.raises(PermissionError):
        task_owner(ServerCallContext())  # unauthenticated user
    with pytest.raises(PermissionError):
        task_owner(_ctx(""))
    request = Request({"type": "http", "method": "POST", "path": A2A_PATH, "headers": []})
    with pytest.raises(PermissionError):
        PolicyContextBuilder().build_user(request)
    request.state.principal = Principal(id="alice")
    assert PolicyContextBuilder().build_user(request).user_name == "alice"


def test_the_call_context_does_not_carry_the_credential() -> None:
    headers = [
        (b"authorization", b"Bearer secret-token"),
        (b"cookie", b"sid=secret-session"),
        (b"a2a-version", b"1.0"),
    ]
    request = Request({"type": "http", "method": "POST", "path": A2A_PATH, "headers": headers})
    request.state.principal = Principal(id="alice")
    context = PolicyContextBuilder().build(request)
    assert context.user.user_name == "alice"
    assert context.state["principal"].id == "alice"
    assert context.state["headers"] == {"a2a-version": "1.0"}
    assert "secret" not in repr(context)


async def test_without_the_policy_override_the_rpc_still_requires_the_key() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        r = await client.post(A2A_PATH, json={"jsonrpc": "2.0", "id": "1", "method": "ListTasks"})
        assert r.status_code == 401


# --- which store, and how the Postgres store writes and fails (no database) --------------


class RecordingDb:
    """Stands in for `Database`: records statements, or fails them with `error`."""

    a2a_tasks_table = "a2a_tasks"
    locks_table = "thread_locks"
    is_postgres = True

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.statements: list[tuple[str, Any]] = []

    async def _run(self, sql: str, params: Any) -> Any:
        self.statements.append((" ".join(sql.split()), params))
        if self.error is not None:
            raise self.error
        return None

    async def execute(self, sql: str, params: Any = ()) -> None:
        await self._run(sql, params)

    async def fetchone(self, sql: str, params: Any = ()) -> Any:
        return await self._run(sql, params)

    async def fetchall(self, sql: str, params: Any = ()) -> list[Any]:
        return await self._run(sql, params) or []

    def writes(self) -> list[str]:
        return [sql for sql, _ in self.statements if sql.startswith("INSERT")]


def _task(state: int, text: str = "", second: int = 0) -> Task:
    task = Task(id="t1", context_id="c1")
    task.status.state = state
    task.status.timestamp.FromSeconds(1_800_000_000 + second)
    if text:
        artifact = task.artifacts.add()
        artifact.artifact_id = "a1"
        artifact.parts.add().text = text
    return task


async def test_the_task_store_follows_the_runtime_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Postgres (shared by replicas) when the runtime's database is, memory otherwise."""
    runtime = chat_module.RUNTIME
    store = RuntimeTaskStore(60)
    monkeypatch.setattr(runtime, "db", Database(MEMORY))
    assert store.postgres() is None
    db = Database(POSTGRES, "postgresql://agent@db.invalid/agent")
    monkeypatch.setattr(runtime, "db", db)
    monkeypatch.setattr(runtime, "started", True)
    monkeypatch.setattr(runtime, "initialising", True)
    with pytest.raises(HTTPException) as refused:  # the schema is not set up yet
        store.postgres()
    assert refused.value.status_code == 503
    monkeypatch.setattr(runtime, "initialising", False)
    postgres = store.postgres()
    assert isinstance(postgres, PostgresTaskStore)
    assert postgres.db is db and postgres.table == "a2a_tasks" and store.postgres() is postgres
    server = Database.for_server()
    assert (server.a2a_tasks_table, db.a2a_tasks_table) == ("agent_a2a_tasks", "a2a_tasks")
    assert "CREATE TABLE IF NOT EXISTS a2a_tasks" in db.ddl()


async def test_reply_chunks_are_written_at_most_once_a_second() -> None:
    """A streamed reply is not one database write per chunk; a status change always is."""
    clock = Clock()
    db = RecordingDb()
    store = PostgresTaskStore(60, db, clock=clock)  # type: ignore[arg-type]
    await store.save(_task(TaskState.TASK_STATE_WORKING), _ctx("alice"))
    for text in ("Hel", "Hello", "Hello, wor"):
        clock.now += 0.2
        await store.save(_task(TaskState.TASK_STATE_WORKING, text), _ctx("alice"))
    assert len(db.writes()) == 1
    clock.now += 1
    await store.save(_task(TaskState.TASK_STATE_WORKING, "Hello, world"), _ctx("alice"))
    assert len(db.writes()) == 2
    await store.save(_task(TaskState.TASK_STATE_COMPLETED, "Hello, world!", 1), _ctx("alice"))
    assert len(db.writes()) == 3
    # Another principal's task of the same id is its own.
    await store.save(_task(TaskState.TASK_STATE_WORKING), _ctx("bob"))
    assert len(db.writes()) == 4
    owners = [params[0] for sql, params in db.statements if sql.startswith("INSERT")]
    assert owners == ["alice", "alice", "alice", "bob"]


async def test_database_errors_never_reach_the_caller_as_text() -> None:
    import psycopg

    unreachable = RecordingDb(psycopg.OperationalError("host db.internal:5432"))
    down = PostgresTaskStore(60, unreachable)  # type: ignore[arg-type]
    with pytest.raises(HTTPException) as unavailable:
        await down.get("t1", _ctx("alice"))
    assert unavailable.value.status_code == 503
    assert "db.internal" not in str(unavailable.value.detail)
    failing = RecordingDb(RuntimeError("syntax error at SELECT secret"))
    broken = PostgresTaskStore(60, failing)  # type: ignore[arg-type]
    with pytest.raises(a2a_module.InternalError) as internal:
        await broken.get("t1", _ctx("alice"))
    assert "Reference" in str(internal.value) and "secret" not in str(internal.value)


def _strings_in(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for key, item in value.items() for s in (key, *_strings_in(item))]
    if isinstance(value, list):
        return [s for item in value for s in _strings_in(item)]
    return []


async def test_a_nul_character_never_reaches_the_database() -> None:
    """Postgres stores no U+0000 (jsonb and TEXT refuse it), so the store writes U+FFFD.

    Everywhere in the task (text, data, metadata, the contextId) and in the ids
    a caller names. A save that failed on one lost the task while its run, and
    whatever its tools did, went on. The owner is never changed.
    """
    db = RecordingDb()
    store = PostgresTaskStore(60, db)  # type: ignore[arg-type]
    task = _task(TaskState.TASK_STATE_COMPLETED, "reading\x00 42, and \\u0000 as text")
    task.context_id = "ab\x00cd"
    message = task.history.add()
    message.message_id = "m1"
    message.parts.add().text = "tell me\x00 more"
    json_format.ParseDict({"key\x00": ["value\x00"]}, message.parts.add().data)
    json_format.ParseDict({"note": "x\x00"}, message.metadata)
    await store.save(task, _ctx("alice"))
    await store.get("t\x00", _ctx("alice"))
    await store.list(ListTasksRequest(context_id="ab\x00cd"), _ctx("alice"))
    await store.delete("t\x00", _ctx("alice"))
    sent = [param for _, params in db.statements for param in params if isinstance(param, str)]
    assert not [param for param in sent if "\x00" in param]
    (insert,) = [params for sql, params in db.statements if sql.startswith("INSERT")]
    stored = json.loads(insert[6])
    assert not [text for text in _strings_in(stored) if "\x00" in text]
    assert stored["contextId"] == "ab\ufffdcd" and insert[2:4] == ("ab\ufffdcd", "ab\ufffdcd")
    assert stored["artifacts"][0]["parts"][0]["text"] == "reading\ufffd 42, and \\u0000 as text"
    assert stored["history"][0]["parts"] == [
        {"text": "tell me\ufffd more"},
        {"data": {"key\ufffd": ["value\ufffd"]}},
    ]
    assert stored["history"][0]["metadata"] == {"note": "x\ufffd"}
    lookups = [params for sql, params in db.statements if not sql.startswith("INSERT")]
    assert ("alice", "t\ufffd") in [tuple(params[:2]) for params in lookups]
    assert any("ab\ufffdcd" in params for params in lookups)


def test_the_a2a_defaults_on_record() -> None:
    """Defaults decided for 0.3 (P4 and the trace scope read them): the user's own words go
    only to peers that declare the origin extension, and under the default trace scope an
    incoming `traceparent` is continued on the A2A routes only."""
    assert a2a_module.DEFAULT_A2A_FORWARD_ORIGIN == "auto"
    assert telemetry.PEERS_INBOUND_TRACE_PREFIX == "/a2a/"
    for path in (a2a_module.A2A_RPC_PATH, a2a_module.A2A_CARD_PATH):
        assert path.startswith(telemetry.PEERS_INBOUND_TRACE_PREFIX)
    assert not "/chat".startswith(telemetry.PEERS_INBOUND_TRACE_PREFIX)


# --- agents acting for the same user (0.3): tasks are owned by the owner key ---------------


class ActorHeaderPolicy:
    """Test policy: `X-User` is the subject, `X-Actor` (when sent) the agent presenting it."""

    async def authenticate(self, request: Request) -> Principal:
        user = request.headers.get("x-user")
        if not user:
            raise HTTPException(401, "no user", headers={"WWW-Authenticate": "Bearer"})
        actor = request.headers.get("x-actor")
        return Principal(
            id=user,
            roles=["user"],
            permissions=set(ACTIONS),
            actor=Actor(id=actor) if actor else None,
        )

    async def authorize(self, principal: Principal, action: str, resource: str | None) -> None:
        return None


@pytest.fixture
async def agents(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[dict[str, Any]]:
    """A2A clients of one user: directly, and through the agents concierge and billing."""
    monkeypatch.setattr(auth_module, "get_policy", lambda: ActorHeaderPolicy())
    monkeypatch.setenv("AUTH_ALLOWED_ACTORS", "concierge,billing")
    user = f"alice-{uuid.uuid4().hex[:8]}"
    headers = {
        "direct": {"X-User": user},
        "concierge": {"X-User": user, "X-Actor": "concierge"},
        "billing": {"X-User": user, "X-Actor": "billing"},
    }
    async with app.router.lifespan_context(app):
        https = {
            name: httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
                headers=value,
                timeout=30,
            )
            for name, value in headers.items()
        }
        try:
            clients: dict[str, Any] = {
                name: await create_client(A2A_URL, ClientConfig(streaming=False, httpx_client=h))
                for name, h in https.items()
            }
            clients["http"] = https
            yield clients
        finally:
            for h in https.values():
                await h.aclose()


def test_the_task_owner_is_the_owner_key() -> None:
    request = Request({"type": "http", "method": "POST", "path": A2A_PATH, "headers": []})
    request.state.principal = Principal(id="alice", actor=Actor(id="concierge"))
    assert PolicyContextBuilder().build_user(request).user_name == "alice\x1fconcierge"
    request.state.principal = Principal(id="alice")
    assert PolicyContextBuilder().build_user(request).user_name == "alice"  # as in 0.2


async def test_other_actor_cannot_get_list_continue_cancel(agents: dict[str, Any]) -> None:
    concierge, billing = agents["concierge"], agents["billing"]
    task = await _send(concierge, _message("my pin is 9876"))
    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    assert [t.id for t in (await concierge.list_tasks(ListTasksRequest())).tasks] == [task.id]
    for other in (billing, agents["direct"]):
        # Another agent for the same user, and the user directly: other owner keys.
        assert (await other.list_tasks(ListTasksRequest())).total_size == 0
        with pytest.raises(Exception, match="not found"):
            await other.get_task(GetTaskRequest(id=task.id))
        with pytest.raises(Exception, match="not found"):
            await other.cancel_task(CancelTaskRequest(id=task.id))
        with pytest.raises(Exception, match="not found"):
            await _send(other, _message("hi", task_id=task.id))
    # The conversation: another agent is refused (no oracle beyond the thread rule) ...
    refused = await _send(billing, _message("hi", context_id=task.context_id))
    assert refused.status.state == TaskState.TASK_STATE_FAILED
    assert "another principal" in refused.status.message.parts[0].text
    # ... while the person, who owns every thread of theirs, may continue it.
    continued = await _send(agents["direct"], _message("hi", context_id=task.context_id))
    assert continued.status.state == TaskState.TASK_STATE_COMPLETED


async def test_a_delegated_caller_is_refused_until_listed(
    agents: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_ALLOWED_ACTORS", "billing")
    r = await agents["http"]["concierge"].get(f"{A2A_PATH}/.well-known/agent-card.json")
    assert r.status_code == 403
    assert r.json() == {
        "detail": "Delegated caller concierge is not allowed here (AUTH_ALLOWED_ACTORS)."
    }
