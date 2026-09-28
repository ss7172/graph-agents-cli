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

"""Calls to JSON-RPC APIs (`protocol: jsonrpc|a2a`): what the policy client reads from the body.

The client derives what a request is from the JSON it sends, never from the
tool's labels: its JSON-RPC method and, for a message to another agent, whether
it approves or rejects a pending approval there. Every refusal happens before
anything is sent. Requests go to an httpx MockTransport; nothing leaves the
process. (The rules themselves are tested through both copies of the shared
block in graph-agents-cli's own suite.)
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

os.environ.setdefault("MODEL_PROVIDER", "fake")

import httpx
import pytest
from langchain.agents import create_agent
from langchain.tools import ToolRuntime
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from {{cookiecutter.agent_directory}}.app_utils.api_client import (
    A2A_ORIGIN_EXTENSION,
    APPROVAL_INTERRUPT,
    ApiCallError,
    ApiClient,
    ApiPolicy,
    ApiPolicyError,
    BoundApproval,
    call_identity,
    get_client,
    propagates,
    reset_limits,
    reset_policy_cache,
    set_approval_ledger,
    set_outbound_headers,
)
from {{cookiecutter.agent_directory}}.app_utils.approvals import (
    APPROVED,
    REJECTED,
    ApprovalRecord,
    ApprovalStore,
    approval_digest,
    approval_view,
    decision_value,
    record_from_interrupt,
)
from {{cookiecutter.agent_directory}}.app_utils.auth import Principal
from {{cookiecutter.agent_directory}}.app_utils.db import Database

POLICY = """
apis:
  orders_agent:
    description: "Orders agent: reads and cancels the caller's orders."
    protocol: a2a
    a2a: {path: /a2a/orders}
    base_url_env: ORDERS_AGENT_URL
    auth: bearer
    token_env: ORDERS_AGENT_KEY
    allowed_methods: [GET, POST]
    allowed_operations:
      - {operationId: getAgentCard, methods: [GET], path: /a2a/orders/.well-known/agent-card.json}
      - {rpc_method: SendMessage, methods: [POST], path: /a2a/orders}
      - {rpc_method: GetTask, methods: [POST], path: /a2a/orders}
      - {operationId: getTask, rpc_method: GetTask}
    approval:
      required_for: {operations: [{a2a_operation: approve}]}
      approvers: [requester]
  billing_agent:
    protocol: a2a
    a2a: {path: /a2a/billing}
    base_url_env: BILLING_AGENT_URL
    auth: bearer
    token_env: BILLING_AGENT_KEY
    allowed_methods: [POST]
    denied_operations:
      - {a2a_operation: approve}
  ledger:
    protocol: jsonrpc
    base_url_env: LEDGER_URL
    auth: none
    allowed_methods: [POST]
    allowed_operations:
      - {rpc_method: balance, path: /rpc}
  plain:
    base_url_env: PLAIN_URL
    auth: none
    allowed_methods: [POST]
