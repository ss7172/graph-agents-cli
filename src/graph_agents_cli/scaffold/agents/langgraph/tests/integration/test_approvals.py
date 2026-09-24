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

"""Human approval of gated API calls through the app, in-process (the fastapi runtime).

A test tool cancels an order through `api_client` against an API the test
policy gates; the upstream API is an `httpx.MockTransport` that records every
request it receives, so each test can say exactly what was sent. Several
principals come from a header test policy (`X-User`, `X-Roles`). Every test
runs with the in-memory checkpointer, and again on Postgres (checkpoints,
approvals table, run leases) when `TEST_POSTGRES_DSN` is set.
`tests/integration/test_approvals_server.py` runs the same round trip under
the langgraph-server runtime against a real LangGraph dev server.
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
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from starlette.requests import Request

from {{cookiecutter.agent_directory}}.app_utils import approvals as approvals_module
from {{cookiecutter.agent_directory}}.app_utils import auth as auth_module
from {{cookiecutter.agent_directory}}.app_utils.api_client import get_client, reset_policy_cache
from {{cookiecutter.agent_directory}}.app_utils.auth import ACTIONS, Principal
from {{cookiecutter.agent_directory}}.app_utils.chat import RUNTIME
from {{cookiecutter.agent_directory}}.fast_api_app import app

A2A_PATH = "/a2a/{{cookiecutter.agent_directory}}"
PROMPT = "Cancel the order for 7"

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
      approvers: APPROVERS
      timeout_s: 60
"""

# What the upstream API received, and what the tool will put in the body.
SENT: list[httpx.Request] = []
BODY: dict[str, Any] = {"reason": "customer asked"}
SEEN_BY_TOOL: list[str] = []


def _upstream(request: httpx.Request) -> httpx.Response:
    SENT.append(request)
    return httpx.Response(200, json={"cancelled": request.url.path})


@tool
async def cancel_order(order_id: str, runtime: ToolRuntime[Any]) -> str:
    """Cancel an order by its id."""
    context = getattr(runtime, "context", None)
    SEEN_BY_TOOL.append(str(getattr(context, "principal_id", "")))
    client = get_client("shop", context=context, transport=httpx.MockTransport(_upstream))
    data = await client.post(
        "/orders/{order_id}/cancel",
        operation_id="cancelOrder",
        path_params={"order_id": order_id},
        json_body=dict(BODY),
        redact=["card"],
    )
    return json.dumps(data)


class HeaderPolicy:
    """Test policy: `X-User` is the principal id, `X-Roles` its comma-separated roles."""

    async def authenticate(self, request: Request) -> Principal:
        user = request.headers.get("x-user")
        if not user:
            raise HTTPException(401, "no user", headers={"WWW-Authenticate": "Bearer"})
        roles = [r for r in (request.headers.get("x-roles") or "user").split(",") if r]
        return Principal(id=user, roles=roles, permissions=set(ACTIONS))

    async def authorize(self, principal: Principal, action: str, resource: str | None) -> None:
        return None


def _as(user: str, roles: str = "user") -> dict[str, str]:
    return {"X-User": user, "X-Roles": roles}


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    event = None
    for line in text.splitlines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:") and event:
            events.append((event, json.loads(line[5:].strip())))
            event = None
    return events


def _write_policy(path: Path, approvers: str) -> None:
    path.write_text(POLICY.replace("APPROVERS", approvers), encoding="utf-8")
    reset_policy_cache()


# What a new image's policy may say by the time a paused call's decision comes
# (the fixture's policy gates cancelOrder for [requester, "role:ops"]).
_GATED = POLICY.replace("APPROVERS", '[requester, "role:ops"]')
POLICY_CHANGES = {
    "gate removed": _GATED.split("    approval:")[0],
    "gate narrowed": _GATED.replace("cancelOrder", "refundOrder").replace(
        "/orders/{order_id}/cancel", "/orders/{order_id}/refund"
    ),
    "call denied": _GATED.replace(
        "    approval:",
        "    denied_operations:\n      - operationId: cancelOrder\n        path: "
        "/orders/{order_id}/cancel\n    approval:",
    ),
    "allowed_methods narrowed": _GATED.replace(
        "allowed_methods: [GET, POST]", "allowed_methods: [GET]"
    ),
}
# The policy refuses before any decision is read; else the decision (or the gate) does.
REFUSED_BY = {
    "call denied": "denied by denied_operations",
    "allowed_methods narrowed": "is not in allowed_methods",
}
DECIDED_BY = {
    "reject": "was not approved: an approver rejected it",
    "expired": "was not approved: the approval request expired",
    "approve": "approved under an approval gate the policy no longer has",
}


def _change_policy(tmp_path: Path, change: str) -> None:
    (tmp_path / "api-policy.yaml").write_text(POLICY_CHANGES[change], encoding="utf-8")
    reset_policy_cache()


