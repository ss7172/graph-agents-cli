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

"""The A2A client (`app_utils/a2a_client.py`): asking other agents, and relaying approvals.

The peer is a fake A2A 1.0 agent `orders` behind `httpx.MockTransport` (its
card, its JSON-RPC endpoint and its approvals ledger), so every request the
client sends, and what the policy lets through, is checked without a network.
The relay runs in a real graph (the fake model, the ledger an `ApprovalStore`)
that pauses at the policy's approve gate and resumes with a decision made here.
The same client against a real agent (this template, with token exchange) is in
`tests/integration/test_a2a_relay.py`.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("MODEL_PROVIDER", "fake")

import httpx
import pytest
from a2a.types.a2a_pb2 import SendMessageRequest
from google.protobuf import json_format
from langchain.agents import create_agent
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from {{cookiecutter.agent_directory}}.app_utils import a2a_client
from {{cookiecutter.agent_directory}}.app_utils.a2a_client import (
    A2APeerClient,
    PeerRpcError,
    client_settings,
    context_id_for,
    peer_tools,
    reset_a2a_client,
)
from {{cookiecutter.agent_directory}}.app_utils.api_client import (
    A2A_ORIGIN_EXTENSION,
    ApiCallError,
    ApiPolicyError,
    reset_limits,
    reset_policy_cache,
    set_approval_ledger,
)
from {{cookiecutter.agent_directory}}.app_utils.approvals import (
    APPROVED,
    REJECTED,
    ApprovalStore,
    decision_value,
    record_from_interrupt,
)
from {{cookiecutter.agent_directory}}.app_utils.auth import Principal
from {{cookiecutter.agent_directory}}.app_utils.db import Database
from {{cookiecutter.agent_directory}}.app_utils.limits import SettingsError

BASE = "http://orders-agent:8080"
PATH = "/a2a/orders"

POLICY = """
apis:
  orders_agent:
    description: "Orders agent: reads the caller's orders; cancels one after approval."
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
      - {rpc_method: CancelTask, methods: [POST], path: /a2a/orders}
      - {operationId: listContextApprovals, methods: [GET], path: "/threads/{context_id}/approvals"}
    approval:
      - required_for: {operations: [{a2a_operation: approve}]}
        approvers: [requester]
        timeout_s: 900
    limits: {max_calls_per_run: 12, max_response_bytes: 1048576}
    timeouts_ms: {connect: 2000, read: 120000}
  billing_agent:
    description: "Billing agent: invoices and refunds."
    protocol: a2a
    a2a: {path: /a2a/billing}
    base_url_env: BILLING_AGENT_URL
    auth: bearer
    token_env: BILLING_AGENT_KEY
    allowed_methods: [GET, POST]
    allowed_operations:
      - {operationId: getAgentCard, methods: [GET], path: /a2a/billing/.well-known/agent-card.json}
      - {rpc_method: SendMessage, methods: [POST], path: /a2a/billing}
      - {rpc_method: GetTask, methods: [POST], path: /a2a/billing}
    denied_operations:
      - {a2a_operation: approve}
  shop:
    base_url_env: SHOP_API_BASE_URL
    auth: none
    allowed_methods: [GET]
"""
PEERS = {
    "orders": {
        "api": "orders_agent",
        "approvals": "relay",
        "description": "Orders agent: reads the caller's orders; cancels one after approval.",
    },
    "billing": {"api": "billing_agent", "approvals": "deny", "description": "Billing agent."},
}
ALICE = {"principal_id": "alice", "roles": ["user"], "attributes": {}}


def _approval(**overrides: Any) -> dict[str, Any]:
    """orders' pending approval, as its public approval object (exact values)."""
    approval = {
        "approval_id": "de2e",
        "status": "pending",
        "api": "orders_api",
        "method": "POST",
        "path": "/orders/ORD-1002/cancel",
        "operation_id": "cancelOrder",
        "query": {},
        "body": {"reason": "customer asked", "amount": 1, "big": 12345678901234567890},
        "reason": "cancel_order: the customer asked",
        "approvers": ["requester"],
        "decide_with": "relayed",
        "digest": "sha256:" + "d" * 64,
        "created_at": "2026-09-28T10:00:00+00:00",
        "expires_at": "2099-01-01T00:00:00+00:00",
    }
    approval.update(overrides)
    return approval


