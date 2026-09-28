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

"""`protocol: jsonrpc|a2a` in api-policy.yaml: the schema, what a request is, and matching.

Everything here lives in the SHARED block, so every case runs through both
copies (the CLI's and the template runtime's `api_client.py`), which must agree
to the error text. The property tests compare today's rules with the frozen
0.2.0 rules (`api_policy_rules_v0_2.py`): an entry that pins none of the fields
0.3 added allows, denies and gates every call exactly as 0.2 did, whatever the
request's derived JSON-RPC method and decision; and deriving what a request is
fails closed (a message naming an approval never reads as anything but
`approve`, `reject` when every part rejects, or a refusal).
"""

from __future__ import annotations

import copy
import importlib.util
import json
import random
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from graph_agents_cli import _api_policy as cli

TEMPLATE_CLIENT = (
    Path(cli.__file__).parent / "scaffold/agents/langgraph/app/app_utils/api_client.py"
)
V02_RULES = Path(__file__).with_name("api_policy_rules_v0_2.py")


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def runtime() -> ModuleType:
    return _load("_rpc_runtime_api_client", TEMPLATE_CLIENT)


@pytest.fixture(scope="module")
def v02() -> ModuleType:
    return _load("_rpc_api_policy_v0_2", V02_RULES)


@pytest.fixture(params=["cli", "runtime"])
def rules(request: pytest.FixtureRequest, runtime: ModuleType) -> ModuleType:
    """Each case once per copy of the SHARED block."""
    return cli if request.param == "cli" else runtime


def _errors(document: Any, runtime: ModuleType) -> list[str]:
    errors = cli.policy_errors(document)
    assert runtime.policy_errors(copy.deepcopy(document)) == errors
    return errors


# --- the schema -----------------------------------------------------------------------------

PEER_PATH = "/a2a/orders"


def _peer(**overrides: Any) -> dict[str, Any]:
    """An A2A peer as `peer add` writes one (approve gated for the requester)."""
    api: dict[str, Any] = {
        "description": "Orders agent: reads and cancels the caller's orders.",
        "protocol": "a2a",
        "a2a": {"path": PEER_PATH},
        "base_url_env": "ORDERS_AGENT_URL",
        "auth": "exchange",
        "exchange": {"audience": "orders"},
        "allowed_methods": ["GET", "POST"],
        "allowed_operations": [
            {
                "operationId": "getAgentCard",
                "methods": ["GET"],
                "path": "/a2a/orders/.well-known/agent-card.json",
            },
            {"rpc_method": "SendMessage", "methods": ["POST"], "path": PEER_PATH},
            {"rpc_method": "GetTask", "methods": ["POST"], "path": PEER_PATH},
            {
                "operationId": "listContextApprovals",
                "methods": ["GET"],
                "path": "/threads/{context_id}/approvals",
            },
        ],
        "approval": {
            "required_for": {"operations": [{"a2a_operation": "approve"}]},
            "approvers": ["requester"],
            "timeout_s": 900,
        },
    }
    api.update(overrides)
    return {key: value for key, value in api.items() if value is not None}


def _doc(**apis: dict[str, Any]) -> dict[str, Any]:
    return {"apis": apis}


def _rpc_api(**overrides: Any) -> dict[str, Any]:
    api: dict[str, Any] = {
        "protocol": "jsonrpc",
        "base_url_env": "RPC_URL",
        "auth": "bearer",
        "token_env": "RPC_TOKEN",
        "allowed_methods": ["POST"],
    }
    api.update(overrides)
    return {key: value for key, value in api.items() if value is not None}


VALID = [
    _doc(orders_agent=_peer()),
    _doc(orders_agent=_peer(approval=None, denied_operations=[{"a2a_operation": "approve"}])),
    # Gating every POST covers every message that approves.
    _doc(
        orders_agent=_peer(
            approval={"required_for": {"methods": ["POST"]}, "approvers": ["requester"]}
        )
    ),
    _doc(
        orders_agent=_peer(approval={"required_for": {"methods": ["*"]}, "approvers": ["role:x"]})
    ),
    # An allow-list that cannot send a message needs no gate.
    _doc(
        orders_agent=_peer(
            approval=None,
            allowed_operations=[{"rpc_method": "GetTask", "path": PEER_PATH}],
        )
    ),
    _doc(orders_agent=_peer(approval=None, allowed_methods=["GET", "HEAD"])),
    _doc(
        orders_agent=_peer(
            approval=None,
            allowed_operations=[{"a2a_operation": "reject", "rpc_method": "SendMessage"}],
        )
    ),
    _doc(orders_agent=_peer(auth="bearer", token_env="ORDERS_KEY", exchange=None)),
    _doc(orders_agent=_peer(auth="forward", exchange=None)),
    _doc(rpc=_rpc_api()),
    _doc(rpc=_rpc_api(allowed_methods=["GET", "POST", "HEAD"], description="A JSON-RPC API.")),
    _doc(
        rpc=_rpc_api(
            allowed_operations=[{"rpc_method": "eth_getBalance"}, {"rpc_method": "rpc.discover"}],
            denied_operations=[{"rpc_method": "admin/shutdown", "methods": ["POST"]}],
            approval={
                "required_for": {"operations": [{"rpc_method": "transfer"}]},
                "approvers": ["requester"],
            },
        )
    ),
    # 0.3 method names are ordinary names on a plain JSON-RPC API.
    _doc(rpc=_rpc_api(allowed_operations=[{"rpc_method": "message/send"}])),
    # `description` and `protocol: http` are valid on any API.
    _doc(
        a={
            "description": "Any API may say what it is for.",
            "protocol": "http",
            "base_url_env": "A_URL",
            "auth": "none",
            "allowed_methods": ["*"],
        }
    ),
]