ADMIN_DSN = os.environ.get("TEST_POSTGRES_DSN", "")


@pytest.fixture(params=["memory", "postgres"])
async def database(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[str]:
    """Every test in memory, and on Postgres (a fresh database) when `TEST_POSTGRES_DSN` is set."""
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
async def client(
    database: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use_test_tools
) -> AsyncIterator[httpx.AsyncClient]:
    policy = tmp_path / "api-policy.yaml"
    _write_policy(policy, '[requester, "role:ops"]')
    monkeypatch.setenv("API_POLICY_PATH", str(policy))
    monkeypatch.setenv("SHOP_API_BASE_URL", "http://shop.test")
    monkeypatch.delenv("AUTH_READ_ACROSS_ROLES", raising=False)
    monkeypatch.delenv("TRACE_CAPTURE", raising=False)
    monkeypatch.setattr(auth_module, "get_policy", lambda: HeaderPolicy())
    SENT.clear()
    SEEN_BY_TOOL.clear()
    BODY.clear()
    BODY.update({"reason": "customer asked", "card": "4111-1111"})
    async with app.router.lifespan_context(app):
        use_test_tools(cancel_order)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", timeout=30
        ) as c:
            started = asyncio.get_running_loop().time()
            while (await c.get("/ready")).status_code != 200:  # postgres: the schema set up
                assert asyncio.get_running_loop().time() - started < 30, "not ready"
                await asyncio.sleep(0.1)
            yield c
    reset_policy_cache()


async def _pause(client: httpx.AsyncClient, user: str = "alice") -> dict[str, Any]:
    """Start a run that pauses before the gated call; the `message.end` event."""
    r = await client.post("/chat", json={"message": PROMPT}, headers=_as(user))
    assert r.status_code == 200, r.text
    events = parse_sse(r.text)
    names = [e for e, _ in events]
    assert names[0] == "message.start" and names[-1] == "message.end", names
    assert "tool.call" in names and "tool.result" not in names
    return events[-1][1]


async def _decide(
    client: httpx.AsyncClient,
    end: dict[str, Any],
    decision: str,
    user: str = "alice",
    roles: str = "user",
    comment: str | None = None,
) -> httpx.Response:
    approval = end["approval"]
    body: dict[str, Any] = {"decision": decision}
    if comment is not None:
        body["comment"] = comment
    return await client.post(
        f"/threads/{end['thread_id']}/approvals/{approval['approval_id']}",
        json=body,
        headers=_as(user, roles),
    )


# --- pause, approve, send exactly once -----------------------------------------------


async def test_a_gated_call_pauses_the_run_and_sends_nothing(client) -> None:
    end = await _pause(client)
    assert end["status"] == "awaiting_approval"
    approval = end["approval"]
    assert end["approvals"] == [approval]
    assert approval["status"] == "pending"
    assert (approval["api"], approval["method"], approval["path"]) == (
        "shop",
        "POST",
        "/orders/7/cancel",
    )
    assert approval["operation_id"] == "cancelOrder"
    # The approver sees the body, with the field the tool named in `redact=` masked.
    assert approval["body"] == {"reason": "customer asked", "card": "<redacted>"}
    assert approval["query"] == {}
    assert approval["tool"] == "cancel_order" and approval["reason"].startswith("cancel_order")
    assert approval["approvers"] == ["requester", "role:ops"]
    assert approval["expires_at"] > approval["created_at"]
    assert approval["requester"] == Principal(id="alice").hashed_id()
    # Internals stay in the server.
    assert "call_hash" not in approval and "interrupt_id" not in approval
    assert SENT == []
    listed = await client.get(f"/threads/{end['thread_id']}/approvals", headers=_as("alice"))
    assert listed.status_code == 200
    assert [a["approval_id"] for a in listed.json()] == [approval["approval_id"]]


