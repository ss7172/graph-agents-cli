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

"""What an agent calling this one reads and sends over A2A (0.3), and what is never kept.

This agent plays `orders`, with a gated `cancelOrder` call to a backend that
records every request (`httpx.MockTransport`) and a tool that reports the user's
words its run was given. A test auth policy reads the caller from headers:
`X-User` is the person, `X-Actor` the agent presenting the request for them
(`concierge`), so the tests send what that agent would, over A2A JSON-RPC and
HTTP.

* The origin extension: the card declares it; the user's own words an agent
  forwards reach the run (never a direct caller's metadata), and are never
  stored with the task; too many hops fail the task.
* The approval request: `approval_json` keeps exact values, the text shows the body.
* A failed or refused task names why in an error part (`thread_busy`).
* Tasks waiting on an approval follow its outcome (KI-025): a decision sent on the
  context alone, one the person takes over HTTP, a rejection, an expiry; another
  owner's task on the same thread stays as it was.

Every test runs with the in-memory checkpointer, and on Postgres when
`TEST_POSTGRES_DSN` is set.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
from fastapi import HTTPException
from langchain.tools import ToolRuntime
from langchain_core.tools import tool
from starlette.requests import Request

from {{cookiecutter.agent_directory}}.app_utils import auth as auth_module
from {{cookiecutter.agent_directory}}.app_utils import chat as chat_module
from {{cookiecutter.agent_directory}}.app_utils.api_client import (
    A2A_ORIGIN_EXTENSION,
    get_client,
)
from {{cookiecutter.agent_directory}}.app_utils.api_client import (
    reset_policy_cache as reset_api_policy,
)
from {{cookiecutter.agent_directory}}.app_utils.approvals import utcnow
from {{cookiecutter.agent_directory}}.app_utils.auth import ACTIONS, Actor, Principal
from {{cookiecutter.agent_directory}}.app_utils.chat import RUNTIME
from {{cookiecutter.agent_directory}}.fast_api_app import app

A2A_PATH = "/a2a/{{cookiecutter.agent_directory}}"
PROMPT = "Cancel the order for 7"
BODY = {"reason": "customer asked", "amount": 1, "big": 12345678901234567890}

POLICY = """
apis:
  shop:
    base_url_env: SHOP_API_BASE_URL
    auth: none
    allowed_methods: [GET, POST]
    approval:
      required_for:
        operations:
          - operationId: cancelOrder
            path: /orders/{order_id}/cancel
            methods: [POST]
      approvers: [requester]
      timeout_s: 60
      decide_with: relayed
      relayers: [concierge]