class FakePeer:
    """An A2A 1.0 agent `orders` behind httpx.MockTransport.

    A message containing "cancel" pauses for `approvals` (input-required, with
    `approval_json`); a decision message completes; anything else is echoed.
    `busy` answers that many messages with a failed `thread_busy` task, `lost`
    answers GetTask -32001, `cancel_busy` answers that many cancels -32002, and
    `card` / `answer` replace the card or every JSON-RPC answer.
    """

    def __init__(self, name: str = "orders", base: str = BASE) -> None:
        self.name, self.base = name, base
        self.requests: list[httpx.Request] = []
        self.tasks: dict[str, dict[str, Any]] = {}
        self.approvals: list[dict[str, Any]] = [_approval()]
        self.busy = self.cancel_busy = 0
        self.lost = False
        self.approval_json = True
        self.context_override: str | None = None
        self.card: dict[str, Any] | None = None
        self.card_status = 200
        self.answer: Any = None

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    @property
    def calls(self) -> list[str]:
        """Each JSON-RPC request's method (and decision), in order; card and ledger reads too."""
        out = []
        for r in self.requests:
            if r.method == "GET":
                out.append("card" if r.url.path.endswith("agent-card.json") else "ledger")
                continue
            body = json.loads(r.content)
            decisions = [
                p["data"]["decision"]
                for p in body["params"].get("message", {}).get("parts", [])
                if "data" in p
            ]
            out.append(" ".join([body["method"], *decisions]))
        return out

    def default_card(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": "Orders agent\x07: reads and cancels orders." + " x" * 400,
            "supportedInterfaces": [
                {
                    "url": f"{self.base}/a2a/{self.name}",
                    "protocolBinding": "JSONRPC",
                    "protocolVersion": "1.0",
                }
            ],
            "version": "0.1.0",
            "capabilities": {"streaming": True, "extensions": [{"uri": A2A_ORIGIN_EXTENSION}]},
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET" and request.url.path.endswith("agent-card.json"):
            if self.card_status != 200:
                return httpx.Response(self.card_status, json={"detail": "no"})
            return httpx.Response(200, json=self.card or self.default_card())
        if request.method == "GET":  # the approvals ledger
            return httpx.Response(200, json=self.approvals)
        body = json.loads(request.content)
        if self.answer is not None:
            return httpx.Response(200, json=self.answer)
        rpc_id, method, params = body["id"], body["method"], body["params"]
        if method == "SendMessage":
            return self._send(rpc_id, params["message"])
        if method == "GetTask":
            if self.lost or params["id"] not in self.tasks:
                return self._error(rpc_id, -32001, "Task not found")
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": rpc_id, "result": self.tasks[params["id"]]}
            )
        if method == "CancelTask":
            if self.cancel_busy:
                self.cancel_busy -= 1
                return self._error(rpc_id, -32002, "running on another replica")
            task = self._task("TASK_STATE_CANCELED", "canceled", body["params"].get("id"))
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rpc_id, "result": task})
        return self._error(rpc_id, -32601, "Method not found")

    @staticmethod
    def _error(rpc_id: Any, code: int, message: str) -> httpx.Response:
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}
        )

    def _task(
        self,
        state: str,
        text: str,
        task_id: str | None = None,
        context_id: str = "",
        parts: list[dict[str, Any]] | None = None,
        reply: str | None = None,
    ) -> dict[str, Any]:
        task: dict[str, Any] = {
            "id": task_id or uuid.uuid4().hex,
            "contextId": self.context_override or context_id,
            "status": {
                "state": state,
                "message": {
                    "messageId": uuid.uuid4().hex,
                    "role": "ROLE_AGENT",
                    "parts": parts or [{"text": text}],
                },
            },
        }
        if reply is not None:
            task["artifacts"] = [
                {"artifactId": "a1", "name": "response", "parts": [{"text": "first draft"}]},
                {"artifactId": "a2", "name": "response", "parts": [{"text": reply}]},
            ]
        self.tasks[task["id"]] = task
        return task

    def _send(self, rpc_id: Any, message: dict[str, Any]) -> httpx.Response:
        context_id = message.get("contextId", "")
        text = "".join(p.get("text", "") for p in message["parts"])
        decisions = [p["data"] for p in message["parts"] if "data" in p]
        if self.busy:
            self.busy -= 1
            task = self._task(
                "TASK_STATE_FAILED",
                "thread_busy: busy",
                context_id=context_id,
                parts=[
                    {"text": "thread_busy: busy"},
                    {"data": {"type": "error", "code": "thread_busy"}},
                ],
            )
        elif decisions:
            done = "Cancelled ORD-1002." if decisions[0]["decision"] == "approve" else "Kept it."
            task = self._task("TASK_STATE_COMPLETED", "", context_id=context_id, reply=done)
        elif "cancel" in text:
            data: dict[str, Any] = {
                "type": "approval_request",
                "approval": self.approvals[0] if self.approvals else None,
                "approvals": self.approvals,
            }
            if self.approval_json:
                data["approval_json"] = json.dumps(self.approvals)
            task = self._task(
                "TASK_STATE_INPUT_REQUIRED",
                "",
                context_id=context_id,
                parts=[
                    {"text": "Waiting for approval de2e: POST /orders/ORD-1002/cancel"},
                    {"data": data},
                ],
            )
        else:
            task = self._task(
                "TASK_STATE_COMPLETED", "", context_id=context_id, reply=f"echo: {text}"
            )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": rpc_id, "result": {"task": task}})