"""


@pytest.fixture
def policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    # Agents are reached at cluster-internal names (plain http is refused elsewhere outside dev).
    monkeypatch.delenv("APP_ENV", raising=False)
    path = tmp_path / "api-policy.yaml"
    path.write_text(POLICY, encoding="utf-8")
    monkeypatch.setenv("API_POLICY_PATH", str(path))
    monkeypatch.setenv("ORDERS_AGENT_URL", "http://orders-agent:8080")
    monkeypatch.setenv("ORDERS_AGENT_KEY", "orders-key")
    monkeypatch.setenv("BILLING_AGENT_URL", "http://billing-agent.billing.svc")
    monkeypatch.setenv("BILLING_AGENT_KEY", "billing-key")
    monkeypatch.setenv("LEDGER_URL", "http://ledger.test")
    monkeypatch.setenv("PLAIN_URL", "http://plain.test")
    reset_policy_cache()
    reset_limits()
    yield path
    reset_policy_cache()
    reset_limits()


SENT: list[httpx.Request] = []


def _transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        SENT.append(request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": "1", "result": {}})

    SENT.clear()
    return httpx.MockTransport(handler)


def _client(api: str) -> ApiClient:
    return get_client(api, transport=_transport())


def _request(method: str, params: Any = None, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": "1", "method": method}
    if params is not None:
        body["params"] = params
    body.update(extra)
    return body


def _message(*parts: Any) -> dict[str, Any]:
    return {"message": {"role": "ROLE_USER", "messageId": "m1", "parts": list(parts)}}


def _decision(decision: str) -> dict[str, Any]:
    return {"data": {"approval_id": "a1", "decision": decision}}


ASK = _request("SendMessage", _message({"text": "Which orders are open?"}))
APPROVE = _request("SendMessage", _message(_decision("approve")))
REJECT = _request("SendMessage", _message(_decision("reject")))
GET_TASK = _request("GetTask", {"id": "t1"})


async def test_the_calls_the_policy_allows_are_sent(policy: Path) -> None:
    client = _client("orders_agent")
    await client.post("/a2a/orders", json_body=ASK)
    await client.post("/a2a/orders", json_body=GET_TASK)
    await client.post("/a2a/orders", json_body=_request("tasks/get", {"id": "t1"}))
    await client.post("/a2a/orders", operation_id="getTask", json_body=GET_TASK)
    await client.get("/a2a/orders/.well-known/agent-card.json", operation_id="getAgentCard")
    # A rejection is not gated: the agent that asked is told at once.
    await client.post("/a2a/orders", json_body=REJECT)
    assert [(r.method, r.url.path) for r in SENT] == [("POST", "/a2a/orders")] * 4 + [
        ("GET", "/a2a/orders/.well-known/agent-card.json"),
        ("POST", "/a2a/orders"),
    ]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            _request("CancelTask", {"id": "t1"}),
            "orders_agent: POST /a2a/orders (rpc_method CancelTask) refused by the API policy: "
            "not in allowed_operations.",
        ),
        (
            _request("tasks/cancel", {"id": "t1"}),
            "orders_agent: POST /a2a/orders (rpc_method CancelTask) refused by the API policy: "
            "not in allowed_operations.",
        ),
        (
            [GET_TASK],
            "orders_agent: protocol a2a sends one JSON-RPC request per call (a batch or "
            "non-request body refused: a batch).",
        ),
        (
            {"jsonrpc": "2.0", "method": "GetTask", "params": {"id": "t1"}},
            "orders_agent: protocol a2a sends one JSON-RPC request per call (a batch or "
            "non-request body refused: a notification (no id)).",
        ),
        (
            {"decision": "approve", "approval_id": "a1"},
            "orders_agent: protocol a2a sends one JSON-RPC request per call (a batch or "
            "non-request body refused: members other than jsonrpc, method, params and id).",
        ),
    ],
)
async def test_what_the_policy_refuses_is_never_sent(
    policy: Path, body: Any, message: str, caplog: pytest.LogCaptureFixture
) -> None:
    client = _client("orders_agent")
    with caplog.at_level(logging.WARNING), pytest.raises(ApiPolicyError) as refused:
        await client.post("/a2a/orders", json_body=body)
    assert str(refused.value) == message
    assert SENT == []
    # The log names the rule, never the body.
    assert "api call refused" in caplog.text and "t1" not in caplog.text


async def test_a_read_carries_no_body(policy: Path) -> None:
    client = _client("orders_agent")
    with pytest.raises(ApiPolicyError, match="a GET sends no body"):
        await client.get("/a2a/orders/.well-known/agent-card.json", json_body={"x": 1})
    assert SENT == []


async def test_a_label_never_carries_a_request_past_its_rule(policy: Path) -> None:
    """T11: `getTask` names an entry pinning GetTask, so a message labelled so is refused."""
    client = _client("orders_agent")
    with pytest.raises(ApiPolicyError) as refused:
        await client.post("/a2a/orders", operation_id="getTask", json_body=APPROVE)
    assert str(refused.value) == (
        "orders_agent: operation_id 'getTask' does not match the request (rpc_method "
        "SendMessage); refused."
    )
    assert SENT == []


@pytest.mark.parametrize(
    "body",
    [
        APPROVE,
        _request("message/send", _message(_decision("approve"))),
        # Approve wins over a rejection in the same message.
        _request("SendMessage", _message(_decision("reject"), _decision("approve"))),
    ],
)
async def test_a_message_that_approves_waits_for_a_human(policy: Path, body: Any) -> None:
    """Gated for the requester, whatever its label or spelling: outside an agent run,
    nothing can pause and ask, so it is refused and nothing is sent."""
    client = _client("orders_agent")
    with pytest.raises(ApiPolicyError, match=r"needs human approval \(requester\)"):
        await client.post("/a2a/orders", operation_id="justAsking", json_body=body)
    assert SENT == []


async def test_a_denied_approve_is_refused_and_the_rest_goes_through(policy: Path) -> None:
    client = _client("billing_agent")
    with pytest.raises(ApiPolicyError) as refused:
        await client.post("/a2a/billing", json_body=APPROVE)
    assert str(refused.value) == (
        "billing_agent: POST /a2a/billing (rpc_method SendMessage, a2a_operation approve) refused "
        "by the API policy: denied by denied_operations (a2a_operation=approve)."
    )
    assert SENT == []
    await client.post("/a2a/billing", json_body=ASK)
    assert len(SENT) == 1


async def test_an_approve_no_rule_holds_is_refused_even_by_an_unchecked_policy(
    policy: Path,
) -> None:
    """The validator refuses such a policy; the client holds even one it never validated."""
    settings = {
        "protocol": "a2a",
        "a2a": {"path": "/a2a/orders"},
        "base_url_env": "ORDERS_AGENT_URL",
        "auth": "bearer",
        "token_env": "ORDERS_AGENT_KEY",
        "allowed_methods": ["POST"],
    }
    client = ApiClient(ApiPolicy(apis={"orders_agent": settings}), "orders_agent")
    client._transport = _transport()
    with pytest.raises(ApiPolicyError) as refused:
        await client.post("/a2a/orders", json_body=APPROVE)
    assert str(refused.value) == (
        "orders_agent: POST /a2a/orders (rpc_method SendMessage, a2a_operation approve) refused "
        "by the API policy: a message that approves must wait for an approval or be denied "
        "(gate or deny a2a_operation: approve)."
    )
    assert SENT == []
    await client.post("/a2a/orders", json_body=ASK)
    assert len(SENT) == 1


async def test_a_json_rpc_api_is_judged_by_its_method(policy: Path) -> None:
    client = _client("ledger")
    await client.post("/rpc", json_body=_request("balance", ["acct-1"]))
    with pytest.raises(ApiPolicyError, match=r"\(rpc_method transfer\) refused"):
        await client.post("/rpc", json_body=_request("transfer", ["acct-1", 5]))
    # No A2A reading on a plain JSON-RPC API: a 0.3 name is just a name.
    with pytest.raises(ApiPolicyError, match=r"\(rpc_method message/send\) refused"):
        await client.post("/rpc", json_body=_request("message/send", _message(_decision("x"))))
    assert len(SENT) == 1


async def test_an_http_api_sends_what_it_did_before(policy: Path) -> None:
    """No JSON-RPC reading on an `http` API: any body goes, as in 0.2."""
    client = _client("plain")
    await client.post("/anything", json_body=[APPROVE, {"not": "json-rpc"}])
    await client.post("/anything", operation_id="getTask", json_body=APPROVE)
    assert len(SENT) == 2


# --- which call a decision is bound to (call_identity) ---------------------------------------
#
# Every call to an A2A peer is a POST to one endpoint, so API, method and path cannot tell a
# relay's calls apart: the read it sends first on resume would take the decision its approve
# message waits for, and a rejection would stop the message that tells the peer. The request
# each body is (its JSON-RPC method and A2A decision) is part of which call it is.

ALICE = Principal(id="alice", roles=["user"], attributes={"tenant": "t1"})
# The relay tool reads the peer's task before it decides (as the A2A client will).
READ_FIRST: list[bool] = []
PEER_SENT: list[dict[str, Any]] = []
# The peer's approval the decision is about, as the A2A client copies it into the message.
APPROVING: list[dict[str, Any]] = []


def _decide(decision: str) -> dict[str, Any]:
    """The decision message, built the same on every run (the approval binds its body)."""
    message: dict[str, Any] = {
        "messageId": f"m-{decision}-a1",
        "role": "ROLE_USER",
        "contextId": "ctx-1",
        "parts": [{"data": {"approval_id": "a1", "decision": decision}}],
    }
    if APPROVING:
        message["metadata"] = {A2A_ORIGIN_EXTENSION: {"approving": APPROVING[0]}}
    return {
        "jsonrpc": "2.0",
        "id": f"{decision}-a1",
        "method": "SendMessage",
        "params": {"message": message},
    }


def _peer_upstream(request: httpx.Request) -> httpx.Response:
    PEER_SENT.append(json.loads(request.content))
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": "1", "result": {}})


@tool
async def relay_approval(task_id: str, runtime: ToolRuntime[Any]) -> str:
    """Relay the person's approval to the orders agent, or tell it they rejected it."""
    client = get_client("orders_agent", transport=httpx.MockTransport(_peer_upstream))
    if READ_FIRST:
        await client.post("/a2a/orders", json_body=_request("GetTask", {"id": task_id}))
    try:
        await client.post("/a2a/orders", json_body=_decide("approve"))
    except ApiPolicyError as exc:
        if "rejected" not in str(exc):
            raise
        await client.post("/a2a/orders", json_body=_decide("reject"))
        return "The person did not approve it; the orders agent was told."
    return "Approved and sent to the orders agent."