@pytest.mark.parametrize("document", VALID)
def test_valid_documents(document: dict[str, Any], runtime: ModuleType) -> None:
    assert _errors(document, runtime) == []


INVALID = [
    (
        _doc(
            a={"protocol": "grpc", "base_url_env": "A", "auth": "none", "allowed_methods": ["GET"]}
        ),
        "apis.a.protocol: must be one of http, jsonrpc, a2a (got 'grpc')",
    ),
    (
        _doc(rpc=_rpc_api(allowed_methods=["GET", "POST", "DELETE"])),
        "apis.rpc.allowed_methods: protocol jsonrpc allows GET, POST and HEAD only (a JSON-RPC "
        "request is a POST), not DELETE",
    ),
    (
        _doc(rpc=_rpc_api(allowed_methods=["*"])),
        "apis.rpc.allowed_methods: protocol jsonrpc allows GET, POST and HEAD only",
    ),
    (_doc(orders_agent=_peer(a2a=None)), "apis.orders_agent.a2a: required with protocol a2a"),
    (
        _doc(rpc=_rpc_api(a2a={"path": "/a2a/x"})),
        "apis.rpc.a2a: only valid with protocol a2a",
    ),
    (_doc(orders_agent=_peer(a2a="/a2a/orders")), "apis.orders_agent.a2a: must be a mapping"),
    (_doc(orders_agent=_peer(a2a={})), "apis.orders_agent.a2a.path: required"),
    (
        _doc(orders_agent=_peer(a2a={"path": "/a2a/{name}"})),
        "apis.orders_agent.a2a.path: must be the literal path of one endpoint",
    ),
    (_doc(orders_agent=_peer(a2a={"path": "/"})), "must be the literal path of one endpoint"),
    (_doc(orders_agent=_peer(a2a={"path": "a2a"})), "apis.orders_agent.a2a.path: must be"),
    (
        _doc(orders_agent=_peer(a2a={"path": PEER_PATH, "url": "x"})),
        "apis.orders_agent.a2a: unknown key 'url'",
    ),
    (
        _doc(orders_agent=_peer(auth="none", exchange=None)),
        "apis.orders_agent.auth: protocol a2a needs a credential",
    ),
    (_doc(orders_agent=_peer(description="")), "apis.orders_agent.description: must be text"),
    (_doc(orders_agent=_peer(description="x" * 301)), "of 1-300 characters"),
    (_doc(orders_agent=_peer(description="a\nb")), "without control characters"),
    (_doc(orders_agent=_peer(description=7)), "apis.orders_agent.description: must be text"),
    # rpc_method and a2a_operation belong to JSON-RPC APIs.
    (
        _doc(
            a={
                "base_url_env": "A",
                "auth": "none",
                "allowed_methods": ["POST"],
                "allowed_operations": [{"rpc_method": "x", "path": "/x"}],
            }
        ),
        "apis.a.allowed_operations[0].rpc_method: only valid with protocol jsonrpc or a2a",
    ),
    (
        _doc(rpc=_rpc_api(denied_operations=[{"a2a_operation": "approve"}])),
        "apis.rpc.denied_operations[0].a2a_operation: only valid with protocol a2a",
    ),
    (
        _doc(rpc=_rpc_api(allowed_operations=[{"rpc_method": "9lives"}])),
        "apis.rpc.allowed_operations[0].rpc_method: must be a JSON-RPC method name",
    ),
    (
        _doc(rpc=_rpc_api(allowed_operations=[{"rpc_method": "x" * 65}])),
        "must be a JSON-RPC method name",
    ),
    (
        _doc(orders_agent=_peer(denied_operations=[{"rpc_method": "tasks/cancel"}])),
        "apis.orders_agent.denied_operations[0].rpc_method: tasks/cancel is the A2A 0.3 name; "
        "write CancelTask",
    ),
    (
        _doc(orders_agent=_peer(denied_operations=[{"a2a_operation": "maybe"}])),
        "apis.orders_agent.denied_operations[0].a2a_operation: must be approve or reject",
    ),
    (
        _doc(
            orders_agent=_peer(
                denied_operations=[{"a2a_operation": "approve", "rpc_method": "GetTask"}]
            )
        ),
        "a2a_operation: goes with rpc_method SendMessage or SendStreamingMessage",
    ),
    (
        _doc(rpc=_rpc_api(allowed_operations=[{"methods": ["POST"]}])),
        "apis.rpc.allowed_operations[0]: needs operationId, path, rpc_method and/or a2a_operation",
    ),
    (
        _doc(
            orders_agent=_peer(
                approval={
                    "required_for": {"operations": [{"a2a_operation": "approve", "x": 1}]},
                    "approvers": ["requester"],
                }
            )
        ),
        "apis.orders_agent.approval.required_for.operations[0]: unknown key 'x'",
    ),
]