async def test_approve_sends_exactly_the_approved_request_once(client) -> None:
    end = await _pause(client)
    r = await _decide(client, end, "approve", comment="looks right")
    assert r.status_code == 200, r.text
    events = parse_sse(r.text)
    names = [e for e, _ in events]
    assert names[0] == "message.start" and names[-1] == "message.end", names
    start = events[0][1]
    assert start["approval_id"] == end["approval"]["approval_id"]
    assert start["decision"] == "approve" and start["run_id"] != end["run_id"]
    result = next(d for e, d in events if e == "tool.result")
    assert result["is_error"] is False and "/orders/7/cancel" in result["result"]
    assert events[-1][1]["status"] == "ok"
    assert len(SENT) == 1
    sent = SENT[0]
    assert (sent.method, sent.url.path) == ("POST", "/orders/7/cancel")
    # The masked field was masked for the approver only: the request is the tool's.
    assert json.loads(sent.content) == {"reason": "customer asked", "card": "4111-1111"}
    # The resumed run acted as the requester.
    assert SEEN_BY_TOOL == ["alice", "alice"]
    approvals = (
        await client.get(f"/threads/{end['thread_id']}/approvals", headers=_as("alice"))
    ).json()
    assert approvals[0]["status"] == "approved"
    assert approvals[0]["decided_by"] == Principal(id="alice").hashed_id()
    assert approvals[0]["comment"] == "looks right"
    # Decided: the call's content is no longer kept (TRACE_CAPTURE=metadata).
    assert "body" not in approvals[0] and "query" not in approvals[0]
    # Single use: deciding again is a conflict, and nothing more is sent.
    again = await _decide(client, end, "approve")
    assert again.status_code == 409 and again.json()["code"] == "approval_not_pending"
    assert again.json()["status"] == "approved"
    assert len(SENT) == 1
    # The thread takes new messages again.
    r = await client.post(
        "/chat", json={"message": "hello", "thread_id": end["thread_id"]}, headers=_as("alice")
    )
    assert r.status_code == 200 and parse_sse(r.text)[-1][1]["status"] == "ok"


async def test_reject_sends_nothing_and_the_model_hears_why(client) -> None:
    end = await _pause(client)
    r = await _decide(client, end, "reject", comment="wrong customer")
    assert r.status_code == 200, r.text
    events = parse_sse(r.text)
    result = next(d for e, d in events if e == "tool.result")
    assert result["is_error"] is True
    assert events[-1][1]["status"] == "ok"
    assert SENT == []
    # The model read the refusal (the history keeps the tool's error text).
    from {{cookiecutter.agent_directory}}.agent import graph

    state = await graph.aget_state({"configurable": {"thread_id": end["thread_id"]}})
    tool_message = next(m for m in state.values["messages"] if m.type == "tool")
    assert "was not approved: an approver rejected it" in tool_message.content
    assert "wrong customer" in tool_message.content and "nothing was sent" in tool_message.content


async def test_an_expired_approval_is_410_sends_nothing_and_frees_the_thread(
    client, monkeypatch
) -> None:
    end = await _pause(client)
    later = approvals_module.utcnow() + timedelta(seconds=61)
    assert RUNTIME.approvals is not None
    monkeypatch.setattr(RUNTIME.approvals, "_clock", lambda: later)
    r = await _decide(client, end, "approve")
    assert r.status_code == 410 and r.json()["code"] == "approval_expired"
    listed = await client.get(f"/threads/{end['thread_id']}/approvals", headers=_as("alice"))
    assert listed.json()[0]["status"] == "expired"
    # Expired = rejected: the thread takes a new message, and the paused call's
    # result says it was not approved.
    r = await client.post(
        "/chat", json={"message": "hello", "thread_id": end["thread_id"]}, headers=_as("alice")
    )
    assert r.status_code == 200 and parse_sse(r.text)[-1][1]["status"] == "ok"
    messages = (
        await client.get(f"/threads/{end['thread_id']}/messages", headers=_as("alice"))
    ).json()
    from {{cookiecutter.agent_directory}}.agent import graph

    state = await graph.aget_state({"configurable": {"thread_id": end["thread_id"]}})
    tool_message = next(m for m in state.values["messages"] if m.type == "tool")
    assert "approval request expired" in tool_message.content
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "user", "assistant"]
    assert SENT == []


async def test_a_request_changed_after_approval_is_refused(client) -> None:
    end = await _pause(client)
    BODY["reason"] = "something else"  # the tool now builds a different request
    r = await _decide(client, end, "approve")
    assert r.status_code == 200, r.text
    result = next(d for e, d in parse_sse(r.text) if e == "tool.result")
    assert result["is_error"] is True
    assert SENT == []
    from {{cookiecutter.agent_directory}}.agent import graph

    state = await graph.aget_state({"configurable": {"thread_id": end["thread_id"]}})
    tool_message = next(m for m in state.values["messages"] if m.type == "tool")
    assert "differs from the request that was approved" in tool_message.content
    # The approval stays unused: it can never send the changed request.
    record = await RUNTIME.approvals.get(end["approval"]["approval_id"])
    assert record is not None and record.status == "approved" and record.used_at is None


async def test_an_approval_asked_of_other_approvers_than_the_policy_names_now_sends_nothing(
    client, tmp_path
) -> None:
    end = await _pause(client)
    assert end["approval"]["approvers"] == ["requester", "role:ops"]
    # A new image while the call waits: its policy asks for four eyes.
    _write_policy(tmp_path / "api-policy.yaml", '["role:ops"]')
    r = await _decide(client, end, "approve")  # allowed by the approvers it was asked of
    assert r.status_code == 200, r.text
    result = next(d for e, d in parse_sse(r.text) if e == "tool.result")
    assert result["is_error"] is True
    assert SENT == []
    from {{cookiecutter.agent_directory}}.agent import graph

    state = await graph.aget_state({"configurable": {"thread_id": end["thread_id"]}})
    tool_message = next(m for m in state.values["messages"] if m.type == "tool")
    assert "approval gate that has changed" in tool_message.content


