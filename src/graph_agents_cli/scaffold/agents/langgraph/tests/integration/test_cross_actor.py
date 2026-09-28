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

"""Agents calling this agent for a user: one agent never reaches or decides another's work.

This agent plays `orders`, behind the `jwt` policy, with a gated `cancelOrder`
call to a backend that records every request (`httpx.MockTransport`). A fake
issuer (`fake_issuer.py`: a JWKS and RFC 8693 token exchange with nested `act`)
mints alice's own token and exchanges it for the agents that call `orders` for
her: `concierge`, and `billing` (called by concierge). The tests send what
those agents would, with their exchanged tokens, over A2A JSON-RPC and HTTP.

* The concierge's request pauses at the gated cancel. Billing, acting for the
  same user, gets GetTask -32001, ListTasks with no row, and its decisions are
  refused; the backend receives no POST.
* With `decide_with: relayed` naming the concierge, the concierge delivers the
  person's decision (naming the approval's digest): the call is sent once and
  the approval records `decided_via`.
* With `decide_with: direct` (the default), the concierge's relay is refused
  with `approval_direct_only`, and alice's own token decides.

Every test runs with the in-memory checkpointer, and on Postgres when
`TEST_POSTGRES_DSN` is set.
"""

from __future__ import annotations

import asyncio
import json
import os
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
import pytest
from fake_issuer import FakeIssuer
from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from {{cookiecutter.agent_directory}}.app_utils.api_client import get_client
from {{cookiecutter.agent_directory}}.app_utils.api_client import (
    reset_policy_cache as reset_api_policy,
)
from {{cookiecutter.agent_directory}}.app_utils.auth import reset_policy_cache
from {{cookiecutter.agent_directory}}.fast_api_app import app

A2A_PATH = "/a2a/{{cookiecutter.agent_directory}}"
AUDIENCE = "orders"
PROMPT = "Cancel the order for 7"
SECRET = "s3cret"

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
"""
RELAYED = "      decide_with: relayed\n      relayers: [concierge]\n"

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
        json_body={"reason": "customer asked"},
    )
    return json.dumps(data)


@pytest.fixture(scope="module")
def issuer() -> Iterator[FakeIssuer]:
    server = FakeIssuer(
        {
            "concierge": (SECRET, {"billing", AUDIENCE}),
            "billing": (SECRET, {AUDIENCE}),
        }
    )
    try:
        yield server
    finally:
        server.close()


def _exchange(issuer: FakeIssuer, client: str, subject_token: str, audience: str) -> str:
    """What `client` gets from the issuer for `subject_token` (RFC 8693), for `audience`."""
    answer = httpx.post(
        issuer.token_url,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": subject_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "audience": audience,
        },
        auth=(client, SECRET),
        timeout=10,
    )
    assert answer.status_code == 200, answer.text
    return str(answer.json()["access_token"])


@pytest.fixture
def tokens(issuer: FakeIssuer) -> dict[str, str]:
    """alice's own token at orders, and the tokens her agents present there for her."""
    user = f"alice-{uuid.uuid4().hex[:8]}"  # the A2A task store lives as long as the app
    at_concierge = issuer.login(user, audience="concierge")
    concierge = _exchange(issuer, "concierge", at_concierge, AUDIENCE)
    at_billing = _exchange(issuer, "concierge", at_concierge, "billing")
    billing = _exchange(issuer, "billing", at_billing, AUDIENCE)  # act: billing <- concierge
    return {
        "alice": issuer.login(user, audience=AUDIENCE),
        "concierge": concierge,
        "billing": billing,
    }


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


async def _orders(
    issuer: FakeIssuer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_test_tools: Any,
    decide: str,
) -> AsyncIterator[httpx.AsyncClient]:
    policy = tmp_path / "api-policy.yaml"
    policy.write_text(POLICY + decide, encoding="utf-8")
    for name, value in {
        "API_POLICY_PATH": str(policy),
        "SHOP_API_BASE_URL": "http://shop.test",
        "AUTH_POLICY": "jwt",
        "AUTH_JWT_JWKS_URL": issuer.jwks_url,
        "AUTH_JWT_ISSUER": issuer.issuer,
        "AUTH_JWT_AUDIENCE": AUDIENCE,
        "AUTH_ALLOWED_ACTORS": "concierge,billing",
    }.items():
        monkeypatch.setenv(name, value)
    for name in ("AUTH_JWT_PUBLIC_KEY", "AUTH_READ_ACROSS_ROLES", "TRACE_CAPTURE"):
        monkeypatch.delenv(name, raising=False)
    reset_policy_cache()
    reset_api_policy()
    SENT.clear()
    async with app.router.lifespan_context(app):
        use_test_tools(cancel_order)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", timeout=30
        ) as client:
            started = asyncio.get_running_loop().time()
            while (await client.get("/ready")).status_code != 200:  # postgres: the schema
                assert asyncio.get_running_loop().time() - started < 30, "not ready"
                await asyncio.sleep(0.1)
            yield client
    reset_policy_cache()
    reset_api_policy()