ADMIN_DSN = os.environ.get("TEST_POSTGRES_DSN", "")


@pytest.fixture(params=["memory", "file", "postgres"])
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[ApprovalStore]:
    """The ledger in memory, kept in a file (`langgraph dev`), and on Postgres when
    `TEST_POSTGRES_DSN` is set (a fresh database)."""
    if request.param == "memory":
        yield ApprovalStore(Database("memory"))
        return
    if request.param == "file":
        kept = ApprovalStore(Database("memory"), path=tmp_path / ".langgraph_api" / "a.json")
        assert await kept.load() == 0
        yield kept
        return
    if not ADMIN_DSN:
        pytest.skip("TEST_POSTGRES_DSN is not set")
    import psycopg

    name = f"gac_test_{uuid.uuid4().hex[:12]}"
    async with await psycopg.AsyncConnection.connect(ADMIN_DSN, autocommit=True) as admin:
        await admin.execute(f'CREATE DATABASE "{name}"')
    db = Database("postgres", urlsplit(ADMIN_DSN)._replace(path=f"/{name}").geturl())
    await db.open()
    try:
        yield ApprovalStore(db)
    finally:
        await db.close()
        async with await psycopg.AsyncConnection.connect(ADMIN_DSN, autocommit=True) as admin:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def relay(policy: Path, store: ApprovalStore) -> Iterator[Any]:
    """An agent with the relay tool, the fake model and the ledger `store`."""
    from {{cookiecutter.agent_directory}} import agent
    from {{cookiecutter.agent_directory}}.app_utils.model import get_model

    READ_FIRST.clear()
    PEER_SENT.clear()
    APPROVING.clear()
    set_approval_ledger(store)
    yield create_agent(
        model=get_model(),
        tools=[relay_approval],
        system_prompt=agent.SYSTEM_PROMPT,
        middleware=agent.middleware(),
        context_schema=agent.AgentContext,
        checkpointer=InMemorySaver(),
    )
    set_approval_ledger(None)