@pytest.mark.parametrize(("document", "expected"), INVALID)
def test_invalid_documents(document: dict[str, Any], expected: str, runtime: ModuleType) -> None:
    errors = _errors(document, runtime)
    assert any(expected in error for error in errors), errors


APPROVE_REFUSED = (
    "apis.orders_agent: protocol a2a allows SendMessage, so this agent could decide approvals at "
    "orders: gate them (graph-agents-cli api approval orders_agent --a2a-operations approve "
    "--approvers requester) or deny them (graph-agents-cli api deny orders_agent "
    "--a2a-operation approve)"
)


@pytest.mark.parametrize(
    "api",
    [
        _peer(approval=None),
        # Every operation within POST: any message.
        _peer(approval=None, allowed_operations=None),
        # A label alone: a call so labelled may carry any body.
        _peer(approval=None, allowed_operations=[{"operationId": "ask", "methods": ["POST"]}]),
        _peer(approval=None, allowed_operations=[{"path": PEER_PATH}]),
        _peer(approval=None, allowed_operations=[{"rpc_method": "SendStreamingMessage"}]),
        _peer(approval=None, allowed_operations=[{"a2a_operation": "approve"}]),
        # A gate on SendMessage leaves SendStreamingMessage's approvals out.
        _peer(
            approval={
                "required_for": {"operations": [{"rpc_method": "SendMessage"}]},
                "approvers": ["requester"],
            }
        ),
        # A gate on another method, or on approve with methods that are not POST.
        _peer(approval={"required_for": {"methods": ["GET"]}, "approvers": ["requester"]}),
        _peer(
            approval={
                "required_for": {"operations": [{"a2a_operation": "approve", "methods": ["GET"]}]},
                "approvers": ["requester"],
            }
        ),
        # A denial by path is not every approve.
        _peer(approval=None, denied_operations=[{"path": "/elsewhere"}]),
        _peer(approval=None, denied_operations=[{"a2a_operation": "reject"}]),
    ],
)
def test_an_a2a_api_that_may_send_an_approve_must_gate_or_deny_it(
    api: dict[str, Any], runtime: ModuleType
) -> None:
    assert _errors(_doc(orders_agent=api), runtime) == [APPROVE_REFUSED]


def test_the_approve_rule_names_the_agent_by_its_endpoint(runtime: ModuleType) -> None:
    assert _errors(
        _doc(orders_agent=_peer(approval=None, a2a={"path": "/rpc/orders-v2/"})), runtime
    ) == [APPROVE_REFUSED.replace("approvals at orders:", "approvals at orders-v2:")]


# --- what a request is (derive_rpc) -----------------------------------------------------------

A2A = {"protocol": "a2a"}
JSONRPC = {"protocol": "jsonrpc"}