@pytest.fixture
def policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.delenv("APP_ENV", raising=False)  # cluster-internal peers over plain http
    for name in (
        "A2A_FORWARD_ORIGIN",
        "A2A_CARD_TTL_S",
        "A2A_REPLY_MAX_CHARS",
        "A2A_ORIGIN_MAX_CHARS",
        "PRINCIPAL_HASH_SALT",
        "A2A_NAME",
    ):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "api-policy.yaml"
    path.write_text(POLICY, encoding="utf-8")
    monkeypatch.setenv("API_POLICY_PATH", str(path))
    monkeypatch.setenv("ORDERS_AGENT_URL", BASE)
    monkeypatch.setenv("ORDERS_AGENT_KEY", "orders-key")
    monkeypatch.setenv("BILLING_AGENT_URL", "http://billing-agent:8080")
    monkeypatch.setenv("BILLING_AGENT_KEY", "billing-key")
    reset_policy_cache()
    reset_limits()
    reset_a2a_client()
    yield path
    reset_policy_cache()
    reset_limits()
    reset_a2a_client()
    a2a_client._PEERS.clear()


def _runtime(text: str = "Cancel ORD-1002 please", context: Any = None, thread: str = "t1") -> Any:
    return SimpleNamespace(
        context=context if context is not None else dict(ALICE),
        config={"configurable": {"thread_id": thread}},
        state={"messages": [{"type": "human", "content": text}]},
    )


def _client(peer: FakePeer, runtime: Any = None, name: str = "orders") -> A2APeerClient:
    return A2APeerClient(name, runtime=runtime or _runtime(), transport=peer.transport())


# --- requests, cards, contexts ---------------------------------------------------------------