async def _run(graph: Any, thread: str, graph_input: Any) -> list[Any]:
    config = {"configurable": {"thread_id": thread}}
    async for _ in graph.astream(graph_input, config, stream_mode="updates"):
        pass
    return list((await graph.aget_state(config)).interrupts)


async def _pause(graph: Any, store: ApprovalStore) -> tuple[Any, ApprovalRecord]:
    interrupts = await _run(graph, "t1", {"messages": [("user", "Relay the approval for t1")]})
    assert len(interrupts) == 1
    record, _, _ = await store.add(
        record_from_interrupt(
            interrupts[0].value,
            interrupt_id=interrupts[0].id,
            thread_id="t1",
            run_id="r",
            requester=ALICE,
        )
    )
    return interrupts[0], record


async def _tool_result(graph: Any) -> str:
    state = await graph.aget_state({"configurable": {"thread_id": "t1"}})
    return next(m.content for m in reversed(state.values["messages"]) if m.type == "tool")


def _sent() -> list[str]:
    """What reached the peer: each request's method, and its decision if any."""
    return [
        " ".join(
            [body["method"]]
            + [
                p["data"]["decision"]
                for p in (body.get("params") or {}).get("message", {}).get("parts", [])
                if "data" in p
            ]
        )
        for body in PEER_SENT
    ]