@pytest.mark.parametrize("change", list(POLICY_CHANGES))
@pytest.mark.parametrize("decision", ["reject", "approve"])
async def test_a_decision_binds_its_call_whatever_the_policy_says_by_then(
    client, tmp_path, change: str, decision: str
) -> None:
    """Reject never sends, approve never outruns a later denial or narrowing."""
    end = await _pause(client)
    _change_policy(tmp_path, change)  # a new image while the call waits
    r = await _decide(client, end, decision)
    assert r.status_code == 200, r.text
    events = parse_sse(r.text)
    result = next(d for e, d in events if e == "tool.result")
    assert result["is_error"] is True
    assert REFUSED_BY.get(change, DECIDED_BY[decision]) in result["result"], result
    assert events[-1][1]["status"] == "ok"
    assert SENT == []
    record = await RUNTIME.approvals.get(end["approval"]["approval_id"])
    assert record is not None and record.used_at is None
    # The thread goes on, and nothing is sent later either.
    r = await client.post(
        "/chat", json={"message": "hello", "thread_id": end["thread_id"]}, headers=_as("alice")
    )
    assert r.status_code == 200 and parse_sse(r.text)[-1][1]["status"] == "ok"
    assert SENT == []


async def test_approvals_a_failed_run_recorded_do_not_block_the_thread(client, monkeypatch) -> None:
    record = RUNTIME._record_approvals

    async def record_then_fail(*args: Any, **kwargs: Any) -> Any:
        await record(*args, **kwargs)
        raise RuntimeError("the database went away")

    monkeypatch.setattr(RUNTIME, "_record_approvals", record_then_fail)
    r = await client.post("/chat", json={"message": PROMPT}, headers=_as("alice"))
    events = parse_sse(r.text)
    assert events[-1][0] == "error", events
    thread = events[0][1]["thread_id"]
    monkeypatch.setattr(RUNTIME, "_record_approvals", record)
    # Nobody was told about the approval: it is expired, not left pending.
    listed = (await client.get(f"/threads/{thread}/approvals", headers=_as("alice"))).json()
    assert [a["status"] for a in listed] == ["expired"]
    r = await client.post(
        "/chat", json={"message": "hello", "thread_id": thread}, headers=_as("alice")
    )
    assert r.status_code == 200 and parse_sse(r.text)[-1][1]["status"] == "ok"
    assert SENT == []


# --- who decides ---------------------------------------------------------------------


async def test_only_an_approver_decides(client) -> None:
    end = await _pause(client)
    for user, roles in (("bob", "user"), ("eve", "admin,support")):
        r = await _decide(client, end, "approve", user=user, roles=roles)
        assert r.status_code == 403 and r.json()["code"] == "not_an_approver", (user, r.text)
    # A principal with a listed role decides (four eyes), and the run still acts
    # as the requester.
    r = await _decide(client, end, "approve", user="carol", roles="ops")
    assert r.status_code == 200, r.text
    assert len(SENT) == 1 and SEEN_BY_TOOL == ["alice", "alice"]
    approvals = (
        await client.get(f"/threads/{end['thread_id']}/approvals", headers=_as("alice"))
    ).json()
    assert approvals[0]["decided_by"] == Principal(id="carol").hashed_id()


async def test_four_eyes_the_requester_cannot_approve_their_own_call(client, tmp_path) -> None:
    _write_policy(tmp_path / "api-policy.yaml", '["role:ops"]')
    end = await _pause(client, user="alice")
    assert end["approval"]["approvers"] == ["role:ops"]
    # Holding the role does not make the requester a second pair of eyes.
    r = await _decide(client, end, "approve", user="alice", roles="ops")
    assert r.status_code == 403
    r = await _decide(client, end, "approve", user="carol", roles="ops")
    assert r.status_code == 200 and len(SENT) == 1