@pytest.fixture
async def relayed(
    database: str,
    issuer: FakeIssuer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_test_tools: Any,
) -> AsyncIterator[httpx.AsyncClient]:
    """orders, its cancel gate opted into `decide_with: relayed` by the concierge."""
    async for client in _orders(issuer, tmp_path, monkeypatch, use_test_tools, RELAYED):
        yield client


@pytest.fixture
async def direct(
    database: str,
    issuer: FakeIssuer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_test_tools: Any,
) -> AsyncIterator[httpx.AsyncClient]:
    """orders, its cancel gate at the default `decide_with: direct`."""
    async for client in _orders(issuer, tmp_path, monkeypatch, use_test_tools, ""):
        yield client


async def _rpc(client: httpx.AsyncClient, token: str, method: str, params: dict) -> dict:
    r = await client.post(
        A2A_PATH,
        json={"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": params},
        headers={"Authorization": f"Bearer {token}", "A2A-Version": "1.0"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _message(task: dict[str, Any] | None = None, **part: Any) -> dict[str, Any]:
    fields = {"taskId": task["id"], "contextId": task["contextId"]} if task else {}
    return {
        "message": {
            "messageId": uuid.uuid4().hex,
            "role": "ROLE_USER",
            "parts": [part],
            **fields,
        }
    }


def _approval_part(task: dict[str, Any]) -> dict[str, Any]:
    data = next(p["data"] for p in task["status"]["message"]["parts"] if "data" in p)
    assert data["type"] == "approval_request"
    return data["approval"]


def _note(task: dict[str, Any]) -> str:
    return "".join(p.get("text", "") for p in task["status"]["message"]["parts"])


async def _paused_by_concierge(client: httpx.AsyncClient, tokens: dict[str, str]) -> Any:
    sent = await _rpc(client, tokens["concierge"], "SendMessage", _message(text=PROMPT))
    task = sent["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED", task
    approval = _approval_part(task)
    assert approval["path"] == "/orders/7/cancel" and approval["status"] == "pending"
    assert approval["requester_actor"] == "concierge"
    assert approval["digest"].startswith("sha256:")
    assert SENT == []
    return task, approval


def _decision(approval: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"data": {"approval_id": approval["approval_id"], "decision": "approve", **extra}}


async def test_another_agent_for_the_same_user_cannot_reach_or_decide(
    relayed: httpx.AsyncClient, tokens: dict[str, str]
) -> None:
    client = relayed
    task, approval = await _paused_by_concierge(client, tokens)
    billing = tokens["billing"]
    # Billing's token: the task is not found, not listed, not continued, not decided.
    got = await _rpc(client, billing, "GetTask", {"id": task["id"]})
    assert got["error"]["code"] == -32001
    listed = await _rpc(client, billing, "ListTasks", {})
    assert listed["result"].get("tasks", []) == []
    decided = await _rpc(
        client, billing, "SendMessage", _message(task, **_decision(approval, digest="x"))
    )
    assert decided["error"]["code"] == -32001
    on_context = await _rpc(
        client,
        billing,
        "SendMessage",
        {"message": {**_message(**_decision(approval))["message"], "contextId": task["contextId"]}},
    )
    assert on_context["result"]["task"]["status"]["state"] == "TASK_STATE_FAILED"
    assert "another principal" in _note(on_context["result"]["task"])
    http = await client.post(
        f"/threads/{task['contextId']}/approvals/{approval['approval_id']}",
        json={"decision": "approve", "digest": approval["digest"]},
        headers={"Authorization": f"Bearer {billing}"},
    )
    assert http.status_code == 403 and http.json()["code"] == "not_an_approver"
    seen = await client.get(
        f"/threads/{task['contextId']}/approvals", headers={"Authorization": f"Bearer {billing}"}
    )
    assert seen.status_code == 403
    # The order is unchanged: the backend received nothing.
    assert SENT == []
    # The concierge's own relay, naming the digest the person approved, is sent once.
    missing = await _rpc(
        client, tokens["concierge"], "SendMessage", _message(task, **_decision(approval))
    )
    assert missing["result"]["task"]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert "approval_digest_mismatch" in _note(missing["result"]["task"])
    assert SENT == []
    done = await _rpc(
        client,
        tokens["concierge"],
        "SendMessage",
        _message(task, **_decision(approval, digest=approval["digest"])),
    )
    assert done["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED", done
    assert [(r.method, r.url.path) for r in SENT] == [("POST", "/orders/7/cancel")]
    # The person sees who relayed it.
    listed = await client.get(
        f"/threads/{task['contextId']}/approvals",
        headers={"Authorization": f"Bearer {tokens['alice']}"},
    )
    [record] = listed.json()
    assert (record["status"], record["decided_via"], record["requester_actor"]) == (
        "approved",
        "concierge",
        "concierge",
    )
    again = await _rpc(
        client,
        tokens["concierge"],
        "SendMessage",
        _message(task, **_decision(approval, digest=approval["digest"])),
    )
    assert "error" in again or again["result"]["task"]["status"]["state"] != "TASK_STATE_COMPLETED"
    assert len(SENT) == 1


async def test_under_direct_the_relay_is_refused_and_the_person_decides(
    direct: httpx.AsyncClient, tokens: dict[str, str]
) -> None:
    client = direct
    task, approval = await _paused_by_concierge(client, tokens)
    assert approval["decide_with"] == "direct"
    relayed = await _rpc(
        client,
        tokens["concierge"],
        "SendMessage",
        _message(task, **_decision(approval, digest=approval["digest"])),
    )
    refused = relayed["result"]["task"]
    assert refused["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert "approval_direct_only" in _note(refused)
    assert "not relayed by agent concierge" in _note(refused)
    http = await client.post(
        f"/threads/{task['contextId']}/approvals/{approval['approval_id']}",
        json={"decision": "approve", "digest": approval["digest"]},
        headers={"Authorization": f"Bearer {tokens['concierge']}"},
    )
    assert http.status_code == 403 and http.json()["code"] == "approval_direct_only"
    assert SENT == []
    # alice, at orders with her own token, sees the approval her agent's run asked for ...
    seen = await client.get(
        f"/threads/{task['contextId']}/approvals",
        headers={"Authorization": f"Bearer {tokens['alice']}"},
    )
    assert [a["approval_id"] for a in seen.json()] == [approval["approval_id"]]
    # ... and decides it; a digest from another view of the call is refused first.
    stale = await client.post(
        f"/threads/{task['contextId']}/approvals/{approval['approval_id']}",
        json={"decision": "approve", "digest": "sha256:" + "0" * 64},
        headers={"Authorization": f"Bearer {tokens['alice']}"},
    )
    assert stale.status_code == 409 and stale.json()["code"] == "approval_digest_mismatch"
    decided = await client.post(
        f"/threads/{task['contextId']}/approvals/{approval['approval_id']}",
        json={"decision": "approve"},
        headers={"Authorization": f"Bearer {tokens['alice']}"},
    )
    assert decided.status_code == 200, decided.text
    assert "message.end" in decided.text
    assert [(r.method, r.url.path) for r in SENT] == [("POST", "/orders/7/cancel")]
    [record] = (
        await client.get(
            f"/threads/{task['contextId']}/approvals",
            headers={"Authorization": f"Bearer {tokens['alice']}"},
        )
    ).json()
    assert (record["status"], record["decided_via"]) == ("approved", None)


async def test_an_unlisted_agent_is_refused_before_anything_runs(
    direct: httpx.AsyncClient, tokens: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_ALLOWED_ACTORS", "concierge")
    r = await direct.post(
        A2A_PATH,
        json={
            "jsonrpc": "2.0",
            "id": "1",
            "method": "SendMessage",
            "params": _message(text=PROMPT),
        },
        headers={"Authorization": f"Bearer {tokens['billing']}", "A2A-Version": "1.0"},
    )
    assert r.status_code == 403
    assert r.json() == {
        "detail": "Delegated caller billing is not allowed here (AUTH_ALLOWED_ACTORS)."
    }
    assert SENT == []