"""

# What the backend received.
SENT: list[httpx.Request] = []


def _backend(request: httpx.Request) -> httpx.Response:
    SENT.append(request)
    return httpx.Response(200, json={"cancelled": request.url.path})


@tool
async def cancel_order(order_id: str, runtime: ToolRuntime[Any]) -> str:
    """Cancel an order by its id."""
    context = getattr(runtime, "context", None)
    client = get_client("shop", context=context, transport=httpx.MockTransport(_backend))
    data = await client.post(
        "/orders/{order_id}/cancel",
        operation_id="cancelOrder",
        path_params={"order_id": order_id},
        json_body=BODY,
    )
    return json.dumps(data)


@tool
async def echo_origin(note: str, runtime: ToolRuntime[Any]) -> str:
    """Echo the words of the user this run was given (the origin), if any."""
    context = getattr(runtime, "context", None)
    attributes = getattr(context, "attributes", None) or {}
    origin = (attributes.get("credentials") or {}).get("@origin")
    return "ORIGIN=" + json.dumps(origin)


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


ADMIN_DSN = os.environ.get("TEST_POSTGRES_DSN", "")


@pytest.fixture(params=["memory", "postgres"])
async def database(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[str]:
    if request.param == "memory":
        yield "memory"
        return
    if not ADMIN_DSN:
        pytest.skip("TEST_POSTGRES_DSN is not set")
    import psycopg

    name = f"gac_test_{uuid.uuid4().hex[:12]}"
    async with await psycopg.AsyncConnection.connect(ADMIN_DSN, autocommit=True) as admin:
        await admin.execute(f'CREATE DATABASE "{name}"')
    monkeypatch.setenv("CHECKPOINTER", "postgres")
    monkeypatch.setenv("POSTGRES_DSN", urlsplit(ADMIN_DSN)._replace(path=f"/{name}").geturl())
    yield "postgres"
    async with await psycopg.AsyncConnection.connect(ADMIN_DSN, autocommit=True) as admin:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
async def orders(
    database: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_test_tools: Any,
) -> AsyncIterator[dict[str, Any]]:
    """The app as `orders`: clients for one person, directly and through the concierge."""
    policy = tmp_path / "api-policy.yaml"
    policy.write_text(POLICY, encoding="utf-8")
    monkeypatch.setenv("API_POLICY_PATH", str(policy))
    monkeypatch.setenv("SHOP_API_BASE_URL", "http://shop.test")
    monkeypatch.setenv("AUTH_ALLOWED_ACTORS", "concierge")
    monkeypatch.delenv("TRACE_CAPTURE", raising=False)
    monkeypatch.setattr(auth_module, "get_policy", lambda: ActorHeaderPolicy())
    reset_api_policy()
    SENT.clear()
    user = f"alice-{uuid.uuid4().hex[:8]}"  # the A2A task store lives as long as the app
    headers = {
        "person": {"X-User": user},
        "concierge": {"X-User": user, "X-Actor": "concierge"},
    }
    async with app.router.lifespan_context(app):
        use_test_tools(cancel_order, echo_origin)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        clients = {
            name: httpx.AsyncClient(
                transport=transport, base_url="http://testserver", headers=value, timeout=30
            )
            for name, value in headers.items()
        }
        try:
            started = asyncio.get_running_loop().time()
            while (await clients["person"].get("/ready")).status_code != 200:  # the schema
                assert asyncio.get_running_loop().time() - started < 30, "not ready"
                await asyncio.sleep(0.1)
            yield {**clients, "database": database, "user": user}
        finally:
            for client in clients.values():
                await client.aclose()
    reset_api_policy()


async def _rpc(client: httpx.AsyncClient, method: str, params: dict[str, Any]) -> dict[str, Any]:
    r = await client.post(
        A2A_PATH,
        json={"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": params},
        headers={"A2A-Version": "1.0"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _message(
    *parts: dict[str, Any], task: dict[str, Any] | None = None, **fields: Any
) -> dict[str, Any]:
    message: dict[str, Any] = {"messageId": uuid.uuid4().hex, "role": "ROLE_USER", "parts": parts}
    if task is not None:
        message.update(taskId=task["id"], contextId=task["contextId"])
    message.update(fields)
    return {"message": message}


def _data_parts(task: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [
        p["data"]
        for p in task["status"]["message"]["parts"]
        if isinstance(p.get("data"), dict) and p["data"].get("type") == kind
    ]


def _note(task: dict[str, Any]) -> str:
    return "".join(p.get("text", "") for p in task["status"]["message"]["parts"])


async def _paused(orders: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The concierge's request to cancel, paused for the person's approval."""
    sent = await _rpc(orders["concierge"], "SendMessage", _message({"text": PROMPT}))
    task = sent["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED", task
    [request] = _data_parts(task, "approval_request")
    [approval] = json.loads(request["approval_json"])
    assert approval["status"] == "pending" and SENT == []
    return task, approval


def _decision(approval: dict[str, Any], decision: str = "approve") -> dict[str, Any]:
    return {
        "data": {
            "approval_id": approval["approval_id"],
            "decision": decision,
            "digest": approval["digest"],
        }
    }


async def _stored_tasks(orders: dict[str, Any]) -> list[str]:
    """Every stored task's JSON text (Postgres); [] under memory."""
    if orders["database"] != "postgres":
        return []
    import psycopg

    async with await psycopg.AsyncConnection.connect(os.environ["POSTGRES_DSN"]) as conn:
        cursor = await conn.execute("SELECT task::text FROM a2a_tasks")
        return [str(row[0]) for row in await cursor.fetchall()]


# --- the origin extension --------------------------------------------------------------------


async def test_the_card_declares_the_origin_extension(orders: dict[str, Any]) -> None:
    card = (await orders["person"].get(f"{A2A_PATH}/.well-known/agent-card.json")).json()
    [extension] = card["capabilities"]["extensions"]
    assert extension["uri"] == A2A_ORIGIN_EXTENSION
    assert not extension.get("required")  # an optional extension: clients may ignore it


async def test_the_users_words_reach_the_run_and_are_never_stored(orders: dict[str, Any]) -> None:
    origin = {"text": "please echo the origin, it is ORD-7", "truncated": False, "hops": 1}
    sent = await _rpc(
        orders["concierge"],
        "SendMessage",
        _message(
            {"text": "Echo the origin"},
            metadata={A2A_ORIGIN_EXTENSION: {"origin": origin}, "other": "kept"},
        ),
    )
    task = sent["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED", task
    reply = "".join(p["text"] for a in task["artifacts"] for p in a["parts"])
    assert json.loads(reply.split("ORIGIN=", 1)[1].rstrip(".").split("\n")[0]) == origin
    # Not stored: neither in the task the caller reads back nor in the table.
    got = await _rpc(orders["concierge"], "GetTask", {"id": task["id"]})
    held = json.dumps(got["result"])
    assert A2A_ORIGIN_EXTENSION not in held and "ORD-7" not in held.split("ORIGIN=")[0]
    assert got["result"]["history"][0]["metadata"] == {"other": "kept"}
    for stored in await _stored_tasks(orders):
        assert A2A_ORIGIN_EXTENSION not in stored


async def test_a_persons_own_metadata_is_not_read_as_forwarded_words(
    orders: dict[str, Any],
) -> None:
    """A direct caller's message is the person's own words: no origin is taken from it."""
    origin = {"text": "someone else's words", "truncated": False, "hops": 1}
    sent = await _rpc(
        orders["person"],
        "SendMessage",
        _message({"text": "Echo the origin"}, metadata={A2A_ORIGIN_EXTENSION: {"origin": origin}}),
    )
    task = sent["result"]["task"]
    reply = "".join(p["text"] for a in task["artifacts"] for p in a["parts"])
    assert "ORIGIN=null" in reply


async def test_words_forwarded_through_too_many_agents_fail_the_task(
    orders: dict[str, Any],
) -> None:
    origin = {"text": "cancel ORD-7", "truncated": False, "hops": 4}  # the default depth is 3
    sent = await _rpc(
        orders["concierge"],
        "SendMessage",
        _message({"text": PROMPT}, metadata={A2A_ORIGIN_EXTENSION: {"origin": origin}}),
    )
    task = sent["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert "delegation chain too deep (AUTH_MAX_DELEGATION_DEPTH=3)" in _note(task)
    assert _data_parts(task, "error") == [{"type": "error", "code": "delegation_too_deep"}]
    assert SENT == []


# --- what an agent reads: exact values, and why a task failed ---------------------------------


async def test_the_approval_request_holds_exact_values_and_shows_the_body(
    orders: dict[str, Any],
) -> None:
    task, approval = await _paused(orders)
    assert approval["body"] == BODY  # approval_json: exact, the big integer included
    [request] = _data_parts(task, "approval_request")
    assert request["approvals"][0]["body"]["amount"] == 1.0  # the Struct: a double
    assert request["approvals"][0]["decide_with"] == "relayed"
    assert '"big": 12345678901234567890' in _note(task)
    assert "until" in _note(task) and approval["digest"] in json.dumps(request)


async def test_a_busy_thread_fails_the_task_with_an_error_part(
    orders: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def busy(self: Any, *args: Any, **kwargs: Any) -> Any:
        yield chat_module.EVENT_ERROR, chat_module.thread_busy_error()

    monkeypatch.setattr(chat_module.ChatRuntime, "stream", busy)
    sent = await _rpc(orders["concierge"], "SendMessage", _message({"text": "hello"}))
    task = sent["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert _data_parts(task, "error") == [{"type": "error", "code": "thread_busy"}]


# --- tasks follow the approval's outcome (KI-025) ---------------------------------------------


async def test_a_task_follows_a_decision_sent_on_its_context(orders: dict[str, Any]) -> None:
    """The relay's decision names the context and the task it answers, not the task: it runs
    as a new task, and the task that waited ends as that run did, pointing at it."""
    task, approval = await _paused(orders)
    decided = await _rpc(
        orders["concierge"],
        "SendMessage",
        _message(_decision(approval), contextId=task["contextId"], referenceTaskIds=[task["id"]]),
    )
    carrier = decided["result"]["task"]
    assert carrier["id"] != task["id"]
    assert carrier["status"]["state"] == "TASK_STATE_COMPLETED", carrier
    assert [(r.method, r.url.path) for r in SENT] == [("POST", "/orders/7/cancel")]
    waited = (await _rpc(orders["concierge"], "GetTask", {"id": task["id"]}))["result"]
    assert waited["status"]["state"] == "TASK_STATE_COMPLETED"
    assert _note(waited).startswith(f"Continued in task {carrier['id']}.")
    # The old status (what it waited for) is kept in the history.
    assert any("Waiting for approval" in json.dumps(m) for m in waited["history"])


async def test_a_task_follows_a_rejection(orders: dict[str, Any]) -> None:
    task, approval = await _paused(orders)
    decided = await _rpc(
        orders["concierge"],
        "SendMessage",
        _message(_decision(approval, "reject"), contextId=task["contextId"]),
    )
    carrier = decided["result"]["task"]
    waited = (await _rpc(orders["concierge"], "GetTask", {"id": task["id"]}))["result"]
    assert waited["status"]["state"] == "TASK_STATE_COMPLETED"
    assert _note(waited).startswith(
        f"Approval {approval['approval_id']} was rejected. Continued in task {carrier['id']}."
    )
    assert SENT == []


async def test_a_task_follows_a_decision_the_person_takes_over_http(
    orders: dict[str, Any],
) -> None:
    """The person approves at this agent directly (the KI-025 case): the agent's task, under
    another owner key than the person's, ends too, with the run's reply."""
    task, approval = await _paused(orders)
    r = await orders["person"].post(
        f"/threads/{task['contextId']}/approvals/{approval['approval_id']}",
        json={"decision": "approve"},
    )
    assert r.status_code == 200 and "message.end" in r.text
    waited = (await _rpc(orders["concierge"], "GetTask", {"id": task["id"]}))["result"]
    assert waited["status"]["state"] == "TASK_STATE_COMPLETED"
    note = _note(waited)
    assert note.startswith(
        f"Approval {approval['approval_id']} was approved outside this task; the run "
        "continued there."
    )
    assert "Here is what I found" in note  # the resumed run's reply


async def test_a_task_follows_an_expiry(orders: dict[str, Any]) -> None:
    task, approval = await _paused(orders)
    store = RUNTIME.approvals
    assert store is not None
    later = utcnow() + timedelta(hours=1)
    original = store._clock
    store._clock = lambda: later
    try:
        await RUNTIME.sweep_approvals()
    finally:
        store._clock = original
    waited = (await _rpc(orders["concierge"], "GetTask", {"id": task["id"]}))["result"]
    assert waited["status"]["state"] == "TASK_STATE_FAILED"
    assert _note(waited) == f"Approval {approval['approval_id']} expired before anyone decided."
    assert SENT == []


async def test_another_owners_task_on_the_thread_is_left_alone(orders: dict[str, Any]) -> None:
    """The person asks on their agent's thread while it waits: their task waits too, under
    their own key. The agent's decision ends the agent's task only."""
    task, approval = await _paused(orders)
    asked = await _rpc(
        orders["person"], "SendMessage", _message({"text": "hello"}, contextId=task["contextId"])
    )
    persons = asked["result"]["task"]
    assert persons["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert _data_parts(persons, "error") == [{"type": "error", "code": "approval_pending"}]
    await _rpc(
        orders["concierge"],
        "SendMessage",
        _message(_decision(approval), contextId=task["contextId"]),
    )
    waited = (await _rpc(orders["concierge"], "GetTask", {"id": task["id"]}))["result"]
    assert waited["status"]["state"] == "TASK_STATE_COMPLETED"
    left = (await _rpc(orders["person"], "GetTask", {"id": persons["id"]}))["result"]
    assert left["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
