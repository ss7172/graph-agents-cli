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
"""

from __future__ import annotations

import asyncio
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
from starlette.requests import Request

from {{cookiecutter.agent_directory}}.app_utils import auth as auth_module
from {{cookiecutter.agent_directory}}.app_utils import chat as chat_module
from {{cookiecutter.agent_directory}}.app_utils.a2a import (
    DEFAULT_TASK_TTL_S,
    ExpiringTaskStore,
    PolicyContextBuilder,
    PrincipalUser,
    task_owner,
    task_ttl_s,
)
from {{cookiecutter.agent_directory}}.app_utils.auth import ACTIONS, Principal
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
    async with app.router.lifespan_context(app):
        https = [
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
                headers={"X-User": name},
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
        monkeypatch.setenv("A2A_TASK_TTL_S", bad)
        assert task_ttl_s() == DEFAULT_TASK_TTL_S
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
