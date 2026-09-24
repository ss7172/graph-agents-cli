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

"""Approvals of gated API calls: the records, who decides, the ledger, and the client's side.

The client's side runs a real graph (the fake model, one gated tool, an
in-memory checkpointer) that pauses in LangGraph's `interrupt()` and resumes
with decisions made here, the ledger being an `ApprovalStore`. Every store
test runs in memory, and again on Postgres when `TEST_POSTGRES_DSN` is set
(see `tests/integration/test_postgres.py`).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
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

from {{cookiecutter.agent_directory}}.app_utils import api_client
from {{cookiecutter.agent_directory}}.app_utils.api_client import (
    APPROVAL_DECISION,
    APPROVAL_INTERRUPT,
    ApiPolicyError,
    call_hash,
    canonical_call,
    get_client,
    redact_fields,
    reset_policy_cache,
    set_approval_ledger,
    stated_purpose,
)
from {{cookiecutter.agent_directory}}.app_utils.approvals import (
    APPROVED,
    EXPIRED,
    PENDING,
    REJECTED,
    ApprovalRecord,
    ApprovalStore,
    decision_value,
    may_decide,
    may_view,
    record_from_interrupt,
    resume_principal,
    utcnow,
)
from {{cookiecutter.agent_directory}}.app_utils.auth import Principal
from {{cookiecutter.agent_directory}}.app_utils.db import Database

ALICE = Principal(
    id="alice", roles=["user"], attributes={"tenant": "t1", "credentials": {"x": "s"}}
)

POLICY = """
apis:
  shop:
    base_url_env: SHOP_API_BASE_URL
    auth: none
    allowed_methods: [GET, POST]
    approval:
      required_for:
        methods: [POST]
      approvers: [requester, "role:ops"]
      timeout_s: 60