async def test_a_message_is_an_a2a_1_0_request_with_its_version(policy: Path) -> None:
    peer = FakePeer()
    reply = await _client(peer).send("Where is ORD-1002?")
    assert reply.state == "TASK_STATE_COMPLETED"
    assert reply.text == "echo: Where is ORD-1002?"  # the LAST response artifact
    assert peer.calls == ["card", "SendMessage"]
    sent = peer.requests[1]
    assert sent.headers["A2A-Version"] == "1.0"
    body = json.loads(sent.content)
    assert body["jsonrpc"] == "2.0" and body["method"] == "SendMessage"
    request = SendMessageRequest()
    json_format.ParseDict(body["params"], request)  # a valid SDK request
    assert request.message.context_id == context_id_for("t1", "orders_agent", "alice")
    assert request.configuration.history_length == 0
    assert "authorization" in {k.lower() for k in sent.headers}  # the policy's credential


def test_the_context_id_is_keyed_stable_and_uuid_shaped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRINCIPAL_HASH_SALT", raising=False)
    plain = context_id_for("t1", "orders_agent", "alice")
    assert plain == context_id_for("t1", "orders_agent", "alice")  # every turn, every replica
    assert uuid.UUID(plain).version == 4 and "t1" not in plain
    others = {
        context_id_for("t2", "orders_agent", "alice"),
        context_id_for("t1", "billing_agent", "alice"),
        context_id_for("t1", "orders_agent", "bob"),
    }
    assert plain not in others and len(others) == 3
    monkeypatch.setenv("PRINCIPAL_HASH_SALT", "s3cret")
    salted = context_id_for("t1", "orders_agent", "alice")
    assert salted != plain and uuid.UUID(salted).version == 4


async def test_the_card_is_read_once_and_its_url_never_dialed(policy: Path) -> None:
    peer = FakePeer()
    client = _client(peer)
    await client.send("one")
    await client.send("two")
    assert peer.calls == ["card", "SendMessage", "SendMessage"]
    card = await client.card()
    assert card.origin and "\x07" not in card.description and len(card.description) <= 300


@pytest.mark.parametrize(
    ("card", "refusal"),
    [
        (
            {
                "supportedInterfaces": [
                    {
                        "url": "http://evil.test/a2a/orders",
                        "protocolBinding": "JSONRPC",
                        "protocolVersion": "1.0",
                    }
                ]
            },
            "names http://evil.test/a2a/orders as its A2A endpoint, not the URL this agent calls",
        ),
        (
            {
                "supportedInterfaces": [
                    {
                        "url": f"{BASE}/a2a/orders",
                        "protocolBinding": "JSONRPC",
                        "protocolVersion": "0.3.0",
                    }
                ]
            },
            "offers no A2A 1.x JSON-RPC interface",
        ),
        ({"name": "billing"}, "is agent 'billing', not 'orders'"),
    ],
)
async def test_a_card_that_is_not_this_peer_refuses_every_call(
    policy: Path, card: dict[str, Any], refusal: str
) -> None:
    peer = FakePeer()
    peer.card = {**peer.default_card(), **card}
    with pytest.raises(ApiCallError, match=refusal):
        await _client(peer).send("hello")
    assert peer.calls == ["card"]  # nothing was sent, and the card's URL was never dialed
    with pytest.raises(ApiCallError, match=refusal):  # the failure is kept for a while
        await _client(peer).send("hello")
    assert peer.calls == ["card"]


async def test_a_card_behind_a_credential_the_peer_refuses(policy: Path) -> None:
    peer = FakePeer()
    peer.card_status = 401
    with pytest.raises(ApiCallError, match=r"orders refused the credential \(401\)"):
        await _client(peer).send("hello")


def test_locks_and_cards_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(a2a_client, "LOCKS_MAX", 3)
    reset_a2a_client()
    held = a2a_client._lock_for("c0")
    asyncio.run(held.acquire())
    for index in range(1, 6):
        a2a_client._lock_for(f"c{index}")
    assert len(a2a_client._LOCKS) == 3 and "c0" in a2a_client._LOCKS  # a held lock stays
    reset_a2a_client()