async def test_listing_follows_who_may_see(client, monkeypatch) -> None:
    end = await _pause(client)
    thread = end["thread_id"]
    approval_id = end["approval"]["approval_id"]
    # A decider sees it, with the call; a stranger does not.
    ops = await client.get(f"/threads/{thread}/approvals", headers=_as("carol", "ops"))
    assert ops.status_code == 200 and ops.json()[0]["body"]["reason"] == "customer asked"
    stranger = await client.get(f"/threads/{thread}/approvals", headers=_as("bob"))
    assert stranger.status_code == 403
    # Read-across roles list it, without the call's content, and cannot decide.
    monkeypatch.setenv("AUTH_READ_ACROSS_ROLES", "auditor")
    auditor = await client.get(f"/threads/{thread}/approvals", headers=_as("ann", "auditor"))
    assert auditor.status_code == 200
    assert auditor.json()[0]["approval_id"] == approval_id and "body" not in auditor.json()[0]
    r = await _decide(client, end, "approve", user="ann", roles="auditor")
    assert r.status_code == 403
    # Across threads: the requester and the ops role see it, bob does not.
    for user, roles, seen in (
        ("alice", "user", True),
        ("carol", "ops", True),
        ("bob", "user", False),
    ):
        listed = await client.get("/approvals?status=pending", headers=_as(user, roles))
        assert listed.status_code == 200
        assert (approval_id in [a["approval_id"] for a in listed.json()]) is seen, user
    assert (await client.get("/approvals?status=bogus", headers=_as("alice"))).status_code == 422
    unknown = await client.get(f"/threads/{uuid.uuid4()}/approvals", headers=_as("alice"))
    assert unknown.status_code == 404


async def test_an_unknown_or_foreign_approval_is_404(client) -> None:
    end = await _pause(client)
    other = await _pause(client, user="dora")
    # Another thread's approval id on this thread's route.
    r = await client.post(
        f"/threads/{end['thread_id']}/approvals/{other['approval']['approval_id']}",
        json={"decision": "approve"},
        headers=_as("alice"),
    )
    assert r.status_code == 404 and r.json()["code"] == "approval_not_found"
    r = await client.post(
        f"/threads/{end['thread_id']}/approvals/{uuid.uuid4().hex}",
        json={"decision": "approve"},
        headers=_as("alice"),
    )
    assert r.status_code == 404
    bad = await client.post(
        f"/threads/{end['thread_id']}/approvals/{end['approval']['approval_id']}",
        json={"decision": "maybe"},
        headers=_as("alice"),
    )
    assert bad.status_code == 422
    assert SENT == []


# --- the thread while pending ------------------------------------------------------------


async def test_a_pending_approval_blocks_new_messages(client) -> None:
    end = await _pause(client)
    r = await client.post(
        "/chat", json={"message": "hello", "thread_id": end["thread_id"]}, headers=_as("alice")
    )
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "approval_pending"
    assert [a["approval_id"] for a in body["approvals"]] == [end["approval"]["approval_id"]]
    # Other threads are not affected.
    other = await client.post("/chat", json={"message": "hello"}, headers=_as("alice"))
    assert other.status_code == 200


async def test_two_decisions_race_and_one_wins(client) -> None:
    end = await _pause(client)
    first, second = await asyncio.gather(
        _decide(client, end, "approve", user="alice"),
        _decide(client, end, "reject", user="carol", roles="ops"),
    )
    statuses = sorted([first.status_code, second.status_code])
    assert statuses == [200, 409], (first.text, second.text)
    loser = first if first.status_code == 409 else second
    assert loser.json()["code"] in ("approval_not_pending", "thread_busy")
    assert len(SENT) <= 1
    record = await RUNTIME.approvals.get(end["approval"]["approval_id"])
    assert record is not None
    assert len(SENT) == (1 if record.status == "approved" else 0)


async def test_an_approval_the_run_no_longer_waits_for_cannot_be_decided(client) -> None:
    end = await _pause(client)
    # The paused call gets a result some other way (as a history repair would).
    from {{cookiecutter.agent_directory}}.app_utils.chat import ChatRequest

    lease = await RUNTIME.acquire_thread(end["thread_id"])  # writes need the run lock
    try:
        await RUNTIME._repair_history(ChatRequest(message=""), end["thread_id"], "closed")
    finally:
        await lease.release()
    r = await _decide(client, end, "approve")
    assert r.status_code == 409 and r.json() == {
        "code": "approval_not_pending",
        "detail": "The run no longer waits for this approval.",
        "status": "expired",
    }
    listed = await client.get(f"/threads/{end['thread_id']}/approvals", headers=_as("alice"))
    assert listed.json()[0]["status"] == "expired"
    # Not pending any more: the thread takes messages again.
    r = await client.post(
        "/chat", json={"message": "hello", "thread_id": end["thread_id"]}, headers=_as("alice")
    )
    assert r.status_code == 200
    assert SENT == []


@tool
async def ask_a_person(question: str) -> str:
    """Ask a person something (pauses the graph with a plain interrupt)."""
    from langgraph.types import interrupt

    return str(interrupt({"question": question}))


async def test_a_pause_that_is_not_an_approval_ends_the_run_cleanly(client, use_test_tools) -> None:
    use_test_tools(ask_a_person)
    r = await client.post(
        "/chat", json={"message": "Ask a person about the weather"}, headers=_as("alice")
    )
    events = parse_sse(r.text)
    assert events[-1][0] == "error" and events[-1][1]["code"] == "unsupported_interrupt"
    thread = events[0][1]["thread_id"]
    assert (await client.get(f"/threads/{thread}/approvals", headers=_as("alice"))).json() == []
    # The paused call was answered: the thread goes on.
    r = await client.post(
        "/chat", json={"message": "hello", "thread_id": thread}, headers=_as("alice")
    )
    assert parse_sse(r.text)[-1][1]["status"] == "ok"


