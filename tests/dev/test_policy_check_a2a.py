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

"""`lint` on JSON-RPC APIs and A2A peers: `rpc_method` and `a2a_operation` in `API_CALLS`.

The runtime client reads what a request to a `protocol: jsonrpc|a2a` API is from the body it
sends; lint cannot see bodies, so each POST declares its `rpc_method` (and an A2A message
that decides, its `a2a_operation`), and lint judges the declaration with the same rules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from graph_agents_cli.dev import policy_check as pc

PATH = "/a2a/orders"


def _peer(**overrides: Any) -> dict[str, Any]:
    api: dict[str, Any] = {
        "description": "Orders agent.",
        "protocol": "a2a",
        "a2a": {"path": PATH},
        "base_url_env": "ORDERS_AGENT_URL",
        "auth": "bearer",
        "token_env": "ORDERS_KEY",
        "allowed_methods": ["GET", "POST"],
        "allowed_operations": [
            {"rpc_method": "SendMessage", "methods": ["POST"], "path": PATH},
            {"rpc_method": "GetTask", "methods": ["POST"], "path": PATH},
            {"operationId": "getTask", "rpc_method": "GetTask"},
        ],
        "approval": {
            "required_for": {"operations": [{"a2a_operation": "approve"}]},
            "approvers": ["requester"],
        },
    }
    api.update(overrides)
    return {k: v for k, v in api.items() if v is not None}


PLAIN = {"base_url_env": "PLAIN_URL", "auth": "none", "allowed_methods": ["POST"]}


def _call(**entry: Any) -> pc.DeclaredCall:
    return pc.DeclaredCall(
        tool="peers.py",
        api=entry.pop("api", "orders_agent"),
        method=entry.pop("method", "POST"),
        **entry,
    )


def _check(call: pc.DeclaredCall, **apis: dict[str, Any]) -> pc.CheckResult:
    return pc.check_call(call, {"apis": apis or {"orders_agent": _peer(), "plain": PLAIN}})


def test_each_json_rpc_call_is_judged_by_what_it_declares() -> None:
    ask = _check(_call(rpc_method="SendMessage", path=PATH))
    assert ask.status == pc.STATUS_ALLOWED and ask.gate is None
    approve = _check(_call(rpc_method="SendMessage", a2a_operation="approve", path=PATH))
    assert approve.status == pc.STATUS_ALLOWED
    assert approve.gate is not None and approve.gate.approvers == ("requester",)
    reject = _check(_call(rpc_method="message/send", a2a_operation="reject", path=PATH))
    assert reject.status == pc.STATUS_ALLOWED and reject.gate is None
    # 0.3 names are read as their 1.0 names, as the client reads them.
    assert _check(_call(rpc_method="tasks/get", path=PATH)).status == pc.STATUS_ALLOWED
    cancel = _check(_call(rpc_method="tasks/cancel", path=PATH))
    assert (cancel.status, cancel.reason) == (pc.STATUS_DENIED, "not in allowed_operations")
    assert cancel.hint == (
        "graph-agents-cli api allow orders_agent --method POST --path /a2a/orders "
        "--rpc-method tasks/cancel"
    )
    # A GET is judged as before: no JSON-RPC method.
    assert _check(_call(method="GET", path=f"{PATH}/card")).status == pc.STATUS_DENIED


@pytest.mark.parametrize(
    ("call", "reason", "hint"),
    [
        (
            _call(path=PATH),
            "a POST to a protocol a2a API sends a JSON-RPC request: declare its rpc_method",
            'add "rpc_method": "<the JSON-RPC method>" in the call\'s API_CALLS entry',
        ),
        (
            _call(api="plain", rpc_method="SendMessage", path="/x"),
            "rpc_method and a2a_operation are for JSON-RPC APIs; plain is protocol http",
            "remove rpc_method and a2a_operation in the call's API_CALLS entry",
        ),
        (
            _call(rpc_method="GetTask", a2a_operation="approve", path=PATH),
            "a2a_operation is for an A2A message (protocol a2a, rpc_method SendMessage or "
            "SendStreamingMessage)",
            "remove a2a_operation",
        ),
        (
            _call(operation_id="getTask", rpc_method="SendMessage", path=PATH),
            "operation_id 'getTask' does not match the request (rpc_method SendMessage); refused",
            "name the operation_id of the entry for this request",
        ),
    ],
)
def test_a_declaration_that_does_not_fit_the_api_is_refused(
    call: pc.DeclaredCall, reason: str, hint: str
) -> None:
    result = _check(call)
    assert result.status == pc.STATUS_DENIED
    assert result.reason.startswith(reason)
    assert hint in result.hint


def test_a_denied_or_unheld_approve_is_refused() -> None:
    denied = _peer(approval=None, denied_operations=[{"a2a_operation": "approve"}])
    result = _check(
        _call(rpc_method="SendMessage", a2a_operation="approve", path=PATH), orders_agent=denied
    )
    assert result.status == pc.STATUS_DENIED
    assert result.reason == "denied by denied_operations (a2a_operation=approve)"
    assert result.hint.startswith(
        "graph-agents-cli api revoke orders_agent --a2a-operation approve --from denied"
    )
    # A policy that was never validated: lint refuses as the client does.
    unheld = _peer(approval=None)
    result = _check(
        _call(rpc_method="SendMessage", a2a_operation="approve", path=PATH), orders_agent=unheld
    )
    assert result.status == pc.STATUS_DENIED
    assert result.reason == "a message that approves must wait for an approval or be denied"


@pytest.mark.parametrize(
    ("entry", "problem"),
    [
        (
            {"rpc_method": "9x", "path": PATH},
            "has an rpc_method that is not a JSON-RPC method name",
        ),
        ({"method": "GET", "rpc_method": "GetTask", "path": PATH}, "has an rpc_method on a GET"),
        (
            {"rpc_method": "SendMessage", "a2a_operation": "maybe", "path": PATH},
            "has an a2a_operation other than approve or reject",
        ),
        ({"a2a_operation": "approve", "path": PATH}, "has an a2a_operation without rpc_method"),
        ({"rpc": "x", "path": PATH}, "has unknown key(s) 'rpc'"),
    ],
)
def test_malformed_json_rpc_declarations(tmp_path: Path, entry: dict, problem: str) -> None:
    declared = {"api": "orders_agent", "method": "POST", **entry}
    (tmp_path / "peers.py").write_text(f"API_CALLS = [{declared!r}]\n")
    calls, problems = pc.read_api_calls(tmp_path / "peers.py")
    assert calls == []
    assert len(problems) == 1 and problem in problems[0]


def test_declarations_are_read_with_their_json_rpc_keys(tmp_path: Path) -> None:
    (tmp_path / "peers.py").write_text(
        "API_CALLS = [\n"
        '    {"api": "orders_agent", "method": "post", "rpc_method": "SendMessage",\n'
        '     "a2a_operation": "approve", "path": "/a2a/orders"},\n'
        "]\n"
    )
    [call], problems = pc.read_api_calls(tmp_path / "peers.py")
    assert problems == []
    assert (call.method, call.rpc_method, call.a2a_operation) == ("POST", "SendMessage", "approve")
    assert call.operation == "/a2a/orders (SendMessage approve)"


def _project(tmp_path: Path, **apis: dict[str, Any]) -> Path:
    (tmp_path / "app" / "tools").mkdir(parents=True)
    (tmp_path / "api-policy.yaml").write_text(yaml.safe_dump({"apis": apis}))
    return tmp_path


def test_lint_warns_about_peers_the_model_cannot_choose_well(tmp_path: Path) -> None:
    apis = {"orders_agent": _peer(description=None), "plain": PLAIN}
    report = pc.build_report(_project(tmp_path, **apis), "app")
    assert (
        "warning: A2A peer orders_agent has no description: the model chooses an agent by what "
        "it does (set apis.orders_agent.description, 1-300 characters)"
    ) in report.notes
    assert not report.policy_invalid


def test_lint_warns_above_forty_peers(tmp_path: Path) -> None:
    peers = {f"peer_{i}": _peer(a2a={"path": f"/a2a/p{i}"}) for i in range(41)}
    report = pc.build_report(_project(tmp_path, **peers), "app")
    assert any(note.startswith("warning: 41 A2A peers (more than 40)") for note in report.notes)
    forty = {f"peer_{i}": _peer(a2a={"path": f"/a2a/p{i}"}) for i in range(40)}
    report = pc.build_report(_project(tmp_path / "forty", **forty), "app")
    assert not any("A2A peers (more than" in note for note in report.notes)


def test_the_example_tool_never_declares_an_unlabelled_json_rpc_post() -> None:
    """`create` renders the example tool from the first API: a POST it cannot label with an
    rpc_method is skipped, so the example passes lint."""
    document = {"apis": {"orders_agent": _peer(allowed_methods=["POST"], allowed_operations=None)}}
    assert pc.example_call(document) is None
    with_card = {
        "apis": {
            "orders_agent": _peer(
                allowed_operations=[
                    {"operationId": "getAgentCard", "methods": ["GET"], "path": f"{PATH}/card"},
                    *_peer()["allowed_operations"],
                ]
            )
        }
    }
    example = pc.example_call(with_card)
    assert example is not None and (example.method, example.path) == ("GET", f"{PATH}/card")