async def test_a_busy_peer_is_asked_again_three_times_at_most(
    policy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    waits: list[float] = []

    async def no_wait(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(a2a_client.asyncio, "sleep", no_wait)
    peer = FakePeer()
    peer.busy = 2
    reply = await _client(peer).send("hello")
    assert reply.text == "echo: hello" and waits == [0.5, 1.0]
    peer.busy = 9
    reply = await _client(peer).send("hello")
    assert reply.state == "TASK_STATE_FAILED" and reply.error_code == "thread_busy"
    assert waits == [0.5, 1.0, 0.5, 1.0, 2.0]


async def test_a_cancel_the_peers_other_replica_runs_is_asked_again_once(
    policy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_wait(seconds: float) -> None:
        return None

    monkeypatch.setattr(a2a_client.asyncio, "sleep", no_wait)
    peer = FakePeer()
    peer.cancel_busy = 1
    assert (await _client(peer).cancel("t9")).state == "TASK_STATE_CANCELED"
    peer.cancel_busy = 2
    with pytest.raises(ApiCallError, match="still running on another replica of orders"):
        await _client(peer).cancel("t9")


async def test_a_call_the_policy_does_not_allow_is_never_sent(policy: Path) -> None:
    """billing's entry allows no CancelTask: the allow-list refuses it before sending."""
    peer = FakePeer(name="billing", base="http://billing-agent:8080")
    with pytest.raises(ApiPolicyError, match="rpc_method CancelTask"):
        await _client(peer, name="billing").cancel("t9")
    assert "CancelTask" not in peer.calls


# --- loops and errors --------------------------------------------------------------------


async def test_a_call_back_to_this_agent_or_up_the_chain_is_refused(
    policy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = FakePeer()
    monkeypatch.setenv("A2A_NAME", "orders")
    with pytest.raises(ApiPolicyError, match="would call this agent itself"):
        await _client(peer).send("hello")
    monkeypatch.setenv("A2A_NAME", "billing")
    chained = {
        **ALICE,
        "attributes": {"@actor": {"id": "concierge", "chain": ["concierge"]}},
    }
    monkeypatch.setattr(
        A2APeerClient, "_resolve", staticmethod(lambda peer: ("concierge", "orders_agent"))
    )
    with pytest.raises(
        ApiPolicyError, match=r"loop back through the delegation chain \(concierge -> billing"
    ):
        await _client(peer, _runtime(context=chained)).send("hello")
    assert peer.requests == []


async def test_errors_the_model_reads(policy: Path) -> None:
    with pytest.raises(ApiPolicyError, match="unknown agent 'shipping'; ask one of"):
        A2APeerClient("shipping", runtime=_runtime())
    with pytest.raises(ApiPolicyError, match="is not an A2A peer"):
        A2APeerClient("shop", runtime=_runtime())
    peer = FakePeer()
    await _client(peer).card()
    peer.answer = {"jsonrpc": "2.0", "id": "x", "error": {"code": -32602, "message": "bad " * 200}}
    with pytest.raises(ApiCallError, match="orders answered with something that is not A2A"):
        await _client(peer).send("hello")  # another request's id
    peer.answer = None
    peer.card = None

    def refuse(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "error": {"code": -32602, "message": "bad " * 200},
            },
        )

    client = A2APeerClient("orders", runtime=_runtime(), transport=httpx.MockTransport(refuse))
    reset_a2a_client()
    a2a_client._CARDS.entries[("orders_agent", BASE)] = (
        asyncio.get_running_loop().time() + 60,
        a2a_client.PeerCard(name="orders", description="", origin=False),
    )
    with pytest.raises(PeerRpcError) as refused:
        await client.send("hello")
    assert refused.value.code == -32602
    assert str(refused.value).startswith("orders refused SendMessage: -32602 bad bad")
    assert len(str(refused.value)) < 360


def test_bad_settings_stop_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    client_settings()
    for name, value in (
        ("A2A_FORWARD_ORIGIN", "always"),
        ("A2A_CARD_TTL_S", "-1"),
        ("A2A_REPLY_MAX_CHARS", "0"),
        ("A2A_ORIGIN_MAX_CHARS", "lots"),
    ):
        monkeypatch.setenv(name, value)
        with pytest.raises(SettingsError, match=name):
            client_settings()
        monkeypatch.delenv(name)


# --- the user's own words (the origin extension) ----------------------------------------------


def _sent_origin(peer: FakePeer) -> tuple[Any, str | None]:
    sent = next(r for r in peer.requests if r.method == "POST")
    metadata = json.loads(sent.content)["params"]["message"].get("metadata") or {}
    return metadata.get(A2A_ORIGIN_EXTENSION), sent.headers.get("A2A-Extensions")


async def test_the_users_words_go_to_a_peer_that_reads_them(policy: Path) -> None:
    peer = FakePeer()
    await _client(peer, _runtime("Cancel ORD-1002, it came broken")).send("cancel ORD-1002")
    extension, header = _sent_origin(peer)
    assert header == A2A_ORIGIN_EXTENSION
    assert extension == {
        "origin": {"text": "Cancel ORD-1002, it came broken", "truncated": False, "hops": 1}
    }


async def test_the_words_are_capped_and_forwarded_on_with_one_more_hop(
    policy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("A2A_ORIGIN_MAX_CHARS", "10")
    peer = FakePeer()
    await _client(peer, _runtime("Cancel ORD-1002 now")).send("x")
    assert _sent_origin(peer)[0]["origin"] == {"text": "Cancel ORD", "truncated": True, "hops": 1}
    # An agent calling for the user passes on the words it was given, never its own request.
    delegated = {
        **ALICE,
        "attributes": {
            "@actor": {"id": "concierge", "chain": ["concierge"]},
            "credentials": {"@origin": {"text": "ORD-7", "truncated": False, "hops": 1}},
        },
    }
    peer = FakePeer()
    await _client(peer, _runtime("the concierge's words", context=delegated)).send("x")
    assert _sent_origin(peer)[0]["origin"] == {"text": "ORD-7", "truncated": False, "hops": 2}
    without = {**delegated, "attributes": {"@actor": {"id": "concierge"}}}
    peer = FakePeer()
    await _client(peer, _runtime("the concierge's words", context=without)).send("x")
    assert _sent_origin(peer) == (None, None)


async def test_the_words_stay_here_when_off_or_when_the_peer_does_not_read_them(
    policy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("A2A_FORWARD_ORIGIN", "off")
    peer = FakePeer()
    await _client(peer).send("x")
    assert _sent_origin(peer) == (None, None)
    monkeypatch.delenv("A2A_FORWARD_ORIGIN")
    reset_a2a_client()
    peer = FakePeer()
    peer.card = {**peer.default_card(), "capabilities": {"streaming": True}}
    await _client(peer).send("x")
    assert _sent_origin(peer) == (None, None)


# --- the tools --------------------------------------------------------------------------------


def test_the_tools_name_the_peers_and_fail_closed(policy: Path) -> None:
    from langchain_core.utils.function_calling import convert_to_openai_tool

    ask, relay = peer_tools(PEERS)
    ask_schema = convert_to_openai_tool(ask)["function"]
    assert ask_schema["name"] == "ask_agent"
    assert ask_schema["parameters"]["properties"]["agent"]["enum"] == ["orders", "billing"]
    assert "- orders: Orders agent: reads the caller's orders" in ask_schema["description"]
    relay_schema = convert_to_openai_tool(relay)["function"]
    assert relay_schema["name"] == "approve_agent_action"
    # Only the peers this agent relays to (one: a JSON-schema constant).
    assert relay_schema["parameters"]["properties"]["agent"]["const"] == "orders"
    with pytest.raises(ApiPolicyError, match="'shop' is not a protocol: a2a API"):
        peer_tools({"shop": {"api": "shop", "approvals": "deny"}})
    assert len(peer_tools({"billing": PEERS["billing"]})) == 1  # no relay: no approve tool


# --- the relay, in a graph that pauses at the approve gate -------------------------------------


@pytest.fixture
def store() -> ApprovalStore:
    return ApprovalStore(Database("memory"))


@pytest.fixture
def relay_graph(policy: Path, store: ApprovalStore) -> Iterator[tuple[Any, FakePeer]]:
    """An agent with the peer tools (approve_agent_action first: the fake model calls the first
    tool a message mentions), the fake peer and the ledger `store`."""
    from {{cookiecutter.agent_directory}} import agent
    from {{cookiecutter.agent_directory}}.app_utils.model import get_model

    peer = FakePeer()
    set_approval_ledger(store)
    tools = list(reversed(peer_tools(PEERS, transport=peer.transport())))
    graph = create_agent(
        model=get_model(),
        tools=tools,
        system_prompt=agent.SYSTEM_PROMPT,
        middleware=agent.middleware(),
        context_schema=agent.AgentContext,
        checkpointer=InMemorySaver(),
    )
    yield graph, peer
    set_approval_ledger(None)


async def _run(graph: Any, graph_input: Any) -> list[Any]:
    from {{cookiecutter.agent_directory}}.agent import AgentContext

    config = {"configurable": {"thread_id": "t1"}}
    context = AgentContext(principal_id="alice", roles=["user"])
    async for _ in graph.astream(graph_input, config, context=context, stream_mode="updates"):
        pass
    return list((await graph.aget_state(config)).interrupts)


async def _tool_result(graph: Any) -> str:
    state = await graph.aget_state({"configurable": {"thread_id": "t1"}})
    return next(m.content for m in reversed(state.values["messages"]) if m.type == "tool")


async def _ask(graph: Any) -> str:
    """ask_agent asks orders to cancel: it pauses there, and reports the task to relay."""
    assert await _run(graph, {"messages": [("user", "Ask orders about cancel ORD-1002")]}) == []
    text = await _tool_result(graph)
    start = text.index("{")
    result = json.loads(text[start : text.rindex("}") + 1])
    assert result["status"] == "needs_user_approval" and result["agent"] == "orders"
    return str(result["task_id"])


async def _pause(graph: Any, store: ApprovalStore, task_id: str) -> tuple[Any, Any]:
    interrupts = await _run(graph, {"messages": [("user", f"approve_agent_action for {task_id}")]})
    assert len(interrupts) == 1
    record, _, _ = await store.add(
        record_from_interrupt(
            interrupts[0].value,
            interrupt_id=interrupts[0].id,
            thread_id="t1",
            run_id="r",
            requester=Principal(id="alice", roles=["user"]),
        )
    )
    return interrupts[0], record


async def test_the_relay_shows_the_person_what_happens_and_sends_it_once(
    relay_graph: tuple[Any, FakePeer], store: ApprovalStore
) -> None:
    graph, peer = relay_graph
    task_id = await _ask(graph)
    interrupt, record = await _pause(graph, store, task_id)
    public = record.public()
    assert public["a2a_operation"] == "approve"
    assert public["effect"]["path"] == "/orders/ORD-1002/cancel"
    assert public["effect"]["via"] == ["orders"]
    assert public["nested"]["call"]["body"]["big"] == 12345678901234567890  # exact
    assert public["nested"]["digest"] == _approval()["digest"]
    assert peer.calls[-1] == "GetTask"  # read, nothing decided yet
    decided = await store.decide(record.approval_id, APPROVED, "alice", None)
    await _run(graph, Command(resume={interrupt.id: decision_value(decided, "approve")}))
    assert "Cancelled ORD-1002." in await _tool_result(graph)
    assert [c for c in peer.calls if c.startswith("SendMessage")] == [
        "SendMessage",  # the ask
        "SendMessage approve",  # once
    ]
    decision = json.loads(peer.requests[-1].content)
    message = decision["params"]["message"]
    assert decision["id"] == "approve-de2e"
    assert message["parts"] == [
        {"data": {"approval_id": "de2e", "decision": "approve", "digest": _approval()["digest"]}}
    ]
    assert "taskId" not in message and message["referenceTaskIds"] == [task_id]
    assert message["contextId"] == context_id_for("t1", "orders_agent", "alice")
    assert message["metadata"][A2A_ORIGIN_EXTENSION]["approving"]["approval_id"] == "de2e"
    assert (await store.get(record.approval_id)).used_at is not None


async def test_after_the_peer_lost_the_task_its_ledger_is_read_and_one_decision_sent(
    relay_graph: tuple[Any, FakePeer], store: ApprovalStore
) -> None:
    graph, peer = relay_graph
    task_id = await _ask(graph)
    peer.lost = True
    interrupt, record = await _pause(graph, store, task_id)
    assert peer.calls[-2:] == ["GetTask", "ledger"]
    decided = await store.decide(record.approval_id, APPROVED, "alice", None)
    await _run(graph, Command(resume={interrupt.id: decision_value(decided, "approve")}))
    assert [c for c in peer.calls if c.startswith("SendMessage ")] == ["SendMessage approve"]


async def test_a_rejection_tells_the_peer_and_nothing_is_done(
    relay_graph: tuple[Any, FakePeer], store: ApprovalStore
) -> None:
    graph, peer = relay_graph
    task_id = await _ask(graph)
    interrupt, record = await _pause(graph, store, task_id)
    decided = await store.decide(record.approval_id, REJECTED, "alice", "no")
    await _run(graph, Command(resume={interrupt.id: decision_value(decided, "reject")}))
    result = await _tool_result(graph)
    assert "The user rejected it; orders was told and did nothing" in result
    assert [c for c in peer.calls if c.startswith("SendMessage ")] == ["SendMessage reject"]


async def test_a_task_of_another_conversation_is_refused(
    relay_graph: tuple[Any, FakePeer], store: ApprovalStore
) -> None:
    graph, peer = relay_graph
    task_id = await _ask(graph)
    peer.tasks[task_id]["contextId"] = "someone-elses-context"
    assert await _run(graph, {"messages": [("user", f"approve_agent_action for {task_id}")]}) == []
    assert "belongs to another conversation" in await _tool_result(graph)
    assert not any(c.startswith("SendMessage ") for c in peer.calls)


async def test_a_gate_the_person_decides_at_the_peer_is_reported_not_relayed(
    relay_graph: tuple[Any, FakePeer], store: ApprovalStore
) -> None:
    graph, peer = relay_graph
    peer.approvals = [_approval(decide_with="direct")]
    assert await _run(graph, {"messages": [("user", "Ask orders about cancel ORD-1002")]}) == []
    asked = await _tool_result(graph)
    assert '"status": "needs_direct_approval"' in asked
    task_id = next(iter(peer.tasks))
    assert await _run(graph, {"messages": [("user", f"approve_agent_action for {task_id}")]}) == []
    result = await _tool_result(graph)
    assert '"status": "needs_direct_approval"' in result
    assert "graph-agents-cli approvals approve de2e --url http://orders-agent:8080" in result
    assert '"path": "/orders/ORD-1002/cancel"' in result  # the effect
    assert not any(c.startswith("SendMessage ") for c in peer.calls)  # no gated call


async def test_a_peer_waiting_for_nothing_is_said_so(
    relay_graph: tuple[Any, FakePeer], store: ApprovalStore
) -> None:
    graph, peer = relay_graph
    await _client(peer).send("hello")
    done = next(iter(peer.tasks))
    assert await _run(graph, {"messages": [("user", f"approve_agent_action for {done}")]}) == []
    assert "orders is not waiting for an approval (its task is completed)" in (
        await _tool_result(graph)
    )