async def test_deleting_the_thread_deletes_its_approvals(client) -> None:
    end = await _pause(client)
    approval_id = end["approval"]["approval_id"]
    r = await client.delete(f"/threads/{end['thread_id']}", headers=_as("alice"))
    assert r.status_code == 204
    assert await RUNTIME.approvals.get(approval_id) is None
    assert await RUNTIME.approvals.for_thread(end["thread_id"]) == []
    r = await _decide(client, end, "approve")
    assert r.status_code == 404
    listed = await client.get("/approvals", headers=_as("alice"))
    assert approval_id not in [a["approval_id"] for a in listed.json()]
    assert SENT == []


async def test_the_approval_metrics_count(client, monkeypatch) -> None:
    def count(event: str) -> float:
        from {{cookiecutter.agent_directory}}.app_utils.metrics import APPROVALS

        return APPROVALS.labels(event)._value.get()

    before = {e: count(e) for e in ("requested", "approved", "rejected", "expired")}
    await _decide(client, await _pause(client), "approve")
    await _decide(client, await _pause(client), "reject")
    await _pause(client)
    later = approvals_module.utcnow() + timedelta(seconds=61)
    monkeypatch.setattr(RUNTIME.approvals, "_clock", lambda: later)
    await RUNTIME.sweep_approvals()
    after = {e: count(e) for e in before}
    assert {e: after[e] - before[e] for e in before} == {
        "requested": 3,
        "approved": 1,
        "rejected": 1,
        "expired": 1,
    }
    text = (await client.get("/metrics")).text
    assert 'agent_approvals_total{event="requested"}' in text
    assert 'agent_runs_total{status="awaiting_approval"}' in text


async def test_the_playground_and_the_prompt_know_about_approvals(client) -> None:
    page = (await client.get("/playground")).text
    assert "awaiting_approval" in page and "/approvals/" in page
    assert "Approve" in page and "Reject" in page
    from {{cookiecutter.agent_directory}}.agent import SYSTEM_PROMPT

    assert "need a person's approval" in SYSTEM_PROMPT
    assert "was not approved" in SYSTEM_PROMPT


# --- two gated calls in one step ---------------------------------------------------------


class TwoCancels(BaseChatModel):
    """Asks for two cancellations at once (parallel tool calls), then answers."""

    @property
    def _llm_type(self) -> str:
        return "two-cancels"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self

    def _generate(self, messages: list[Any], stop: Any = None, run_manager: Any = None, **kw: Any):
        if isinstance(messages[-1], ToolMessage):
            reply = AIMessage(content="Done.")
        else:
            reply = AIMessage(
                content="Cancelling orders 7 and 8, as you asked.",
                tool_calls=[
                    {"name": "cancel_order", "args": {"order_id": "7"}, "id": "c7"},
                    {"name": "cancel_order", "args": {"order_id": "8"}, "id": "c8"},
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=reply)])


@pytest.fixture
def two_calls(client, monkeypatch) -> None:
    from langchain.agents import create_agent

    from {{cookiecutter.agent_directory}} import agent

    graph = create_agent(
        model=TwoCancels(),
        tools=[cancel_order],
        system_prompt=agent.SYSTEM_PROMPT,
        middleware=agent.middleware(),
        context_schema=agent.AgentContext,
    )
    graph.checkpointer = agent.graph.checkpointer
    monkeypatch.setattr(agent, "graph", graph)


async def _expire_now(approval_id: str) -> None:
    store = RUNTIME.approvals
    assert store is not None
    past = approvals_module.utcnow() - timedelta(seconds=1)
    if store.db.is_postgres:
        await store.db.execute(
            f"UPDATE {store.table} SET expires_at = %s WHERE approval_id = %s", (past, approval_id)
        )
    else:
        store._memory[approval_id].expires_at = past


