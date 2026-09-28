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

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from {{cookiecutter.agent_directory}}.app_utils.api_client import (
    ApiClient,
    ApiPolicy,
    ApiPolicyError,
    get_client,
    reset_limits,
    reset_policy_cache,
)

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
