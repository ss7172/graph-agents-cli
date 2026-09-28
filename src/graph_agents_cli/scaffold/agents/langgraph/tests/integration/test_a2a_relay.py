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

"""An agent asks another for the user, and relays the user's approval (end to end).

One app plays both agents, in process: the concierge (alice talks to it over
`/chat` with her own token; its tools are `peer_tools`, calling the other agent
over the ASGI transport, with a token the fake issuer mints in exchange for
alice's) and orders (its A2A endpoint, and a `cancelOrder` call to a backend
that records every request, gated so that the concierge may relay alice's
decision). The loop check's idea of "this agent" is pinned to the concierge
(both agents are one process here).

* The relay: alice asks the concierge to cancel; orders pauses for approval;
  alice approves at the concierge, seeing orders' call as the `effect`; the
  decision reaches orders once, the order is cancelled once, orders records
  `decided_via: concierge`, its waiting task follows, and the token was
  exchanged once for the pair (alice, orders).
* With orders' gate at `decide_with: direct`, the concierge relays nothing: it
  says alice must approve at orders, and she does, with her own token there.

Every test runs with the in-memory checkpointer, and on Postgres when
`TEST_POSTGRES_DSN` is set.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from collections.abc import AsyncIterator, Iterator
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
import jwt
import pytest
from fake_issuer import FakeIssuer
from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from {{cookiecutter.agent_directory}}.app_utils import token_exchange
from {{cookiecutter.agent_directory}}.app_utils.a2a_client import (
    context_id_for,
    peer_tools,
    reset_a2a_client,
)
from {{cookiecutter.agent_directory}}.app_utils.api_client import get_client
from {{cookiecutter.agent_directory}}.app_utils.api_client import (
    reset_policy_cache as reset_api_policy,
)
from {{cookiecutter.agent_directory}}.app_utils.auth import reset_policy_cache
from {{cookiecutter.agent_directory}}.fast_api_app import app

A2A_PATH = "/a2a/{{cookiecutter.agent_directory}}"
SECRET = "s3cret"

POLICY = """
apis:
  orders_agent:
    description: "Orders agent: cancels the caller's orders after approval."
    protocol: a2a
    a2a: {path: /a2a/{{cookiecutter.agent_directory}}}
    base_url_env: ORDERS_AGENT_URL
    auth: exchange
    exchange: {audience: orders}
    allowed_methods: [GET, POST]
    allowed_operations:
      - operationId: getAgentCard
        methods: [GET]
        path: /a2a/{{cookiecutter.agent_directory}}/.well-known/agent-card.json
      - {rpc_method: SendMessage, methods: [POST], path: /a2a/{{cookiecutter.agent_directory}}}
      - {rpc_method: GetTask, methods: [POST], path: /a2a/{{cookiecutter.agent_directory}}}
      - {operationId: listContextApprovals, methods: [GET], path: "/threads/{context_id}/approvals"}
    approval:
      - required_for: {operations: [{a2a_operation: approve}]}
        approvers: [requester]
        timeout_s: 900
    limits: {max_calls_per_run: 12, max_response_bytes: 1048576}
    timeouts_ms: {connect: 2000, read: 120000}
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
"""
RELAYED = "      decide_with: relayed\n      relayers: [concierge]\n"
PEERS = {
    "orders": {
        "api": "orders_agent",
        "approvals": "relay",
        "description": "Orders agent: cancels the caller's orders after approval.",
    }
}

# What the backend received.
SENT: list[httpx.Request] = []


def _backend(request: httpx.Request) -> httpx.Response:
    SENT.append(request)
    return httpx.Response(200, json={"cancelled": request.url.path})


@tool
async def cancel_order(order_id: int, runtime: ToolRuntime[Any]) -> str:
    """Cancel an order by its number."""
    context = getattr(runtime, "context", None)
    client = get_client("shop", context=context, transport=httpx.MockTransport(_backend))
    data = await client.post(
        "/orders/{order_id}/cancel",
        operation_id="cancelOrder",
        path_params={"order_id": order_id},
        json_body={"reason": "the customer asked"},
    )
    return json.dumps(data)


@pytest.fixture(scope="module")
def issuer() -> Iterator[FakeIssuer]:
    server = FakeIssuer({"concierge": (SECRET, {"orders"})})
    try:
        yield server
    finally:
        server.close()


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


async def _agents(
    issuer: FakeIssuer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_test_tools: Any,
    decide: str,
) -> AsyncIterator[dict[str, Any]]:
    policy = tmp_path / "api-policy.yaml"
    policy.write_text(POLICY + decide, encoding="utf-8")
    for name, value in {
        "API_POLICY_PATH": str(policy),
        "SHOP_API_BASE_URL": "http://shop.test",
        "ORDERS_AGENT_URL": "http://testserver",
        "AUTH_POLICY": "jwt",
        "AUTH_JWT_JWKS_URL": issuer.jwks_url,
        "AUTH_JWT_ISSUER": issuer.issuer,
        "AUTH_JWT_AUDIENCE": "concierge,orders",
        "AUTH_ALLOWED_ACTORS": "concierge",
        "TOKEN_EXCHANGE_URL": issuer.token_url,
        "TOKEN_EXCHANGE_CLIENT_ID": "concierge",
        "TOKEN_EXCHANGE_CLIENT_SECRET": SECRET,
    }.items():
        monkeypatch.setenv(name, value)
    for name in ("AUTH_JWT_PUBLIC_KEY", "TRACE_CAPTURE", "PRINCIPAL_HASH_SALT", "A2A_NAME"):
        monkeypatch.delenv(name, raising=False)
    # One process plays both agents: "this agent", for the loop check, is the concierge.
    monkeypatch.setattr(token_exchange, "own_names", lambda env=None: {"concierge"})
    reset_policy_cache()
    reset_api_policy()
    reset_a2a_client()
    token_exchange.reset_token_exchange()
    SENT.clear()
    issuer.requests.clear()
    user = f"alice-{uuid.uuid4().hex[:8]}"
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        use_test_tools(*reversed(peer_tools(PEERS, transport=transport)), cancel_order)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", timeout=60
        ) as client:
            started = asyncio.get_running_loop().time()
            while (await client.get("/ready")).status_code != 200:  # postgres: the schema
                assert asyncio.get_running_loop().time() - started < 30, "not ready"
                await asyncio.sleep(0.1)
            yield {
                "http": client,
                "issuer": issuer,
                "at_concierge": issuer.login(user, audience="concierge"),
                "at_orders": issuer.login(user, audience="orders"),
            }
    reset_policy_cache()
    reset_api_policy()
    reset_a2a_client()


@pytest.fixture
async def relayed(
    database: str,
    issuer: FakeIssuer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_test_tools: Any,
) -> AsyncIterator[dict[str, Any]]:
    """orders' cancel gate relays the concierge's decisions (`decide_with: relayed`)."""
    async for agents in _agents(issuer, tmp_path, monkeypatch, use_test_tools, RELAYED):
        yield agents


@pytest.fixture
async def direct(
    database: str,
    issuer: FakeIssuer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_test_tools: Any,
) -> AsyncIterator[dict[str, Any]]:
    """orders' cancel gate at the default, `decide_with: direct`."""
    async for agents in _agents(issuer, tmp_path, monkeypatch, use_test_tools, ""):
        yield agents