async def test_two_gated_calls_wait_for_their_own_decisions(client, two_calls) -> None:
    end = await _pause(client)
    by_path = {a["path"]: a for a in end["approvals"]}
    assert set(by_path) == {"/orders/7/cancel", "/orders/8/cancel"}
    assert by_path["/orders/7/cancel"]["reason"] == (
        "cancel_order: Cancelling orders 7 and 8, as you asked."
    )
    first = {**end, "approval": by_path["/orders/7/cancel"]}
    r = await _decide(client, first, "approve")
    assert r.status_code == 200, r.text
    events = parse_sse(r.text)
    # The other call still waits, for the same approval (not a second one).
    again = events[-1][1]
    assert again["status"] == "awaiting_approval"
    assert [a["approval_id"] for a in again["approvals"]] == [
        by_path["/orders/8/cancel"]["approval_id"]
    ]
    assert [req.url.path for req in SENT] == ["/orders/7/cancel"]
    r = await _decide(client, {**end, "approval": by_path["/orders/8/cancel"]}, "approve")
    assert r.status_code == 200, r.text
    assert parse_sse(r.text)[-1][1]["status"] == "ok"
    # Each approved call was sent once: the first was not sent again on the second resume.
    assert [req.url.path for req in SENT] == ["/orders/7/cancel", "/orders/8/cancel"]


async def test_a_decision_also_closes_the_calls_whose_approval_expired(client, two_calls) -> None:
    end = await _pause(client)
    by_path = {a["path"]: a for a in end["approvals"]}
    await _expire_now(by_path["/orders/8/cancel"]["approval_id"])
    r = await _decide(client, {**end, "approval": by_path["/orders/7/cancel"]}, "approve")
    assert r.status_code == 200, r.text
    events = parse_sse(r.text)
    results = {d["id"]: d for e, d in events if e == "tool.result"}
    assert results["c7"]["is_error"] is False and results["c8"]["is_error"] is True
    assert events[-1][1]["status"] == "ok"
    assert [req.url.path for req in SENT] == ["/orders/7/cancel"]
    listed = (
        await client.get(f"/threads/{end['thread_id']}/approvals", headers=_as("alice"))
    ).json()
    assert {a["path"]: a["status"] for a in listed} == {
        "/orders/7/cancel": "approved",
        "/orders/8/cancel": "expired",
    }


@pytest.mark.parametrize("change", list(POLICY_CHANGES))
async def test_an_expired_approval_stops_its_call_whatever_the_policy_says_by_then(
    client, two_calls, tmp_path, change: str
) -> None:
    end = await _pause(client)
    by_path = {a["path"]: a for a in end["approvals"]}
    await _expire_now(by_path["/orders/8/cancel"]["approval_id"])
    _change_policy(tmp_path, change)
    # Deciding the other call resumes the run: the expired one gets "expired".
    r = await _decide(client, {**end, "approval": by_path["/orders/7/cancel"]}, "reject")
    assert r.status_code == 200, r.text
    events = parse_sse(r.text)
    results = {d["id"]: d for e, d in events if e == "tool.result"}
    assert results["c8"]["is_error"] is True
    assert REFUSED_BY.get(change, DECIDED_BY["expired"]) in results["c8"]["result"]
    assert results["c7"]["is_error"] is True
    assert events[-1][1]["status"] == "ok"
    assert SENT == []


async def test_a_call_still_waiting_waits_on_when_its_gate_is_removed(
    client, two_calls, tmp_path
) -> None:
    end = await _pause(client)
    by_path = {a["path"]: a for a in end["approvals"]}
    _change_policy(tmp_path, "gate removed")
    r = await _decide(client, {**end, "approval": by_path["/orders/7/cancel"]}, "approve")
    assert r.status_code == 200, r.text
    events = parse_sse(r.text)
    results = {d["id"]: d for e, d in events if e == "tool.result"}
    assert DECIDED_BY["approve"] in results["c7"]["result"]
    # The other call was not sent on its own: it still waits, for the same approval.
    again = events[-1][1]
    assert again["status"] == "awaiting_approval"
    waiting = by_path["/orders/8/cancel"]
    assert [a["approval_id"] for a in again["approvals"]] == [waiting["approval_id"]]
    assert again["approvals"][0]["approvers"] == ["requester", "role:ops"]
    assert SENT == []
    r = await _decide(client, {**end, "approval": waiting}, "reject")
    assert r.status_code == 200, r.text
    events = parse_sse(r.text)
    results = {d["id"]: d for e, d in events if e == "tool.result"}
    assert DECIDED_BY["reject"] in results["c8"]["result"]
    assert events[-1][1]["status"] == "ok"
    assert SENT == []


# --- A2A: input-required and back ----------------------------------------------------


async def _rpc(client: httpx.AsyncClient, user: str, method: str, params: dict) -> dict:
    r = await client.post(
        A2A_PATH,
        json={"jsonrpc": "2.0", "id": "1", "method": method, "params": params},
        headers={**_as(user), "A2A-Version": "1.0"},
    )
    return r.json()


def _parts_data(message: dict[str, Any]) -> dict[str, Any]:
    return next(p["data"] for p in message["parts"] if "data" in p)