def _request(method: str = "SendMessage", params: Any = None, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": "1", "method": method}
    if params is not None:
        body["params"] = params
    body.update(extra)
    return body


def _message(*parts: Any) -> dict[str, Any]:
    return {"message": {"role": "ROLE_USER", "messageId": "m", "parts": list(parts)}}


def _decision(decision: Any, **more: Any) -> dict[str, Any]:
    return {"data": {"approval_id": "a1", "decision": decision, **more}}


TEXT = {"text": "hello"}


@pytest.mark.parametrize(
    ("api", "method", "body", "expected"),
    [
        ({}, "POST", {"anything": 1}, (None, None)),
        ({"protocol": "http"}, "POST", [1, 2], (None, None)),
        (JSONRPC, "POST", _request("eth_call", [1]), ("eth_call", None)),
        (
            JSONRPC,
            "POST",
            _request("message/send", _message(_decision("approve"))),
            (
                "message/send",
                None,
            ),
        ),
        (JSONRPC, "GET", None, (None, None)),
        (A2A, "HEAD", None, (None, None)),
        (A2A, "POST", _request("GetTask", {"id": "t"}), ("GetTask", None)),
        (A2A, "post", _request("tasks/get", {"id": "t"}), ("GetTask", None)),
        (A2A, "POST", _request("tasks/cancel", {"id": "t"}), ("CancelTask", None)),
        (A2A, "POST", _request("tasks/resubscribe", {}), ("SubscribeToTask", None)),
        (
            A2A,
            "POST",
            _request("tasks/pushNotificationConfig/list", {}),
            ("ListTaskPushNotificationConfigs", None),
        ),
        (
            A2A,
            "POST",
            _request("agent/getAuthenticatedExtendedCard"),
            ("GetExtendedAgentCard", None),
        ),
        (A2A, "POST", _request("SendMessage", _message(TEXT)), ("SendMessage", None)),
        (
            A2A,
            "POST",
            _request("SendMessage", _message(_decision("approve"))),
            (
                "SendMessage",
                "approve",
            ),
        ),
        (
            A2A,
            "POST",
            _request("message/send", _message(_decision("approve"))),
            (
                "SendMessage",
                "approve",
            ),
        ),
        (
            A2A,
            "POST",
            _request("message/stream", _message(_decision("reject"))),
            (
                "SendStreamingMessage",
                "reject",
            ),
        ),
        (
            A2A,
            "POST",
            _request("SendMessage", _message(TEXT, _decision("reject"))),
            (
                "SendMessage",
                "reject",
            ),
        ),
        # Approve wins: any part naming an approval that does not say exactly reject.
        (
            A2A,
            "POST",
            _request("SendMessage", _message(_decision("reject"), _decision("approve"))),
            ("SendMessage", "approve"),
        ),
        (
            A2A,
            "POST",
            _request("SendMessage", _message(_decision("REJECT"))),
            (
                "SendMessage",
                "approve",
            ),
        ),
        (
            A2A,
            "POST",
            _request("SendMessage", _message({"data": {"approval_id": "a"}})),
            (
                "SendMessage",
                "approve",
            ),
        ),
        (
            A2A,
            "POST",
            _request("SendMessage", _message({"data": {"decision": None}})),
            (
                "SendMessage",
                "approve",
            ),
        ),
        (
            A2A,
            "POST",
            _request("SendMessage", _message({"kind": "data", "data": {"decision": "reject"}})),
            ("SendMessage", "reject"),
        ),
        # Not a decision: nested deeper, or data that is not an object (as the callee reads it).
        (
            A2A,
            "POST",
            _request("SendMessage", _message({"data": {"x": {"decision": "approve"}}})),
            ("SendMessage", None),
        ),
        (
            A2A,
            "POST",
            _request("SendMessage", _message({"data": "approve"})),
            (
                "SendMessage",
                None,
            ),
        ),
        (
            A2A,
            "POST",
            _request("SendMessage", {"message": {"role": "ROLE_USER"}}),
            (
                "SendMessage",
                None,
            ),
        ),
        # Read as the JSON sent: a tuple of parts is a list on the wire.
        (
            A2A,
            "POST",
            _request("SendMessage", {"message": {"parts": (_decision("approve"),)}}),
            ("SendMessage", "approve"),
        ),
        (A2A, "POST", _request("SendMessage", _message(TEXT), id=7), ("SendMessage", None)),
        # Another letter case: an A2A server answers it as unknown, but it is read for a
        # decision all the same (fail closed toward a lenient server).
        (
            A2A,
            "POST",
            _request("sendmessage", _message(_decision("approve"))),
            ("sendmessage", "approve"),
        ),
        (
            A2A,
            "POST",
            _request("Message/Send", _message(_decision("reject"))),
            ("Message/Send", "reject"),
        ),
    ],
)
def test_what_a_request_is(
    rules: ModuleType, api: dict[str, Any], method: str, body: Any, expected: tuple[Any, Any]
) -> None:
    rpc = rules.derive_rpc(api, method, body)
    assert (rpc.rpc_method, rpc.a2a_operation) == expected


@pytest.mark.parametrize(
    ("api", "method", "body", "why"),
    [
        (A2A, "POST", [_request("GetTask")], "a batch"),
        (A2A, "POST", [], "a batch"),
        (A2A, "POST", "GetTask", "not a JSON-RPC request object"),
        (A2A, "POST", None, "not a JSON-RPC request object"),
        (JSONRPC, "POST", {"jsonrpc": "2.0", "method": "x"}, "a notification (no id)"),
        (A2A, "POST", _request(id=None), "an id that is not a string or an integer"),
        (A2A, "POST", _request(id=True), "an id that is not a string or an integer"),
        (A2A, "POST", _request(id=1.5), "an id that is not a string or an integer"),
        (A2A, "POST", {"jsonrpc": "1.0", "id": 1, "method": "x"}, 'jsonrpc is not "2.0"'),
        (A2A, "POST", {"id": 1, "method": "x"}, 'jsonrpc is not "2.0"'),
        (A2A, "POST", _request(method=""), "no method name"),
        (A2A, "POST", {"jsonrpc": "2.0", "id": 1, "method": 5}, "no method name"),
        (A2A, "POST", _request(params="x"), "params that are not an object or an array"),
        (A2A, "POST", _request(extra=1), "members other than jsonrpc, method, params and id"),
        (
            A2A,
            "POST",
            _request(_method="GET"),
            "members other than jsonrpc, method, params and id",
        ),
        (A2A, "POST", _request(params={"x": float("nan")}), "not plain JSON"),
        (A2A, "POST", _request(params={"x": {1, 2}}), "not plain JSON"),
    ],
)
def test_anything_but_one_json_rpc_request_is_refused(
    rules: ModuleType, api: dict[str, Any], method: str, body: Any, why: str
) -> None:
    with pytest.raises(rules.RpcRequestError) as refused:
        rules.derive_rpc(api, method, body)
    assert str(refused.value) == (
        f"protocol {api['protocol']} sends one JSON-RPC request per call (a batch or "
        f"non-request body refused: {why})"
    )


@pytest.mark.parametrize(
    ("method", "body", "message"),
    [
        ("GET", {"q": 1}, "protocol a2a: a GET sends no body (a JSON-RPC request is a POST)"),
        ("HEAD", [], "protocol a2a: a HEAD sends no body (a JSON-RPC request is a POST)"),
        (
            "POST",
            _request("SendMessage", {"message": "hi"}),
            "protocol a2a: a message request needs params.message with a list of parts",
        ),
        ("POST", _request("SendMessage"), "needs params.message with a list of parts"),
        ("POST", _request("SendMessage", [1]), "needs params.message with a list of parts"),
        (
            "POST",
            _request("message/send", {"message": {"parts": {"data": {"decision": "x"}}}}),
            "needs params.message with a list of parts",
        ),
    ],
)
def test_a_message_or_a_read_that_cannot_be_read_is_refused(
    rules: ModuleType, method: str, body: Any, message: str
) -> None:
    with pytest.raises(rules.RpcRequestError, match=re.escape(message)):
        rules.derive_rpc(A2A, method, body)


# --- matching -----------------------------------------------------------------------------


def _refusal(rules: ModuleType, api: dict[str, Any], body: Any, **call: Any) -> str | None:
    rpc = rules.derive_rpc(api, call.get("method", "POST"), body)
    return rules.refusal_reason(
        api,
        call.get("method", "POST"),
        call.get("operation_id"),
        call.get("path", PEER_PATH),
        rpc_method=rpc.rpc_method,
        a2a_operation=rpc.a2a_operation,
    )


def _gate(rules: ModuleType, api: dict[str, Any], body: Any, **call: Any) -> Any:
    rpc = rules.derive_rpc(api, call.get("method", "POST"), body)
    return rules.gated(
        api,
        call.get("method", "POST"),
        call.get("operation_id"),
        call.get("path", PEER_PATH),
        rpc_method=rpc.rpc_method,
        a2a_operation=rpc.a2a_operation,
    )


APPROVE = _request("SendMessage", _message(_decision("approve")))
REJECT = _request("SendMessage", _message(_decision("reject")))
ASK = _request("SendMessage", _message(TEXT))
GET_TASK = _request("GetTask", {"id": "t1"})


def test_an_allow_must_show_every_field_it_pins(rules: ModuleType) -> None:
    api = _peer()
    assert _refusal(rules, api, ASK) is None
    assert _refusal(rules, api, GET_TASK) is None
    # SendMessage allows messages that decide: the gate, not the allow-list, holds them.
    assert _refusal(rules, api, APPROVE) is None and _refusal(rules, api, REJECT) is None
    assert _refusal(rules, api, _request("CancelTask", {"id": "t1"})) == "not in allowed_operations"
    assert _refusal(rules, api, _request("tasks/cancel", {"id": "t1"})) == (
        "not in allowed_operations"
    )
    # The pinned path must match too (AND).
    assert _refusal(rules, api, GET_TASK, path="/a2a/billing") == "not in allowed_operations"
    # A call without a JSON-RPC method (a GET) never matches an entry pinning one.
    only_rpc = _peer(approval=None, allowed_operations=[{"rpc_method": "GetTask"}])
    assert _refusal(rules, only_rpc, None, method="GET") == "not in allowed_operations"
    # Letter case counts for an allow.
    exact = _peer(approval=None, allowed_operations=[{"rpc_method": "gettask"}])
    assert _refusal(rules, exact, GET_TASK) == "not in allowed_operations"
    # An allow of reject alone lets rejections through, nothing else.
    rejects = _peer(approval=None, allowed_operations=[{"a2a_operation": "reject"}])
    assert _refusal(rules, rejects, REJECT) is None
    assert _refusal(rules, rejects, ASK) == "not in allowed_operations"


def test_a_denial_holds_whatever_the_path_label_or_spelling(rules: ModuleType) -> None:
    api = _peer(
        approval=None,
        denied_operations=[
            {"a2a_operation": "approve", "path": "/not/where/it/goes"},
            {"rpc_method": "CancelTask", "path": "/elsewhere", "methods": ["POST"]},
        ],
        allowed_operations=None,
    )
    for body in (APPROVE, _request("message/stream", _message(_decision("approve")))):
        for path in (PEER_PATH, "/a2a/other", "/"):
            assert _refusal(rules, api, body, path=path, operation_id="ask") == (
                "denied by denied_operations (path=/not/where/it/goes a2a_operation=approve)"
            )
    for method in ("CancelTask", "tasks/cancel", "canceltask"):
        assert _refusal(rules, api, _request(method, {"id": "t"})) == (
            "denied by denied_operations (path=/elsewhere methods=['POST'] rpc_method=CancelTask)"
        )
    # The path of a JSON-RPC entry does not widen it either: other calls there go through.
    assert _refusal(rules, api, ASK, path="/not/where/it/goes") is None
    assert _refusal(rules, api, REJECT) is None
    assert _refusal(rules, api, GET_TASK, path="/elsewhere") is None
    # A denial that pins another method does not hold a POST.
    get_only = _peer(
        approval=None, denied_operations=[{"a2a_operation": "approve", "methods": ["GET"]}]
    )
    get_only["approval"] = _peer()["approval"]
    assert _refusal(rules, get_only, APPROVE) is None


def test_a_denial_by_label_on_a_json_rpc_entry(rules: ModuleType) -> None:
    api = _peer(denied_operations=[{"operationId": "cancelIt", "rpc_method": "CancelTask"}])
    assert _refusal(rules, api, ASK, operation_id="CANCELIT") is not None
    # A call that names no label is not refused for it: its method is known.
    assert _refusal(rules, api, ASK) is None


def test_a_gate_on_approve_holds_every_message_that_approves(rules: ModuleType) -> None:
    api = _peer()
    for body in (
        APPROVE,
        _request("SendStreamingMessage", _message(_decision("approve"))),
        _request("message/send", _message(TEXT, _decision("maybe"))),
        _request("SENDMESSAGE", _message(_decision("approve"))),
    ):
        gate = _gate(rules, api, body, operation_id="justAsking", path="/a2a/else")
        assert gate is not None
        assert gate.rule == (
            "approval.required_for.operations (a2a_operation=approve)"
        ) and gate.approvers == ("requester",)
    for body in (ASK, REJECT, GET_TASK):
        assert _gate(rules, api, body) is None


def test_a_gate_by_json_rpc_method(rules: ModuleType) -> None:
    api = _rpc_api(
        approval={
            "required_for": {"operations": [{"rpc_method": "transfer"}]},
            "approvers": ["role:finance"],
        }
    )
    assert _gate(rules, api, _request("Transfer", {}), path="/rpc") is not None
    assert _gate(rules, api, _request("balance", {}), path="/rpc") is None


# --- the label (T11) --------------------------------------------------------------------------


def test_a_label_must_name_the_request(rules: ModuleType) -> None:
    api = _peer(
        allowed_operations=[
            *_peer()["allowed_operations"],
            {"operationId": "sendMessage", "rpc_method": "SendMessage", "a2a_operation": "reject"},
            {"operationId": "getTask", "rpc_method": "GetTask"},
        ]
    )

    def problem(label: str | None, body: Any) -> str | None:
        rpc = rules.derive_rpc(api, "POST", body)
        return rules.label_problem(api, label, rpc.rpc_method, rpc.a2a_operation)

    assert problem("sendMessage", APPROVE) == (
        "operation_id 'sendMessage' does not match the request (a2a_operation approve); refused"
    )
    assert problem("SENDMESSAGE", ASK) == (
        "operation_id 'SENDMESSAGE' does not match the request (a2a_operation none); refused"
    )
    assert problem("getTask", ASK) == (
        "operation_id 'getTask' does not match the request (rpc_method SendMessage); refused"
    )
    assert problem("sendMessage", REJECT) is None
    assert problem("getTask", GET_TASK) is None
    assert problem("anythingElse", APPROVE) is None
    assert problem(None, APPROVE) is None
    # Labels on an http API are the tool's own business (as in 0.2).
    assert rules.label_problem({"allowed_operations": []}, "x", "GetTask", "approve") is None


# --- 0.2 entries match exactly as in 0.2 (property) ----------------------------------------

_IDS = [None, "cancelOrder", "CANCELORDER", "getOrder", "x"]
_PATHS = [
    None,
    "/orders",
    "/orders/{order_id}",
    "/orders/{id}/cancel",
    "/ORDERS/7/cancel",
    "/orders/7/cancel.json",
    "/orders/7/cancel/",
    "/a2a/orders",
    "/%61dmin",
    "/admin",
]
_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
_RPC = [
    None,
    "SendMessage",
    "sendmessage",
    "GetTask",
    "CancelTask",
    "SendStreamingMessage",
    "eth_call",
]
_OPERATIONS = [None, "approve", "reject"]


def _entry(rnd: random.Random) -> dict[str, Any]:
    entry: dict[str, Any] = {}
    while not entry.get("operationId") and not entry.get("path"):
        entry = {}
        if rnd.random() < 0.6:
            entry["operationId"] = rnd.choice(_IDS[1:])
        if rnd.random() < 0.6:
            entry["path"] = rnd.choice([p for p in _PATHS[1:] if p != "/orders/7/cancel/"])
    if rnd.random() < 0.5:
        entry["methods"] = rnd.sample(_METHODS, rnd.randint(1, 3))
    return entry


def _call(rnd: random.Random) -> tuple[str, str | None, str | None, str | None, str | None]:
    method = rnd.choice(_METHODS)
    return (
        method,
        rnd.choice(_IDS),
        rnd.choice(_PATHS),
        rnd.choice(_RPC),
        rnd.choice(_OPERATIONS),
    )


def _api_v02(rnd: random.Random) -> dict[str, Any]:
    """A random valid 0.2 API: methods, allow and deny lists, zero to three approval rules."""
    api: dict[str, Any] = {
        "base_url_env": "A",
        "auth": "none",
        "allowed_methods": ["*"] if rnd.random() < 0.2 else rnd.sample(_METHODS, rnd.randint(1, 5)),
    }
    if rnd.random() < 0.7:
        api["allowed_operations"] = [_entry(rnd) for _ in range(rnd.randint(1, 4))]
    if rnd.random() < 0.6:
        api["denied_operations"] = [_entry(rnd) for _ in range(rnd.randint(0, 3))]
    rules = []
    for _ in range(rnd.randint(0, 3)):
        required_for: dict[str, Any] = {}
        if rnd.random() < 0.5:
            required_for["methods"] = rnd.sample([*_METHODS, "*"], rnd.randint(1, 2))
            if "*" in required_for["methods"]:
                required_for["methods"] = ["*"]
        if rnd.random() < 0.7 or not required_for:
            required_for["operations"] = [_entry(rnd) for _ in range(rnd.randint(1, 3))]
        rules.append(
            {
                "required_for": required_for,
                "approvers": rnd.choice([["requester"], ["role:ops"], ["requester", "role:x"]]),
                "timeout_s": rnd.choice([60, 900]),
            }
        )
    if rules:
        api["approval"] = rules[0] if len(rules) == 1 and rnd.random() < 0.5 else rules
    return api


def _gate_v02(module: ModuleType, api: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
    """The gate as 0.2 saw it (approvers, timeout, rule, index, also), or the conflict."""
    try:
        gate = module.gated(api, *args, **kwargs)
    except module.ApprovalRuleConflict as conflict:
        return ("conflict", str(conflict), conflict.unnamed, conflict.index, conflict.later)
    if gate is None:
        return None
    return (gate.approvers, gate.timeout_s, gate.rule, gate.index, gate.also)


@pytest.mark.parametrize("seed", range(8))
def test_an_entry_without_the_new_fields_matches_exactly_as_in_0_2(
    rules: ModuleType, v02: ModuleType, seed: int
) -> None:
    """Whatever JSON-RPC method and decision a call carries, a 0.2 entry and a 0.2 API
    allow, deny and gate it as 0.2.0 did (the derived values only reach the new fields)."""
    rnd = random.Random(seed)
    for _ in range(400):
        api = _api_v02(rnd)
        assert (
            rules.policy_errors({"apis": {"a": api}})
            == v02.policy_errors({"apis": {"a": api}})
            == []
        )
        entry = _entry(rnd)
        for _ in range(10):
            method, operation_id, path, rpc_method, operation = _call(rnd)
            rpc = {"rpc_method": rpc_method, "a2a_operation": operation}
            assert rules.operation_matches(
                entry, method, operation_id, path, **rpc
            ) == v02.operation_matches(entry, method, operation_id, path)
            assert rules.denial_match(entry, method, operation_id, path, **rpc) == (
                v02.denial_match(entry, method, operation_id, path)
            )
            assert rules.refusal_reason(api, method, operation_id, path, **rpc) == (
                v02.refusal_reason(api, method, operation_id, path)
            )
            template = rnd.choice([None, "/orders/{order_id}/cancel", "/orders/{x}"])
            assert _gate_v02(rules, api, method, operation_id, path, template=template, **rpc) == (
                _gate_v02(v02, api, method, operation_id, path, template=template)
            )
            try:
                gate = rules.gated(api, method, operation_id, path, template=template, **rpc)
            except rules.ApprovalRuleConflict:
                gate = None
            # And 0.2 gates are decided directly, as in 0.2.
            assert gate is None or (gate.decide_with, gate.relayers) == ("direct", ())


def test_the_property_grid_reaches_every_outcome(v02: ModuleType) -> None:
    """The random grid is not vacuous: it allows, refuses, denies, gates and conflicts."""
    rnd = random.Random(0)
    seen: set[str] = set()
    for _ in range(400):
        api = _api_v02(rnd)
        for _ in range(10):
            method, operation_id, path, _, _ = _call(rnd)
            reason = v02.refusal_reason(api, method, operation_id, path)
            seen.add("allowed" if reason is None else reason.split(" ", 1)[0])
            gate = _gate_v02(v02, api, method, operation_id, path)
            seen.add("gated" if gate and gate[0] != "conflict" else str(gate and gate[0]))
    assert {"allowed", "method", "denied", "not", "gated", "conflict", "None"} <= seen


# --- deriving what a request is fails closed (property) ------------------------------------

_DECISIONS = ["approve", "reject", "REJECT", " reject", "", None, 0, ["reject"], {"x": 1}]
_JUNK = [None, 0, 1.5, True, "x", [], {}, [1], {"a": "b"}]


def _junk(rnd: random.Random, depth: int = 0) -> Any:
    roll = rnd.random()
    if depth > 2 or roll < 0.4:
        return rnd.choice(_JUNK)
    if roll < 0.7:
        return [_junk(rnd, depth + 1) for _ in range(rnd.randint(0, 3))]
    return {
        rnd.choice(["data", "text", "kind", "decision", "approval_id", "x"]): _junk(rnd, depth + 1)
    }


def _part(rnd: random.Random) -> Any:
    roll = rnd.random()
    if roll < 0.25:
        return {"text": "hello"}
    if roll < 0.55:
        data: dict[str, Any] = {}
        if rnd.random() < 0.8:
            data["approval_id"] = "a1"
        if rnd.random() < 0.8:
            data["decision"] = rnd.choice(_DECISIONS)
        if rnd.random() < 0.3:
            data["comment"] = "c"
        part: dict[str, Any] = {"data": data}
        if rnd.random() < 0.3:
            part["kind"] = "data"
        return part
    if roll < 0.7:
        return {"data": _junk(rnd)}
    return _junk(rnd)


def _body(rnd: random.Random) -> Any:
    parts: Any = [_part(rnd) for _ in range(rnd.randint(0, 4))]
    if rnd.random() < 0.1:
        parts = tuple(parts)
    message: Any = {"role": "ROLE_USER", "parts": parts}
    if rnd.random() < 0.05:
        message = _junk(rnd)
    params: Any = {"message": message}
    if rnd.random() < 0.05:
        params = _junk(rnd)
    body: Any = {
        "jsonrpc": "2.0",
        "id": rnd.choice(["1", 2, None, True]) if rnd.random() < 0.2 else "1",
        "method": rnd.choice(
            ["SendMessage", "message/send", "message/stream", "GetTask", "x", "sendMESSAGE"]
        ),
        "params": params,
    }
    if rnd.random() < 0.05:
        body["extra"] = 1
    if rnd.random() < 0.05:
        del body["id"]
    if rnd.random() < 0.05:
        body = [body]
    return body


def _oracle(body: Any) -> str | None:
    """What the called agent reads: the decisions in the message's data parts, on the wire."""
    wire = json.loads(json.dumps(body))
    parts = wire["params"]["message"].get("parts", [])
    decisions = [
        p["data"].get("decision")
        for p in parts
        if isinstance(p, dict)
        and isinstance(p.get("data"), dict)
        and ("approval_id" in p["data"] or "decision" in p["data"])
    ]
    if not decisions:
        return None
    return "reject" if all(d == "reject" for d in decisions) else "approve"


@pytest.mark.parametrize("seed", range(4))
def test_deriving_what_a_request_is_fails_closed(rules: ModuleType, seed: int) -> None:
    """Any body: a refusal (RpcRequestError, never another error), or exactly what the called
    agent would read; a message naming an approval is never read as anything but `approve`,
    or `reject` when every part naming one says exactly reject."""
    rnd = random.Random(seed)
    outcomes: set[Any] = set()
    for _ in range(3000):
        body = _body(rnd)
        try:
            rpc = rules.derive_rpc(A2A, "POST", body)
        except rules.RpcRequestError:
            outcomes.add("refused")
            continue
        outcomes.add(rpc.a2a_operation)
        method = rules.canonical_rpc_method("a2a", body["method"])
        assert rpc.rpc_method == method
        messages = {"sendmessage", "sendstreamingmessage", "message/send", "message/stream"}
        if method.casefold() in messages:
            assert rpc.a2a_operation == _oracle(body)
        else:
            assert rpc.a2a_operation is None
    assert outcomes == {"refused", None, "approve", "reject"}


# --- limits.max_response_bytes -----------------------------------------------------------------


@pytest.mark.parametrize("size", [1, 1048576, 67108864])
def test_an_answer_cap_within_64_mib_is_valid(size: int, runtime: ModuleType) -> None:
    api = {"base_url_env": "A", "auth": "none", "allowed_methods": ["GET"]}
    assert _errors(_doc(a={**api, "limits": {"max_response_bytes": size}}), runtime) == []
    peer = _peer(limits={"max_calls_per_run": 12, "max_response_bytes": size})
    assert _errors(_doc(orders_agent=peer), runtime) == []


@pytest.mark.parametrize(
    ("size", "message"),
    [
        (0, "apis.a.limits.max_response_bytes: must be an integer >= 1"),
        ("1024", "apis.a.limits.max_response_bytes: must be an integer >= 1"),
        (True, "apis.a.limits.max_response_bytes: must be an integer >= 1"),
        (
            67108865,
            "apis.a.limits.max_response_bytes: must be an integer from 1 to 67108864 (bytes; "
            "64 MiB at most)",
        ),
    ],
)
def test_a_bad_answer_cap_is_refused(size: Any, message: str, runtime: ModuleType) -> None:
    api = {"base_url_env": "A", "auth": "none", "allowed_methods": ["GET"]}
    assert _errors(_doc(a={**api, "limits": {"max_response_bytes": size}}), runtime) == [message]


def test_the_limits_message_names_every_key(runtime: ModuleType) -> None:
    api = {"base_url_env": "A", "auth": "none", "allowed_methods": ["GET"], "limits": {}}
    assert _errors(_doc(a=api), runtime) == [
        "apis.a.limits: must be a mapping with max_calls_per_run, rate_per_minute and/or "
        "max_response_bytes"
    ]