async def test_the_approval_names_the_request_it_holds(relay: Any, store: ApprovalStore) -> None:
    interrupt, record = await _pause(relay, store)
    assert interrupt.value["type"] == APPROVAL_INTERRUPT
    assert (interrupt.value["rpc_method"], interrupt.value["a2a_operation"]) == (
        "SendMessage",
        "approve",
    )
    public = record.public()
    assert (public["rpc_method"], public["a2a_operation"]) == ("SendMessage", "approve")
    view = approval_view(record)
    assert (view["rpc_method"], view["a2a_operation"]) == ("SendMessage", "approve")
    assert record.display_digest == approval_digest(view)
    value = decision_value(record, "approve")
    assert (value["rpc_method"], value["a2a_operation"]) == ("SendMessage", "approve")
    assert _sent() == []


def _orders_approval(**overrides: Any) -> dict[str, Any]:
    """The orders agent's pending approval as its A2A task reports it (exact values)."""
    approving = {
        "agent": "orders",
        "approval_id": "de2e",
        "call": {
            "api": "orders_api",
            "method": "POST",
            "path": "/orders/ORD-1002/cancel",
            "operation_id": "cancelOrder",
            "query": {},
            "body": {"reason": "customer asked", "amount": 1, "big": 12345678901234567890},
        },
        "reason": "cancel_order: the customer asked",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "digest": "sha256:" + "a" * 64,
        "reported_by": "orders",
        "decide_with": "relayed",
        "nested": None,
        "unknown": "dropped",
    }
    approving.update(overrides)
    return approving


async def test_a_relayed_approval_shows_what_it_decides_and_what_will_happen(
    relay: Any, store: ApprovalStore
) -> None:
    """The approve message names the peer's approval (`approving`, copied from the peer):
    the person's approval shows it as `nested`, exact values kept, and the call that then
    happens as `effect` (orders' cancel, via orders)."""
    APPROVING.append(_orders_approval())
    interrupt, record = await _pause(relay, store)
    nested = interrupt.value["nested"]
    assert nested["call"]["body"] == {
        "reason": "customer asked",
        "amount": 1,
        "big": 12345678901234567890,
    }
    assert "unknown" not in nested and nested["nested"] is None
    effect = interrupt.value["effect"]
    assert (effect["agent"], effect["via"], effect["method"], effect["path"]) == (
        "orders",
        ["orders"],
        "POST",
        "/orders/ORD-1002/cancel",
    )
    public = record.public()
    assert public["nested"] == nested and public["effect"] == effect
    # Bound like the rest of the body: the digest the person approves covers it.
    assert record.display_digest == approval_digest(approval_view(record))
    assert _sent() == []


async def test_a_two_hop_relay_shows_the_innermost_call_and_the_agents_between(
    relay: Any, store: ApprovalStore
) -> None:
    inner = _orders_approval()
    APPROVING.append(
        _orders_approval(
            agent="billing",
            approval_id="b1",
            call={"api": "orders_agent", "method": "POST", "path": "/a2a/orders"},
            reported_by="billing",
            nested=inner,
        )
    )
    interrupt, _record = await _pause(relay, store)
    effect = interrupt.value["effect"]
    assert (effect["agent"], effect["via"], effect["path"]) == (
        "orders",
        ["billing", "orders"],
        "/orders/ORD-1002/cancel",
    )
    assert interrupt.value["nested"]["nested"]["approval_id"] == "de2e"


async def test_a_rejection_naming_the_approval_is_sent_without_waiting(policy: Path) -> None:
    """Only an approve message waits for the person: a rejection that carries the approval
    it is about goes out at once (rejecting is always safe)."""
    APPROVING[:] = [_orders_approval()]
    try:
        await _client("orders_agent").post("/a2a/orders", json_body=_decide("reject"))
    finally:
        APPROVING.clear()
    [sent] = SENT
    assert json.loads(sent.content)["params"]["message"]["parts"][0]["data"]["decision"] == "reject"