"""


def _interrupt_value(**overrides: Any) -> dict[str, Any]:
    value = {
        "type": APPROVAL_INTERRUPT,
        "api": "shop",
        "method": "POST",
        "path": "/orders/7/cancel",
        "query": {"notify": "yes"},
        "body": {"reason": "asked"},
        "operation_id": "cancelOrder",
        "tool": "cancel_order",
        "tool_call_id": "call-1",
        "reason": "cancel_order: the customer asked",
        "approvers": ["requester", "role:ops"],
        "timeout_s": 60,
        "rule": "approval.required_for.methods ['POST']",
        "call_hash": "h1",
    }
    value.update(overrides)
    return value


def _record(**overrides: Any) -> ApprovalRecord:
    fields = {"interrupt_id": "i1", "thread_id": "t1", "run_id": "r1"}
    value_overrides = {k: v for k, v in overrides.items() if k not in fields}
    fields.update({k: v for k, v in overrides.items() if k in fields})
    return record_from_interrupt(_interrupt_value(**value_overrides), requester=ALICE, **fields)


ADMIN_DSN = os.environ.get("TEST_POSTGRES_DSN", "")


@pytest.fixture(params=["memory", "postgres"])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[ApprovalStore]:
    """The store in memory, and on Postgres when `TEST_POSTGRES_DSN` is set (a fresh database)."""
    if request.param == "memory":
        yield ApprovalStore(Database("memory"))
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


# --- records -------------------------------------------------------------------------


def test_a_record_holds_the_call_and_never_shows_its_internals() -> None:
    record = _record()
    assert record.status == PENDING
    assert record.expires_at - record.created_at == timedelta(seconds=60)
    assert record.requester_hash == ALICE.hashed_id()
    # The run context it resumes with: roles and public attributes, never credentials.
    assert record.requester_context == {"roles": ["user"], "attributes": {"tenant": "t1"}}
    public = record.public()
    assert public["body"] == {"reason": "asked"} and public["query"] == {"notify": "yes"}
    assert public["reason"] == "cancel_order: the customer asked"
    for internal in ("call_hash", "interrupt_id", "rule", "tool_call_id", "requester_context"):
        assert internal not in public
    hidden = record.public(include_call=False)
    assert "body" not in hidden and "query" not in hidden


@pytest.mark.parametrize(("given", "kept"), [(5, 30), (100_000, 86_400), ("x", 900), (True, 900)])
def test_the_timeout_is_kept_within_the_policy_bounds(given: Any, kept: int) -> None:
    record = _record(timeout_s=given)
    assert record.expires_at - record.created_at == timedelta(seconds=kept)


def test_a_pending_record_past_its_expiry_reads_as_expired() -> None:
    record = _record()
    assert record.effective_status(record.expires_at - timedelta(seconds=1)) == PENDING
    assert record.effective_status(record.expires_at) == EXPIRED


# --- who decides ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("principal", "approvers", "allowed"),
    [
        (Principal(id="alice"), ["requester"], True),
        (Principal(id="alice", roles=["ops"]), ["role:ops"], False),  # no self-approval
        (Principal(id="alice", roles=["ops"]), ["requester", "role:ops"], True),
        (Principal(id="carol", roles=["ops"]), ["role:ops"], True),
        (Principal(id="carol", roles=["ops"]), ["requester"], False),
        (Principal(id="bob", roles=["user"]), ["requester", "role:ops"], False),
        (Principal(id="bob", roles=["Ops", "ops-team"]), ["role:ops"], False),
        (Principal(id=""), ["requester"], False),
    ],
)
def test_who_may_decide(principal: Principal, approvers: list[str], allowed: bool) -> None:
    assert may_decide(principal, "alice", approvers) is allowed


def test_who_may_see(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_READ_ACROSS_ROLES", "auditor")
    assert may_view(Principal(id="alice"), "alice", ["role:ops"])  # the owner
    assert may_view(Principal(id="carol", roles=["ops"]), "alice", ["role:ops"])
    assert may_view(Principal(id="ann", roles=["auditor"]), "alice", ["role:ops"])
    assert not may_view(Principal(id="bob"), "alice", ["role:ops"])


def test_the_resumed_run_acts_as_the_requester_never_the_decider() -> None:
    record = _record()
    carol = Principal(id="carol", roles=["ops", "admin"], attributes={"credentials": {"x": "c"}})
    acting = resume_principal(record, "alice", carol)
    assert acting.id == "alice" and acting.roles == ["user"]
    assert acting.attributes == {"tenant": "t1"}  # no credentials: not stored, not carol's
    fresh = Principal(id="alice", roles=["user"], attributes={"credentials": {"x": "new"}})
    assert resume_principal(record, "alice", fresh) is fresh


# --- the store and the ledger -----------------------------------------------------------


async def test_an_interrupt_asked_again_keeps_its_pending_approval(store: ApprovalStore) -> None:
    first, created, superseded = await store.add(_record())
    assert created and superseded == 0
    again, created, superseded = await store.add(_record())
    assert not created and again.approval_id == first.approval_id and superseded == 0
    # The same interrupt asking for a different request supersedes the old one.
    changed, created, superseded = await store.add(_record(call_hash="h2"))
    assert created and superseded == 1 and changed.approval_id != first.approval_id
    assert (await store.get(first.approval_id)).status == EXPIRED


async def test_a_decision_is_taken_once(store: ApprovalStore) -> None:
    record, _, _ = await store.add(_record())
    results = await asyncio.gather(
        store.decide(record.approval_id, APPROVED, "a", None),
        store.decide(record.approval_id, REJECTED, "b", "no"),
    )
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert await store.decide(record.approval_id, APPROVED, "c", None) is None
    with pytest.raises(ValueError):
        await store.decide(record.approval_id, PENDING, "c", None)


async def test_decided_records_drop_the_call_unless_full_capture(
    store: ApprovalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, _, _ = await store.add(_record())
    decided = await store.decide(record.approval_id, REJECTED, "x", " wrong customer ")
    assert decided is not None and decided.comment == "wrong customer"
    assert "body" not in decided.payload and "query" not in decided.payload
    assert decided.payload["reason"] == "cancel_order: the customer asked"
    monkeypatch.setenv("TRACE_CAPTURE", "full")
    kept, _, _ = await store.add(_record(interrupt_id="i2"))
    decided = await store.decide(kept.approval_id, APPROVED, "x", None)
    assert decided is not None and decided.payload["body"] == {"reason": "asked"}


async def test_an_expired_approval_cannot_be_decided(store: ApprovalStore) -> None:
    record, _, _ = await store.add(_record())
    store._clock = lambda: record.expires_at + timedelta(seconds=1)
    assert await store.decide(record.approval_id, APPROVED, "x", None) is None
    assert [r.approval_id for r in await store.expire_due()] == [record.approval_id]
    assert (await store.get(record.approval_id)).status == EXPIRED
    assert await store.expire_due() == []


async def test_the_ledger_lets_an_approval_through_once(store: ApprovalStore) -> None:
    record, _, _ = await store.add(_record())
    assert (
        await store.consume(record.approval_id, "h1", "t1")
        == "the approval is pending, not approved"
    )
    await store.decide(record.approval_id, APPROVED, "x", None)
    assert await store.consume(record.approval_id, "h2", "t1") == (
        "the approval is for a different request"
    )
    assert await store.consume(record.approval_id, "h1", "t2") == (
        "the approval belongs to another thread"
    )
    assert await store.consume(record.approval_id, "h1", "t1") is None
    assert await store.consume(record.approval_id, "h1", "t1") == "the approval was already used"
    assert await store.consume("nope", "h1", "t1") == "no such approval"
    rejected, _, _ = await store.add(_record(interrupt_id="i3"))
    await store.decide(rejected.approval_id, REJECTED, "x", None)
    assert await store.consume(rejected.approval_id, "h1", "t1") == (
        "the approval is rejected, not approved"
    )


async def test_listing_across_threads(
    store: ApprovalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine, _, _ = await store.add(_record())
    other = record_from_interrupt(
        _interrupt_value(approvers=["role:finance"]),
        interrupt_id="i9",
        thread_id="t9",
        run_id="r9",
        requester=Principal(id="dora"),
    )
    await store.add(other)
    assert [r.approval_id for r in await store.visible(ALICE)] == [mine.approval_id]
    carol = Principal(id="carol", roles=["ops"])
    assert [r.approval_id for r in await store.visible(carol)] == [mine.approval_id]
    assert await store.visible(Principal(id="bob")) == []
    monkeypatch.setenv("AUTH_READ_ACROSS_ROLES", "auditor")
    ann = Principal(id="ann", roles=["auditor"])
    assert {r.approval_id for r in await store.visible(ann)} == {
        mine.approval_id,
        other.approval_id,
    }
    assert await store.visible(ALICE, status=APPROVED) == []
    assert len(await store.visible(ALICE, status=PENDING)) == 1
    assert await store.delete_for_thread("t1") == 1
    assert await store.visible(ALICE) == []


# --- the call, as bound and as shown ------------------------------------------------------


def test_the_call_hash_binds_everything_that_decides_the_request() -> None:
    base = dict(
        api="shop",
        method="post",
        url="http://shop.test/orders/7/cancel",
        query=httpx.QueryParams([("a", "1"), ("b", "2")]),
        json_body={"reason": "asked", "amount": 5},
        operation_id="cancelOrder",
        headers=[("If-Match", "v1")],
    )
    digest = call_hash(canonical_call(**base))
    reordered = {**base, "json_body": {"amount": 5, "reason": "asked"}}
    assert call_hash(canonical_call(**reordered)) == digest
    for change in (
        {"method": "PUT"},
        {"url": "http://shop.test/orders/8/cancel"},
        {"url": "http://other.test/orders/7/cancel"},
        {"query": httpx.QueryParams([("b", "2"), ("a", "1")])},
        {"json_body": {"reason": "asked", "amount": 6}},
        {"operation_id": "archiveOrder"},
        {"headers": [("If-Match", "v2")]},
        {"api": "other"},
    ):
        assert call_hash(canonical_call(**{**base, **change})) != digest, change
    with pytest.raises(ValueError):
        call_hash(canonical_call(**{**base, "json_body": {"x": float("nan")}}))


def test_redacted_fields_are_masked_at_any_depth() -> None:
    body = {"Card": "4111", "items": [{"card": "4222", "sku": "a"}], "note": {"CARD": 1}}
    assert redact_fields(body, frozenset({"card"})) == {
        "Card": "<redacted>",
        "items": [{"card": "<redacted>", "sku": "a"}],
        "note": {"CARD": "<redacted>"},
    }
    assert redact_fields(body, frozenset()) is body


def test_the_stated_purpose_is_the_text_the_model_wrote_with_the_call() -> None:
    messages = [
        {"type": "human", "content": "cancel 7"},
        {
            "type": "ai",
            "content": [{"type": "text", "text": "Cancelling order 7\x00 as asked."}],
            "tool_calls": [{"id": "c1", "name": "cancel_order", "args": {}}],
        },
    ]
    assert stated_purpose(messages, "c1") == "Cancelling order 7 as asked."
    assert stated_purpose(messages, "c2") is None
    long = [{"type": "ai", "content": "x" * 900, "tool_calls": [{"id": "c1"}]}]
    assert len(stated_purpose(long, "c1")) == 500


# --- the client's side, in a real graph -------------------------------------------------

SENT: list[httpx.Request] = []
BODY: dict[str, Any] = {}
SECOND_CALL: list[bool] = []


def _upstream(request: httpx.Request) -> httpx.Response:
    SENT.append(request)
    return httpx.Response(200, json={"ok": True})


@tool
async def cancel_order(order_id: str, runtime: ToolRuntime[Any]) -> str:
    """Cancel an order by its id."""
    client = get_client("shop", transport=httpx.MockTransport(_upstream))
    data = await client.post(
        "/orders/{order_id}/cancel",
        operation_id="cancelOrder",
        path_params={"order_id": order_id},
        json_body=dict(BODY),
    )
    if SECOND_CALL:
        await client.post("/orders", operation_id="createOrder", json_body={"sku": "a"})
    return json.dumps(data)


@pytest.fixture
def graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: ApprovalStore) -> Iterator[Any]:
    from {{cookiecutter.agent_directory}} import agent
    from {{cookiecutter.agent_directory}}.app_utils.model import get_model

    policy = tmp_path / "api-policy.yaml"
    policy.write_text(POLICY, encoding="utf-8")
    monkeypatch.setenv("API_POLICY_PATH", str(policy))
    monkeypatch.setenv("SHOP_API_BASE_URL", "http://shop.test")
    reset_policy_cache()
    SENT.clear()
    SECOND_CALL.clear()
    BODY.clear()
    BODY.update({"reason": "asked"})
    set_approval_ledger(store)
    yield create_agent(
        model=get_model(),
        tools=[cancel_order],
        system_prompt=agent.SYSTEM_PROMPT,
        middleware=agent.middleware(),
        context_schema=agent.AgentContext,
        checkpointer=InMemorySaver(),
    )
    set_approval_ledger(None)
    reset_policy_cache()


async def _run(graph: Any, thread: str, graph_input: Any) -> list[Any]:
    """Run to the end; the interrupts it paused on (id and value)."""
    config = {"configurable": {"thread_id": thread}}
    async for _ in graph.astream(graph_input, config, stream_mode="updates"):
        pass
    state = await graph.aget_state(config)
    return list(state.interrupts)


async def _pause(graph: Any, thread: str, store: ApprovalStore) -> tuple[Any, ApprovalRecord]:
    interrupts = await _run(graph, thread, {"messages": [("user", "Cancel the order for 7")]})
    assert len(interrupts) == 1
    record, _, _ = await store.add(
        record_from_interrupt(
            interrupts[0].value,
            interrupt_id=interrupts[0].id,
            thread_id=thread,
            run_id="r",
            requester=ALICE,
        )
    )
    return interrupts[0], record


async def _last_tool_result(graph: Any, thread: str) -> str:
    state = await graph.aget_state({"configurable": {"thread_id": thread}})
    return next(m.content for m in reversed(state.values["messages"]) if m.type == "tool")


async def test_a_gated_call_interrupts_with_the_call_before_anything_is_sent(graph, store) -> None:
    interrupt, record = await _pause(graph, "t1", store)
    value = interrupt.value
    assert value["type"] == APPROVAL_INTERRUPT
    assert (value["method"], value["path"], value["body"]) == ("POST", "/orders/7/cancel", BODY)
    assert value["tool"] == "cancel_order" and value["tool_call_id"] == "call_cancel_order"
    assert value["approvers"] == ["requester", "role:ops"] and value["timeout_s"] == 60
    assert len(value["call_hash"]) == 64
    assert SENT == [] and record.status == PENDING


async def test_approved_the_call_is_sent_once(graph, store) -> None:
    interrupt, record = await _pause(graph, "t1", store)
    decided = await store.decide(record.approval_id, APPROVED, "x", None)
    await _run(graph, "t1", Command(resume={interrupt.id: decision_value(decided, "approve")}))
    assert [(r.method, r.url.path) for r in SENT] == [("POST", "/orders/7/cancel")]
    assert json.loads(SENT[0].content) == BODY
    assert '"ok": true' in await _last_tool_result(graph, "t1")
    assert (await store.get(record.approval_id)).used_at is not None


async def test_rejected_or_expired_nothing_is_sent(graph, store) -> None:
    interrupt, record = await _pause(graph, "t1", store)
    decided = await store.decide(record.approval_id, REJECTED, "x", "not this one")
    await _run(graph, "t1", Command(resume={interrupt.id: decision_value(decided, "reject")}))
    assert "an approver rejected it (their comment: not this one)" in await _last_tool_result(
        graph, "t1"
    )
    interrupt, record = await _pause(graph, "t2", store)
    await _run(graph, "t2", Command(resume={interrupt.id: decision_value(record, "expired")}))
    assert "the approval request expired" in await _last_tool_result(graph, "t2")
    assert SENT == []


async def test_a_resume_the_store_did_not_approve_sends_nothing(graph, store) -> None:
    """A forged resume value (the right hash, an approval still pending) is refused."""
    interrupt, record = await _pause(graph, "t1", store)
    forged = decision_value(record, "approve")
    await _run(graph, "t1", Command(resume={interrupt.id: forged}))
    assert "the approval is pending, not approved" in await _last_tool_result(graph, "t1")
    assert SENT == []


async def test_a_used_approval_cannot_be_replayed_on_another_run(graph, store) -> None:
    interrupt, record = await _pause(graph, "t1", store)
    decided = await store.decide(record.approval_id, APPROVED, "x", None)
    value = decision_value(decided, "approve")
    await _run(graph, "t1", Command(resume={interrupt.id: value}))
    assert len(SENT) == 1
    # The same request, paused on another thread, resumed with the used approval.
    other, _ = await _pause(graph, "t2", store)
    await _run(graph, "t2", Command(resume={other.id: value}))
    assert "the approval belongs to another thread" in await _last_tool_result(graph, "t2")
    assert len(SENT) == 1


async def test_a_request_that_changed_is_not_covered(graph, store) -> None:
    interrupt, record = await _pause(graph, "t1", store)
    decided = await store.decide(record.approval_id, APPROVED, "x", None)
    BODY["reason"] = "changed"
    await _run(graph, "t1", Command(resume={interrupt.id: decision_value(decided, "approve")}))
    assert "differs from the request that was approved" in await _last_tool_result(graph, "t1")
    assert SENT == []


async def test_without_a_ledger_an_approval_is_not_used(graph, store) -> None:
    interrupt, record = await _pause(graph, "t1", store)
    decided = await store.decide(record.approval_id, APPROVED, "x", None)
    set_approval_ledger(None)
    await _run(graph, "t1", Command(resume={interrupt.id: decision_value(decided, "approve")}))
    assert "no approvals ledger" in await _last_tool_result(graph, "t1")
    assert SENT == []


async def test_a_tool_call_sends_at_most_one_approved_call(graph, store) -> None:
    SECOND_CALL.append(True)
    interrupt, record = await _pause(graph, "t1", store)
    decided = await store.decide(record.approval_id, APPROVED, "x", None)
    await _run(graph, "t1", Command(resume={interrupt.id: decision_value(decided, "approve")}))
    result = await _last_tool_result(graph, "t1")
    assert "already sent an approved call" in result
    assert [r.url.path for r in SENT] == ["/orders/7/cancel"]
    state = await graph.aget_state({"configurable": {"thread_id": "t1"}})
    assert not state.interrupts


async def test_a_resume_without_a_decision_sends_nothing(graph, store) -> None:
    interrupt, _ = await _pause(graph, "t1", store)
    await _run(graph, "t1", Command(resume={interrupt.id: {"decision": "approve"}}))
    assert "resumed without an approval decision" in await _last_tool_result(graph, "t1")
    await _pause(graph, "t2", store)
    assert SENT == []


async def test_outside_an_agent_run_a_gated_call_is_refused(graph) -> None:
    client = get_client("shop", transport=httpx.MockTransport(_upstream))
    with pytest.raises(ApiPolicyError) as exc:
        await client.post("/orders", operation_id="createOrder", json_body={"sku": "a"})
    assert "possible only inside an agent run" in str(exc.value)
    assert exc.value.reason.startswith("approval required by approval.required_for.methods")
    assert SENT == []
    assert api_client.approval_ledger() is not None  # the fixture's, untouched


def test_the_decision_type_is_what_the_client_expects() -> None:
    assert decision_value(_record(), "approve")["type"] == APPROVAL_DECISION
    assert utcnow().tzinfo is not None
