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

"""`graph-agents-cli api` on JSON-RPC APIs and A2A peers (`protocol`, `rpc_method`,
`a2a_operation`): add, allow, deny, revoke, approval and show.

A project is created once per module with the real bundled template (`--skip-deps`,
nothing installed, no network) and copied for each test.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from graph_agents_cli._api_policy import load_policy_document
from graph_agents_cli.main import main

ENV = {
    "GRAPH_AGENTS_CLI_NO_UPDATE_CHECK": "1",
    "GRAPH_AGENTS_CLI_DISABLE_OVERRIDES": "1",
    "COLUMNS": "250",
}

PEER_ADD = [
    *("api", "add", "orders_agent", "--protocol", "a2a", "--a2a-path", "/a2a/orders"),
    *("--description", "Orders agent: reads the caller's orders."),
    *("--base-url-env", "ORDERS_AGENT_URL", "--auth", "bearer", "--token-env", "ORDERS_KEY"),
    *("--access", "custom", "--methods", "GET,POST"),
]
PEER_TOOL = """\
API_CALLS = [
    {"api": "orders_agent", "method": "POST", "rpc_method": "SendMessage", "path": "/a2a/orders"},
    {"api": "orders_agent", "method": "POST", "rpc_method": "SendMessage",
     "a2a_operation": "approve", "path": "/a2a/orders"},
    {"api": "orders_agent", "method": "POST", "rpc_method": "GetTask", "path": "/a2a/orders"},
]
TOOLS: list = []
"""


@pytest.fixture(scope="module")
def template_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("api-rpc-projects")
    result = CliRunner().invoke(
        main, ["create", "shop", "-y", "--skip-checks", "--skip-deps", "-o", str(out)], env=ENV
    )
    assert result.exit_code == 0, result.output
    return out / "shop"


@pytest.fixture
def project(template_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "shop"
    shutil.copytree(template_project, target)
    monkeypatch.chdir(target)
    return target


def cli(*args: str) -> Result:
    return CliRunner().invoke(main, list(args), env=ENV)


def ok(*args: str) -> Result:
    result = cli(*args)
    assert result.exit_code == 0, result.output
    return result


def peer(project: Path) -> dict:
    return load_policy_document(project / "api-policy.yaml")["apis"]["orders_agent"]


def text(project: Path) -> str:
    return (project / "api-policy.yaml").read_text()


def test_add_an_a2a_peer_denies_its_approvals_until_they_are_gated(project: Path) -> None:
    result = ok(*PEER_ADD)
    api = peer(project)
    assert api["description"] == "Orders agent: reads the caller's orders."
    assert (api["protocol"], api["a2a"]) == ("a2a", {"path": "/a2a/orders"})
    assert api["denied_operations"] == [{"a2a_operation": "approve"}]
    # The file reads in the schema's order.
    written = text(project)
    order = ["description:", "protocol:", "a2a:", "base_url_env:", "allowed_methods:", "denied_"]
    assert [written.index(k) for k in order] == sorted(written.index(k) for k in order)
    flat = " ".join(result.output.split())
    assert "are denied (denied_operations: a2a_operation: approve)" in flat
    assert (
        "graph-agents-cli api approval orders_agent --a2a-operations approve --approvers "
        "requester; graph-agents-cli api revoke orders_agent --a2a-operation approve --from "
        "denied" in flat
    )
    assert 'declare each POST to orders_agent in API_CALLS with its "rpc_method"' in flat


def test_add_a_read_only_peer_needs_no_denial(project: Path) -> None:
    args = [a if a != "custom" else "read-only" for a in PEER_ADD]
    args = args[: args.index("--methods")]
    ok(*args)
    assert "denied_operations" not in peer(project)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (["--a2a-path", None], "--protocol a2a needs --a2a-path"),
        (["--protocol", "jsonrpc"], "--a2a-path goes with --protocol a2a only"),
        (["--auth", "none", "--token-env", None], "--protocol a2a needs a credential"),
    ],
)
def test_add_refuses_an_a2a_peer_that_cannot_work(
    project: Path, change: list, message: str
) -> None:
    args = list(PEER_ADD)
    for flag, value in zip(change[::2], change[1::2], strict=True):
        index = args.index(flag)
        if value is None:
            del args[index : index + 2]
        else:
            args[index + 1] = value
    result = cli(*args)
    assert result.exit_code == 2 and message in result.output
    assert not (project / "api-policy.yaml").exists()


def test_add_a_json_rpc_api_with_methods_it_cannot_have_writes_nothing(project: Path) -> None:
    result = cli(
        *("api", "add", "ledger", "--protocol", "jsonrpc", "--base-url-env", "LEDGER_URL"),
        *("--auth", "none", "--access", "read-write"),
    )
    assert result.exit_code == 3
    assert "protocol jsonrpc allows GET, POST and HEAD only" in " ".join(result.output.split())
    assert not (project / "api-policy.yaml").exists()


def test_gate_then_lift_the_denial_then_allow_and_deny_by_rpc_method(project: Path) -> None:
    ok(*PEER_ADD)
    # Lifting the denial first would leave approve neither gated nor denied: refused.
    early = cli("api", "revoke", "orders_agent", "--a2a-operation", "approve", "--from", "denied")
    assert early.exit_code == 3
    assert "could decide approvals at orders" in " ".join(early.output.split())
    gate = ok(
        *("api", "approval", "orders_agent", "--a2a-operations", "approve"),
        *("--approvers", "requester"),
    )
    assert peer(project)["approval"] == {
        "required_for": {"operations": [{"a2a_operation": "approve"}]},
        "approvers": ["requester"],
    }
    assert "tightens" in gate.output
    lifted = ok("api", "revoke", "orders_agent", "--a2a-operation", "approve", "--from", "denied")
    assert "denied_operations" not in peer(project)
    assert "widens access" in " ".join(lifted.output.split())
    allow = ok(
        *("api", "allow", "orders_agent", "--rpc-method", "GetTask"),
        *("--method", "POST", "--path", "/a2a/orders"),
    )
    assert peer(project)["allowed_operations"] == [
        {"path": "/a2a/orders", "methods": ["POST"], "rpc_method": "GetTask"}
    ]
    assert "This creates the list" in " ".join(allow.output.split())
    ok("api", "deny", "orders_agent", "--rpc-method", "CancelTask")
    assert peer(project)["denied_operations"] == [{"rpc_method": "CancelTask"}]
    shown = ok("api", "deny", "orders_agent", "--rpc-method", "CancelTask")
    assert "already denies" in shown.output
    ok("api", "revoke", "orders_agent", "--rpc-method", "CancelTask")
    assert "denied_operations" not in peer(project)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (
            ["allow", "orders_agent", "--rpc-method", "GetTask"],
            "give both --method POST and --path",
        ),
        (
            ["allow", "orders_agent", "--rpc-method", "GetTask", "--method", "GET", "--path", "/x"],
            "a JSON-RPC request is a POST",
        ),
        (
            ["allow", "orders_agent", "--rpc-method", "9x", "--method", "POST", "--path", "/x"],
            "a JSON-RPC method name is a letter",
        ),
        (["deny", "orders_agent", "--a2a-operation", "maybe"], "is not one of 'approve', 'reject'"),
        (["deny", "orders_agent", "--rpc-method", "X", "--method", "POST"], "give both --method"),
        (
            ["revoke", "orders_agent", "--rpc-method", "X", "--method", "POST", "--path", "/x"],
            "give no --method or --path with them",
        ),
        (["approval", "orders_agent", "--a2a-operations", "maybe"], "is not approve or reject"),
    ],
)
def test_bad_json_rpc_options_are_usage_errors(project: Path, args: list, message: str) -> None:
    ok(*PEER_ADD)
    before = text(project)
    result = cli("api", *args)
    assert result.exit_code == 2 and message in result.output
    assert text(project) == before


def test_an_a2a_entry_on_an_http_api_is_refused(project: Path) -> None:
    ok(
        *("api", "add", "orders", "--base-url-env", "ORDERS_URL", "--auth", "none"),
        *("--access", "read-write"),
    )
    result = cli("api", "deny", "orders", "--a2a-operation", "approve")
    assert result.exit_code == 3
    assert "a2a_operation: only valid with protocol a2a" in result.output


def test_approval_keeps_the_other_kind_of_entry(project: Path) -> None:
    ok(*PEER_ADD)
    ok(
        *("api", "approval", "orders_agent", "--a2a-operations", "approve"),
        *("--approvers", "requester"),
    )
    ok("api", "approval", "orders_agent", "--operations", "askOrders")
    assert peer(project)["approval"]["required_for"]["operations"] == [
        {"operationId": "askOrders"},
        {"a2a_operation": "approve"},
    ]
    ok("api", "approval", "orders_agent", "--operations", "none")
    assert peer(project)["approval"]["required_for"]["operations"] == [{"a2a_operation": "approve"}]
    # With the denial still there, the gate may go: approve stays held.
    ok("api", "approval", "orders_agent", "--a2a-operations", "none", "--methods", "POST")
    assert peer(project)["approval"]["required_for"] == {"methods": ["POST"]}


def test_show_and_check_name_the_protocol_and_each_json_rpc_call(project: Path) -> None:
    ok(*PEER_ADD)
    ok(
        *("api", "approval", "orders_agent", "--a2a-operations", "approve"),
        *("--approvers", "requester"),
    )
    (project / "app" / "tools" / "peers.py").write_text(PEER_TOOL)
    # While the denial written by add stands, the approve is refused (denials win).
    refused = cli("api", "check")
    assert refused.exit_code == 1
    assert "denied by denied_operations (a2a_operation=approve)" in refused.output
    ok("api", "revoke", "orders_agent", "--a2a-operation", "approve", "--from", "denied")
    shown = ok("api", "show", "orders_agent")
    flat = " ".join(shown.output.split())
    assert "description │ Orders agent: reads the caller's orders." in flat
    assert "a2a (another agent, endpoint /a2a/orders)" in flat
    assert "a2a_operation=approve" in flat
    assert "/a2a/orders (SendMessage approve)" in flat
    assert "1 declared call(s) wait for a human approval" in flat
    data = json.loads(ok("api", "show", "orders_agent", "--json").output)
    effective = data["apis"]["orders_agent"]
    assert (effective["protocol"], effective["a2a"]) == ("a2a", {"path": "/a2a/orders"})
    assert effective["description"] == "Orders agent: reads the caller's orders."
    calls = {(c["rpc_method"], c["a2a_operation"]): c for c in data["calls"]}
    assert calls[("SendMessage", "approve")]["approval"]["approvers"] == ["requester"]
    assert calls[("SendMessage", None)]["approval"] is None
    assert data["violations"] == 0
    assert ok("api", "check").exit_code == 0


# ---------------------------------------------------------------------------
# limits.max_response_bytes
# ---------------------------------------------------------------------------


def test_the_answer_cap_is_set_kept_and_cleared(project: Path) -> None:
    ok(*PEER_ADD, "--max-calls-per-run", "12", "--max-response-bytes", "1048576")
    assert peer(project)["limits"] == {"max_calls_per_run": 12, "max_response_bytes": 1048576}
    # Another limit's edit keeps the cap (the keys are written in the schema's order).
    ok("api", "limits", "orders_agent", "--rate-per-minute", "60")
    assert peer(project)["limits"] == {
        "max_calls_per_run": 12,
        "rate_per_minute": 60,
        "max_response_bytes": 1048576,
    }
    raised = ok("api", "limits", "orders_agent", "--max-response-bytes", "2097152")
    assert "This widens access to orders_agent" in " ".join(raised.output.split())
    lowered = ok("api", "limits", "orders_agent", "--max-response-bytes", "1024")
    assert "narrows or keeps access" in lowered.output
    shown = ok("api", "show", "orders_agent")
    assert "answers of at most 1024 bytes" in " ".join(shown.output.split())
    ok("api", "limits", "orders_agent", "--max-response-bytes", "none")
    assert peer(project)["limits"] == {"max_calls_per_run": 12, "rate_per_minute": 60}


def test_an_answer_cap_over_64_mib_is_refused(project: Path) -> None:
    result = cli(*PEER_ADD, "--max-response-bytes", "67108865")
    assert result.exit_code == 2 and "67108865 is not in the range 1<=x<=67108864" in result.output
    ok(*PEER_ADD)
    before = text(project)
    result = cli("api", "limits", "orders_agent", "--max-response-bytes", "67108865")
    assert result.exit_code == 3
    assert "max_response_bytes: must be an integer from 1 to 67108864" in result.output
    assert text(project) == before