async def test_get_task_on_resume_does_not_consume_the_approve_decision(
    relay: Any, store: ApprovalStore
) -> None:
    """The resumed tool reads the task first: that read is its own call, and the approve
    message it then rebuilds takes the decision and is sent once."""
    READ_FIRST.append(True)
    interrupt, record = await _pause(relay, store)
    assert _sent() == ["GetTask"]
    decided = await store.decide(record.approval_id, APPROVED, "alice", None)
    await _run(relay, "t1", Command(resume={interrupt.id: decision_value(decided, "approve")}))
    assert _sent() == ["GetTask", "GetTask", "SendMessage approve"]
    assert "Approved and sent" in await _tool_result(relay)
    assert (await store.get(record.approval_id)).used_at is not None


async def test_reject_is_sent_after_a_rejection(relay: Any, store: ApprovalStore) -> None:
    """The rejection stops the approve message, not the message that tells the peer."""
    interrupt, record = await _pause(relay, store)
    decided = await store.decide(record.approval_id, REJECTED, "alice", "not this one")
    await _run(relay, "t1", Command(resume={interrupt.id: decision_value(decided, "reject")}))
    assert _sent() == ["SendMessage reject"]
    assert "the orders agent was told" in await _tool_result(relay)


async def test_bound_approvals_filter_by_operation(relay: Any, store: ApprovalStore) -> None:
    """A tool call run again without a decision (a thread continued without input): the
    ledger's approval holds the approve message only; the read goes out again."""
    READ_FIRST.append(True)
    _, record = await _pause(relay, store)
    [bound] = await store.bound_approvals(tool_call=(record.message_id, record.tool_call_id))
    assert (bound.rpc_method, bound.a2a_operation) == ("SendMessage", "approve")
    await _run(relay, "t1", None)
    assert _sent() == ["GetTask", "GetTask"]
    assert "was resumed without an approval decision" in await _tool_result(relay)


async def test_a_kept_approval_still_names_its_request(tmp_path: Path) -> None:
    """The ledger kept in a file (`langgraph dev`) reads the request back after a restart."""
    path = tmp_path / ".langgraph_api" / "a.json"
    value = {
        "type": APPROVAL_INTERRUPT,
        "api": "orders_agent",
        "method": "POST",
        "path": "/a2a/orders",
        "body": _decide("approve"),
        "tool_call_id": "c1",
        "message_id": "m1",
        "approvers": ["requester"],
        "call_hash": "h",
        "rpc_method": "SendMessage",
        "a2a_operation": "approve",
    }
    first = ApprovalStore(Database("memory"), path=path)
    await first.load()
    record, _, _ = await first.add(
        record_from_interrupt(value, interrupt_id="i1", thread_id="t1", run_id="r", requester=ALICE)
    )
    again = ApprovalStore(Database("memory"), path=path)
    assert await again.load() == 1
    [bound] = await again.bound_approvals(tool_call=("m1", "c1"))
    assert (bound.rpc_method, bound.a2a_operation) == ("SendMessage", "approve")
    assert (await again.get(record.approval_id)).display_digest == record.display_digest


def test_identity_unchanged_for_http_apis() -> None:
    """Three fields for a call to an `http` API, as in 0.2: nothing new in its record."""
    assert call_identity("shop", "post", "/Orders/7/") == ("shop", "POST", "/orders/7")
    assert call_identity("shop", "POST", "/orders/7", None, None) == ("shop", "POST", "/orders/7")
    assert call_identity(*BoundApproval("shop", "POST", "/orders/7", "pending", False)[:3]) == (
        "shop",
        "POST",
        "/orders/7",
    )
    read = call_identity("peer", "POST", "/a2a/x", "GetTask")
    approve = call_identity("peer", "POST", "/a2a/x", "SendMessage", "approve")
    reject = call_identity("peer", "POST", "/a2a/x", "SendMessage", "reject")
    assert len({read, approve, reject, call_identity("peer", "POST", "/a2a/x")}) == 4
    value = {
        "type": APPROVAL_INTERRUPT,
        "api": "shop",
        "method": "POST",
        "path": "/orders/7/cancel",
        "body": {"reason": "asked"},
        "approvers": ["requester"],
        "call_hash": "h",
    }
    record = record_from_interrupt(
        value, interrupt_id="i1", thread_id="t1", run_id="r", requester=ALICE
    )
    for shown in (record.payload, record.public(), decision_value(record, "approve")):
        assert "rpc_method" not in shown and "a2a_operation" not in shown
    view = approval_view(record)
    assert (view["rpc_method"], view["a2a_operation"]) == (None, None)
    # The digest of an http approval is what 0.3's first builds computed.
    assert record.display_digest == approval_digest(
        {
            "api": "shop",
            "method": "POST",
            "path": "/orders/7/cancel",
            "operation_id": None,
            "rpc_method": None,
            "a2a_operation": None,
            "query": {},
            "body": {"reason": "asked"},
        }
    )