async def test_a2a_input_required_round_trip(client) -> None:
    # A user of its own: the A2A task store lives as long as the app.
    user = f"ann-{uuid.uuid4().hex[:8]}"
    sent = await _rpc(
        client,
        user,
        "SendMessage",
        {"message": {"messageId": "m-1", "role": "ROLE_USER", "parts": [{"text": PROMPT}]}},
    )
    task = sent["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED", task
    data = _parts_data(task["status"]["message"])
    assert data["type"] == "approval_request"
    approval = data["approval"]
    assert approval["path"] == "/orders/7/cancel" and approval["status"] == "pending"
    assert SENT == []

    # A text message meanwhile: still waiting for the decision.
    waiting = await _rpc(
        client,
        user,
        "SendMessage",
        {
            "message": {
                "messageId": "m-2",
                "role": "ROLE_USER",
                "taskId": task["id"],
                "contextId": task["contextId"],
                "parts": [{"text": "hello?"}],
            }
        },
    )
    assert waiting["result"]["task"]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert SENT == []

    # A malformed decision is invalid params.
    bad = await _rpc(
        client,
        user,
        "SendMessage",
        {
            "message": {
                "messageId": "m-3",
                "role": "ROLE_USER",
                "taskId": task["id"],
                "contextId": task["contextId"],
                "parts": [{"data": {"approval_id": approval["approval_id"], "decision": "yes"}}],
            }
        },
    )
    assert bad["error"]["code"] == -32602

    done = await _rpc(
        client,
        user,
        "SendMessage",
        {
            "message": {
                "messageId": "m-4",
                "role": "ROLE_USER",
                "taskId": task["id"],
                "contextId": task["contextId"],
                "parts": [
                    {"data": {"approval_id": approval["approval_id"], "decision": "approve"}}
                ],
            }
        },
    )
    finished = done["result"]["task"]
    assert finished["status"]["state"] == "TASK_STATE_COMPLETED", finished
    assert len(SENT) == 1 and SENT[0].url.path == "/orders/7/cancel"
    text = "".join(p.get("text", "") for a in finished["artifacts"] for p in a["parts"])
    assert "/orders/7/cancel" in text


class NoDecidePolicy(HeaderPolicy):
    """The header policy, refusing the `approval.decide` action."""

    async def authorize(self, principal: Principal, action: str, resource: str | None) -> None:
        if action == "approval.decide":
            raise HTTPException(403, "approval.decide is not allowed for you")


async def test_a2a_decision_needs_the_approval_decide_action(client, monkeypatch) -> None:
    user = f"ann-{uuid.uuid4().hex[:8]}"
    sent = await _rpc(
        client,
        user,
        "SendMessage",
        {"message": {"messageId": "m-1", "role": "ROLE_USER", "parts": [{"text": PROMPT}]}},
    )
    task = sent["result"]["task"]
    approval = _parts_data(task["status"]["message"])["approval"]
    monkeypatch.setattr(auth_module, "get_policy", lambda: NoDecidePolicy())
    refused = await _rpc(
        client,
        user,
        "SendMessage",
        {
            "message": {
                "messageId": "m-2",
                "role": "ROLE_USER",
                "taskId": task["id"],
                "contextId": task["contextId"],
                "parts": [
                    {"data": {"approval_id": approval["approval_id"], "decision": "approve"}}
                ],
            }
        },
    )
    after = refused["result"]["task"]
    assert after["status"]["state"] == "TASK_STATE_INPUT_REQUIRED", after
    note = "".join(p.get("text", "") for p in after["status"]["message"]["parts"])
    assert "approval.decide is not allowed" in note
    # The HTTP route refuses it the same way; the approval stays pending.
    paused_on = {"thread_id": task["contextId"], "approval": approval}
    r = await _decide(client, paused_on, "approve", user)
    assert r.status_code == 403
    assert SENT == []
    record = await RUNTIME.approvals.get(approval["approval_id"])
    assert record is not None and record.status == "pending"


async def test_a2a_decision_by_a_requester_the_policy_does_not_list_is_refused(
    client, tmp_path
) -> None:
    user = f"ann-{uuid.uuid4().hex[:8]}"
    _write_policy(tmp_path / "api-policy.yaml", '["role:ops"]')
    sent = await _rpc(
        client,
        user,
        "SendMessage",
        {"message": {"messageId": "m-1", "role": "ROLE_USER", "parts": [{"text": PROMPT}]}},
    )
    task = sent["result"]["task"]
    approval = _parts_data(task["status"]["message"])["approval"]
    refused = await _rpc(
        client,
        user,
        "SendMessage",
        {
            "message": {
                "messageId": "m-2",
                "role": "ROLE_USER",
                "taskId": task["id"],
                "contextId": task["contextId"],
                "parts": [
                    {"data": {"approval_id": approval["approval_id"], "decision": "approve"}}
                ],
            }
        },
    )
    after = refused["result"]["task"]
    assert after["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    note = "".join(p.get("text", "") for p in after["status"]["message"]["parts"])
    assert "not_an_approver" in note
    assert SENT == []