def _events(text: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for block in text.split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines and "data" in lines:
            events.append((lines["event"], json.loads(lines["data"])))
    return events


async def _chat(agents: dict[str, Any], message: str, thread_id: str | None = None) -> Any:
    body: dict[str, Any] = {"message": message}
    if thread_id:
        body["thread_id"] = thread_id
    r = await agents["http"].post(
        "/chat", json=body, headers={"Authorization": f"Bearer {agents['at_concierge']}"}
    )
    assert r.status_code == 200, r.text
    return _events(r.text)


def _end(events: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    ends = [data for event, data in events if event == "message.end"]
    assert ends, events
    return ends[-1]


def _results(events: list[tuple[str, dict[str, Any]]]) -> str:
    return json.dumps([data for event, data in events if event == "tool.result"])


async def _ask(agents: dict[str, Any], status: str) -> tuple[str, str]:
    """alice asks the concierge to cancel; orders waits for approval. (thread, orders task)"""
    events = await _chat(agents, "Ask orders about cancelling the order")
    end = _end(events)
    assert end["status"] == "ok", events
    assert f'\\"status\\": \\"{status}\\"' in _results(events), _results(events)
    found = re.search(r'\\"task_id\\": \\"([0-9a-f-]+)\\"', _results(events))
    assert found, _results(events)
    assert SENT == []
    return end["thread_id"], found.group(1)


async def test_the_concierge_relays_the_persons_approval(relayed: dict[str, Any]) -> None:
    agents = relayed
    thread, task_id = await _ask(agents, "needs_user_approval")
    # alice asks the concierge to approve: it pauses for her, showing what happens at orders.
    paused = _end(await _chat(agents, f"approve_agent_action for {task_id}", thread))
    assert paused["status"] == "awaiting_approval"
    approval = paused["approval"]
    assert (approval["api"], approval["a2a_operation"]) == ("orders_agent", "approve")
    effect = approval["effect"]
    assert (effect["agent"], effect["via"], effect["method"], effect["path"]) == (
        "orders",
        ["orders"],
        "POST",
        "/orders/1/cancel",
    )
    assert approval["nested"]["decide_with"] == "relayed" and SENT == []
    # She approves: the decision reaches orders once, and the order is cancelled once.
    r = await agents["http"].post(
        f"/threads/{thread}/approvals/{approval['approval_id']}",
        json={"decision": "approve"},
        headers={"Authorization": f"Bearer {agents['at_concierge']}"},
    )
    assert r.status_code == 200, r.text
    resumed = _events(r.text)
    assert _end(resumed)["status"] == "ok", resumed
    assert "cancelled" in _results(resumed)
    assert [(s.method, s.url.path) for s in SENT] == [("POST", "/orders/1/cancel")]
    # At orders (alice's own token): its approval names the concierge as the relayer, and the
    # task that waited follows the run the decision resumed.
    subject = jwt.decode(agents["at_orders"], options={"verify_signature": False})["sub"]
    context = context_id_for(thread, "orders_agent", subject)
    mine = {"Authorization": f"Bearer {agents['at_orders']}"}
    [record] = (await agents["http"].get(f"/threads/{context}/approvals", headers=mine)).json()
    assert (record["status"], record["decided_via"], record["requester_actor"]) == (
        "approved",
        "concierge",
        "concierge",
    )
    got = await agents["http"].post(
        A2A_PATH,
        json={"jsonrpc": "2.0", "id": "1", "method": "GetTask", "params": {"id": task_id}},
        headers={**mine, "A2A-Version": "1.0"},
    )
    waited = got.json()["result"]
    assert waited["status"]["state"] == "TASK_STATE_COMPLETED"
    assert waited["status"]["message"]["parts"][0]["text"].startswith("Continued in task ")
    # Exchanged once for the pair (alice, orders), then reused (the card, the ask, the reads
    # and the decision all went out with it).
    exchanges = [r for r in agents["issuer"].requests if r["form"].get("audience") == "orders"]
    assert len(exchanges) == 1


async def test_under_direct_the_concierge_relays_nothing_and_alice_approves_at_orders(
    direct: dict[str, Any],
) -> None:
    agents = direct
    thread, task_id = await _ask(agents, "needs_direct_approval")
    events = await _chat(agents, f"approve_agent_action for {task_id}", thread)
    assert _end(events)["status"] == "ok"  # no approval asked at the concierge
    result = _results(events)
    assert '\\"status\\": \\"needs_direct_approval\\"' in result
    assert "/orders/1/cancel" in result and SENT == []
    subject = jwt.decode(agents["at_orders"], options={"verify_signature": False})["sub"]
    context = context_id_for(thread, "orders_agent", subject)
    mine = {"Authorization": f"Bearer {agents['at_orders']}"}
    [record] = (await agents["http"].get(f"/threads/{context}/approvals", headers=mine)).json()
    r = await agents["http"].post(
        f"/threads/{context}/approvals/{record['approval_id']}",
        json={"decision": "approve"},
        headers=mine,
    )
    assert r.status_code == 200, r.text
    assert [(s.method, s.url.path) for s in SENT] == [("POST", "/orders/1/cancel")]