# --- another agent is part of the same request (trace headers) and gets credentials over TLS ---

TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def test_an_a2a_peer_carries_the_correlation_headers_whatever_its_auth() -> None:
    """The owner's decision: request ids and trace context go to A2A peers and to the APIs
    that act for the user, never to other third parties."""
    assert propagates({"protocol": "a2a", "auth": "bearer"})
    assert propagates({"protocol": "a2a", "auth": "exchange"})
    assert not propagates({"protocol": "jsonrpc", "auth": "bearer"})
    assert not propagates({"protocol": "http", "auth": "none"})
    assert propagates({"auth": "forward"}) and not propagates({"auth": "bearer"})


async def test_the_correlation_headers_reach_the_peer_only(policy: Path) -> None:
    set_outbound_headers(lambda: {"X-Request-ID": "req-1", "traceparent": TRACEPARENT})
    try:
        await _client("orders_agent").post("/a2a/orders", json_body=ASK)
        peer = SENT[-1]
        await _client("ledger").post("/rpc", json_body=_request("balance", ["a"]))
        ledger = SENT[-1]
        await _client("plain").post("/anything", json_body={"x": 1})
        plain = SENT[-1]
    finally:
        set_outbound_headers(None)
    assert (peer.headers["x-request-id"], peer.headers["traceparent"]) == ("req-1", TRACEPARENT)
    for third_party in (ledger, plain):
        assert "x-request-id" not in third_party.headers
        assert "traceparent" not in third_party.headers


@pytest.mark.parametrize(
    "url",
    [
        "https://orders.example.com",
        "http://orders-agent:8080",
        "http://orders-agent.orders.svc",
        "http://orders-agent.orders.svc.cluster.local",
        "http://127.0.0.1:8001",
        "http://localhost:8001",
        "http://[::1]:8001",
    ],
)
async def test_a_peer_gets_credentials_over_https_or_inside_the_cluster(
    policy: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv("ORDERS_AGENT_URL", url)
    await _client("orders_agent").post("/a2a/orders", json_body=ASK)
    assert len(SENT) == 1


async def test_plain_http_to_a_peer_outside_dev_is_refused(
    policy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The transport rule (3.6), live now that `protocol: a2a` exists: the peer's credential
    never crosses a network in the clear outside APP_ENV=dev."""
    monkeypatch.setenv("ORDERS_AGENT_URL", "http://orders.example.com")
    with pytest.raises(ApiCallError) as refused:
        await _client("orders_agent").post("/a2a/orders", json_body=ASK)
    assert str(refused.value) == (
        "ORDERS_AGENT_URL must use https outside APP_ENV=dev to carry credentials; nothing was "
        "sent to API 'orders_agent'."
    )
    assert SENT == []
    monkeypatch.setenv("APP_ENV", "dev")
    await _client("orders_agent").post("/a2a/orders", json_body=ASK)
    assert len(SENT) == 1


async def test_other_apis_keep_their_transport(
    policy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-peer APIs are unchanged in 0.3 (a plain JSON-RPC API included)."""
    monkeypatch.setenv("LEDGER_URL", "http://ledger.example.com")
    monkeypatch.setenv("PLAIN_URL", "http://plain.example.com")
    await _client("ledger").post("/rpc", json_body=_request("balance", ["a"]))
    await _client("plain").post("/anything", json_body={"x": 1})
    assert len(SENT) == 1  # each _client() starts a new transport
