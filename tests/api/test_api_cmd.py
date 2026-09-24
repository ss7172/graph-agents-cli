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

"""`graph-agents-cli api`: the project's outbound API policy over its life.

A project is created once per module with the real bundled template
(`--skip-deps`, nothing installed, no network) and copied for each test.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner, Result

from graph_agents_cli._api_policy import load_policy_document
from graph_agents_cli.main import main

ENV = {
    "GRAPH_AGENTS_CLI_NO_UPDATE_CHECK": "1",
    "GRAPH_AGENTS_CLI_DISABLE_OVERRIDES": "1",
    "COLUMNS": "250",  # rich tables unwrapped
}

ORDERS_ADD = [
    "api",
    "add",
    "orders",
    "--base-url-env",
    "ORDERS_API_BASE_URL",
    "--auth",
    "bearer",
    "--token-env",
    "ORDERS_API_TOKEN",
    "--access",
    "read-write",
    "--max-calls-per-run",
    "20",
    "--rate-per-minute",
    "120",
]

SPEC = """\
openapi: 3.0.0
info: {title: Orders, version: "1"}
paths:
  /orders:
    get: {operationId: listOrders, responses: {"200": {description: ok}}}
    post: {operationId: createOrder, responses: {"201": {description: created}}}
  /orders/{order_id}:
    get: {operationId: getOrder, responses: {"200": {description: ok}}}
    patch: {operationId: updateOrder, responses: {"200": {description: ok}}}
    delete: {operationId: deleteOrder, responses: {"204": {description: gone}}}
"""

GET_TOOL = """\
API_CALLS = [
    {"api": "orders", "method": "GET", "operation_id": "listOrders", "path": "/orders"},
]
TOOLS: list = []
"""

POST_TOOL = """\
API_CALLS = [
    {"api": "orders", "method": "POST", "operation_id": "createOrder", "path": "/orders"},
]
TOOLS: list = []
"""


@pytest.fixture(scope="module")
def template_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("api-projects")
    result = CliRunner().invoke(
        main,
        [
            *("create", "shop", "-y", "--skip-checks", "--skip-deps"),
            *("-d", "kubernetes", "--registry", "ghcr.io/acme", "-o", str(out)),
        ],
        env=ENV,
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


def policy(project: Path) -> dict:
    return load_policy_document(project / "api-policy.yaml")["apis"]


def manifest(project: Path) -> dict:
    return yaml.safe_load((project / "graph-agents-cli-manifest.yaml").read_text())


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


def test_add_creates_the_policy_and_keeps_the_project_in_step(project: Path) -> None:
    before = (project / "graph-agents-cli-manifest.yaml").read_text()
    dry = ok(*ORDERS_ADD, "--dry-run")
    assert "+++ b/api-policy.yaml" in dry.output
    assert "Dry run: nothing was written." in dry.output
    assert not (project / "api-policy.yaml").exists()
    assert (project / "graph-agents-cli-manifest.yaml").read_text() == before

    result = ok(*ORDERS_ADD)
    assert "This widens access to orders" in result.output
    orders = policy(project)["orders"]
    assert orders["allowed_methods"] == ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]
    assert orders["limits"] == {"max_calls_per_run": 20, "rate_per_minute": 120}
    assert "allowed_operations" not in orders  # every operation, until `allow` narrows it
    data = manifest(project)
    assert data["api_policy"] == {"policy_file": "api-policy.yaml"}
    assert data["secrets"]["keys"][-1] == "ORDERS_API_TOKEN"
    # The manifest's own comments survive.
    assert "# Team or role responsible" in (project / "graph-agents-cli-manifest.yaml").read_text()
    env_example = (project / ".env.example").read_text()
    assert "# orders (auth: bearer)\nORDERS_API_BASE_URL=http://localhost:9000\n" in env_example
    assert "ORDERS_API_TOKEN=\n" in env_example
    assert "No api-policy.yaml is declared" not in env_example
    values = yaml.safe_load((project / "deployment/helm/shop/values.yaml").read_text())
    assert values["env"]["ORDERS_API_BASE_URL"] == "http://CHANGE-ME"
    assert "Left for you" in result.output and "secrets apply" in result.output


def test_add_requires_an_explicit_access_choice(project: Path) -> None:
    without = [a for a in ORDERS_ADD if a not in ("--access", "read-write")]
    result = cli(*without)
    assert result.exit_code == 2
    assert "--access" in result.output
    custom = cli(*[a if a != "read-write" else "custom" for a in ORDERS_ADD])
    assert custom.exit_code == 2 and "--methods" in custom.output
    ok(*[a if a != "read-write" else "custom" for a in ORDERS_ADD], "--methods", "post, get")
    assert policy(project)["orders"]["allowed_methods"] == ["GET", "POST"]


def test_add_read_only_writes_the_methods_not_a_preset_name(project: Path) -> None:
    ok(*[a if a != "read-write" else "read-only" for a in ORDERS_ADD])
    text = (project / "api-policy.yaml").read_text()
    assert "allowed_methods: [GET, HEAD]" in text
    assert "read-only" not in text.split("apis:", 1)[1]


@pytest.mark.parametrize(
    ("args", "code", "fragment"),
    [
        (["--auth", "bearer"], 2, "--token-env"),
        (["--auth", "none", "--token-env", "X"], 2, "--auth bearer only"),
        (["--base-url-env", "1BAD"], 3, "must be an environment variable name"),
    ],
)
def test_add_refuses_bad_input(project: Path, args: list[str], code: int, fragment: str) -> None:
    base = [
        *("api", "add", "crm", "--base-url-env", "CRM_API_BASE_URL"),
        *("--auth", "none", "--access", "read-only"),
    ]
    merged = list(base)
    for flag, value in zip(args[::2], args[1::2], strict=True):
        if flag in merged:
            merged[merged.index(flag) + 1] = value
        else:
            merged += [flag, value]
    result = cli(*merged)
    assert result.exit_code == code, result.output
    assert fragment in result.output
    assert not (project / "api-policy.yaml").exists()


def test_add_refuses_forward_under_langgraph_server_and_a_second_declaration(
    project: Path,
) -> None:
    ok(*ORDERS_ADD)
    again = cli(*ORDERS_ADD)
    assert again.exit_code == 3 and "already declared" in again.output
    manifest_path = project / "graph-agents-cli-manifest.yaml"
    manifest_path.write_text(
        manifest_path.read_text().replace("runtime: 'fastapi'", "runtime: 'langgraph-server'")
    )
    forward = cli(
        *("api", "add", "people", "--base-url-env", "PEOPLE_URL"),
        *("--auth", "forward", "--access", "read-only"),
    )
    assert forward.exit_code == 3
    assert "not supported with runtime langgraph-server" in forward.output


def test_add_copies_an_outside_spec_into_the_project(project: Path, tmp_path: Path) -> None:
    spec = tmp_path / "orders-openapi.yaml"
    spec.write_text(SPEC)
    ok(*ORDERS_ADD, "--openapi", str(spec))
    assert policy(project)["orders"]["openapi"] == "openapi/orders/orders-openapi.yaml"
    assert (project / "openapi/orders/orders-openapi.yaml").read_text() == SPEC


def test_new_files_are_readable_by_the_image_user_and_existing_ones_keep_their_mode(
    project: Path, tmp_path: Path
) -> None:
    """The images copy the policy as it is and run as uid 1000: never write it 0600."""
    spec = tmp_path / "orders-openapi.yaml"
    spec.write_text(SPEC)
    previous = os.umask(0o022)
    try:
        ok(*ORDERS_ADD, "--openapi", str(spec))
    finally:
        os.umask(previous)
    for created in ("api-policy.yaml", "openapi/orders/orders-openapi.yaml"):
        assert stat.S_IMODE((project / created).stat().st_mode) == 0o644, created
    (project / "api-policy.yaml").chmod(0o640)
    ok("api", "access", "orders", "read-only")
    assert stat.S_IMODE((project / "api-policy.yaml").stat().st_mode) == 0o640


def test_edits_keep_crlf_line_endings(project: Path, tmp_path: Path) -> None:
    ok(*ORDERS_ADD)
    path = project / "api-policy.yaml"
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    spec = tmp_path / "orders-openapi.yaml"
    spec.write_bytes(SPEC.encode().replace(b"\n", b"\r\n"))
    result = ok("api", "allow", "orders", "listOrders", "--methods", "GET")
    data = path.read_bytes()
    assert b"      - operationId: listOrders\r\n" in data
    assert data.count(b"\n") == data.count(b"\r\n")
    # The diff shows the entry, not every line rewritten.
    removed = [line for line in result.output.splitlines() if re.match(r"-[^-]", line)]
    assert removed == []


def test_outside_a_project_every_command_exits_3(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    for args in (ORDERS_ADD, ["api", "show"], ["api", "check"], ["api", "remove", "orders"]):
        result = cli(*args)
        assert result.exit_code == 3, (args, result.output)


# ---------------------------------------------------------------------------
# access / allow / deny / revoke / limits
# ---------------------------------------------------------------------------


def _commented_policy(project: Path) -> None:
    """The project's policy with comments, to check every edit keeps them."""
    ok(*ORDERS_ADD)
    path = project / "api-policy.yaml"
    path.write_text(
        path.read_text().replace(
            "    allowed_methods:", "    # reviewed by the orders team\n    allowed_methods:"
        )
        + "    # end of orders\n"
    )


def test_access_changes_the_methods_and_says_which_way(project: Path) -> None:
    _commented_policy(project)
    narrowed = ok("api", "access", "orders", "read-only")
    assert "narrows or keeps access" in narrowed.output
    assert policy(project)["orders"]["allowed_methods"] == ["GET", "HEAD"]
    widened = ok("api", "access", "orders", "custom", "--methods", "GET,HEAD,POST")
    assert "This widens access to orders" in widened.output
    same = ok("api", "access", "orders", "custom", "--methods", "post,head,get")
    assert "already allows" in same.output
    text = (project / "api-policy.yaml").read_text()
    assert "# reviewed by the orders team" in text and "# end of orders" in text


def test_access_keeps_the_comments_of_a_block_list(project: Path) -> None:
    ok(*[a if a != "read-write" else "read-only" for a in ORDERS_ADD])
    path = project / "api-policy.yaml"
    path.write_text(
        path.read_text().replace(
            "    allowed_methods: [GET, HEAD]\n",
            "    allowed_methods:\n      - GET   # reads\n      - HEAD  # probes\n",
        )
    )
    ok("api", "access", "orders", "custom", "--methods", "GET,PATCH")
    assert "      - GET   # reads\n      - PATCH\n" in path.read_text()
    assert policy(project)["orders"]["allowed_methods"] == ["GET", "PATCH"]


def test_allow_creates_the_list_and_warns_that_it_narrows(project: Path) -> None:
    _commented_policy(project)
    (project / "app/tools/orders_read.py").write_text(GET_TOOL)
    (project / "app/tools/orders_write.py").write_text(POST_TOOL)
    first = ok("api", "allow", "orders", "listOrders", "--methods", "GET")
    assert "This creates the list" in first.output and "This narrows access" in first.output
    # The POST tool is no longer covered: the effect on declared calls is shown.
    assert "now refused: orders_write.py: POST createOrder /orders" in first.output
    second = ok("api", "allow", "orders", "--method", "POST", "--path", "/orders")
    assert "now allowed: orders_write.py" in second.output
    assert "This widens access to orders" in second.output
    assert policy(project)["orders"]["allowed_operations"] == [
        {"operationId": "listOrders", "methods": ["GET"]},
        {"path": "/orders", "methods": ["POST"]},
    ]
    again = ok("api", "allow", "orders", "--method", "POST", "--path", "/orders")
    assert "already allows" in again.output
    assert "# reviewed by the orders team" in (project / "api-policy.yaml").read_text()


def test_allow_with_a_spec_fills_in_method_and_path(project: Path, tmp_path: Path) -> None:
    spec = project / "docs" / "orders.yaml"
    spec.parent.mkdir()
    spec.write_text(SPEC)
    ok(*ORDERS_ADD, "--openapi", str(spec))
    assert policy(project)["orders"]["openapi"] == "docs/orders.yaml"
    ok("api", "allow", "orders", "updateOrder")
    assert policy(project)["orders"]["allowed_operations"] == [
        {"operationId": "updateOrder", "path": "/orders/{order_id}", "methods": ["PATCH"]}
    ]
    missing = cli("api", "allow", "orders", "shipOrder")
    assert missing.exit_code == 3 and "shipOrder is not in docs/orders.yaml" in missing.output
    conflict = cli("api", "allow", "orders", "getOrder", "--methods", "DELETE")
    assert conflict.exit_code == 3 and "is GET /orders/{order_id}" in conflict.output


def test_allow_outside_allowed_methods_says_it_has_no_effect(project: Path) -> None:
    ok(*[a if a != "read-write" else "read-only" for a in ORDERS_ADD])
    result = ok("api", "allow", "orders", "createOrder", "--methods", "POST")
    assert "api access orders custom --methods GET,HEAD,POST" in result.output


def test_deny_and_revoke(project: Path) -> None:
    _commented_policy(project)
    (project / "app/tools/orders_read.py").write_text(GET_TOOL)
    denied = ok("api", "deny", "orders", "--method", "GET", "--path", "/orders")
    assert "now refused: orders_read.py" in denied.output
    assert policy(project)["orders"]["denied_operations"] == [
        {"path": "/orders", "methods": ["GET"]}
    ]
    by_id = ok("api", "deny", "orders", "deleteOrder")
    assert "refuses every call that names no operation_id" in by_id.output
    # Without a spec, a denial by id alone knows only the label: the command says so.
    assert "under another operation_id gets past it" in by_id.output
    assert "api deny orders deleteOrder --method M --path P" in by_id.output
    lifted = ok("api", "revoke", "orders", "--method", "GET", "--path", "/orders")
    assert "now allowed: orders_read.py" in lifted.output
    assert "This widens access to orders" in lifted.output
    assert policy(project)["orders"]["denied_operations"] == [{"operationId": "deleteOrder"}]
    ok("api", "revoke", "orders", "deleteOrder")
    # The last denial takes the key with it: no `denied_operations: []` left behind.
    assert "denied_operations" not in policy(project)["orders"]
    assert "denied_operations" not in (project / "api-policy.yaml").read_text()
    missing = cli("api", "revoke", "orders", "deleteOrder")
    assert missing.exit_code == 3 and "names deleteOrder" in missing.output
    assert "# end of orders" in (project / "api-policy.yaml").read_text()


def test_deny_and_allow_pin_the_endpoint_with_the_operation_id(project: Path) -> None:
    """Without a spec, OPERATION_ID --method M --path P writes an entry pinning all three."""
    ok(*ORDERS_ADD)
    denied = ok(
        *("api", "deny", "orders", "deleteOrder", "--method", "delete"),
        *("--path", "/orders/{order_id}"),
    )
    assert "under another operation_id" not in denied.output
    ok("api", "allow", "orders", "createOrder", "--method", "POST", "--path", "/orders")
    orders = policy(project)["orders"]
    assert orders["denied_operations"] == [
        {"operationId": "deleteOrder", "path": "/orders/{order_id}", "methods": ["DELETE"]}
    ]
    assert orders["allowed_operations"] == [
        {"operationId": "createOrder", "path": "/orders", "methods": ["POST"]}
    ]
    half = cli("api", "allow", "orders", "getOrder", "--path", "/orders/{order_id}")
    assert half.exit_code == 2 and "give both --method and --path" in half.output
    # revoke still names an entry one way: by id, or by method and path.
    both = cli("api", "revoke", "orders", "deleteOrder", "--method", "DELETE", "--path", "/x")
    assert both.exit_code == 2 and "not both" in both.output
    # An allow by id alone says that it pins no path.
    loose = ok("api", "allow", "orders", "listOrders", "--methods", "GET")
    assert "a call labelled listOrders is allowed on any path with GET" in loose.output


def test_a_relabelled_call_to_a_denied_endpoint_is_refused(project: Path) -> None:
    """The auditor's case: `api deny <id>` with a spec, then a call naming another id."""
    spec = project / "specs" / "orders.yaml"
    spec.parent.mkdir()
    spec.write_text(SPEC)
    ok(*ORDERS_ADD, "--openapi", "specs/orders.yaml")
    ok("api", "deny", "orders", "deleteOrder")
    for label in ("cancelOrder", "deleteOrdr", "getOrder"):
        tool = project / "app/tools/cancel.py"
        tool.write_text(
            "API_CALLS = [\n"
            f'    {{"api": "orders", "method": "DELETE", "operation_id": "{label}",\n'
            '     "path": "/orders/{order_id}"},\n'
            "]\n"
        )
        refused = cli("api", "check")
        assert refused.exit_code == 1, (label, refused.output)
        assert "denied by denied_operations (operationId=deleteOrder" in refused.output
        data = json.loads(ok("api", "show", "orders", "--json").output)
        assert [c["status"] for c in data["calls"]] == ["denied"], label
    # The runtime's rules (the same shared block) refuse it too.
    api = policy(project)["orders"]
    from graph_agents_cli._api_policy import refusal_reason

    for label in ("cancelOrder", None, "deleteOrder"):
        assert refusal_reason(api, "DELETE", label, "/orders/1"), label
    assert refusal_reason(api, "GET", "getOrder", "/orders/1") is None


def test_revoke_needs_from_when_both_lists_match(project: Path) -> None:
    ok(*ORDERS_ADD)
    ok("api", "allow", "orders", "getOrder")
    ok("api", "allow", "orders", "listOrders")
    ok("api", "deny", "orders", "getOrder")
    both = cli("api", "revoke", "orders", "getOrder")
    assert both.exit_code == 3 and "--from allowed or --from denied" in both.output
    ok("api", "revoke", "orders", "getOrder", "--from", "allowed")
    orders = policy(project)["orders"]
    assert orders["allowed_operations"] == [{"operationId": "listOrders"}]
    assert orders["denied_operations"] == [{"operationId": "getOrder"}]


def test_revoke_never_removes_the_last_allowed_entry(project: Path) -> None:
    ok(*ORDERS_ADD)
    ok("api", "allow", "orders", "listOrders")
    result = cli("api", "revoke", "orders", "listOrders")
    assert result.exit_code == 3
    assert "widens access" in result.output
    assert policy(project)["orders"]["allowed_operations"] == [{"operationId": "listOrders"}]


def test_revoke_one_method_of_an_entry_keeps_the_others(project: Path) -> None:
    ok(*ORDERS_ADD)
    ok(
        "api",
        "allow",
        "orders",
        "--method",
        "GET",
        "--path",
        "/orders/{order_id}",
        "--methods",
        "PUT",
    )
    ok("api", "allow", "orders", "listOrders")
    ok("api", "revoke", "orders", "--method", "PUT", "--path", "/orders/{order_id}")
    assert policy(project)["orders"]["allowed_operations"][0] == {
        "path": "/orders/{order_id}",
        "methods": ["GET"],
    }


def test_revoke_one_method_of_an_entry_without_methods_keeps_every_other_method(
    project: Path,
) -> None:
    ok(*ORDERS_ADD)
    path = project / "api-policy.yaml"
    path.write_text(
        path.read_text()
        + "    allowed_operations:\n      - path: /orders\n      - operationId: getOrder\n"
        + "    denied_operations:\n      - path: /internal/{x}\n"
    )
    # A denial of every method: revoking GET keeps it for every other method.
    lifted = ok("api", "revoke", "orders", "--method", "GET", "--path", "/internal/{x}")
    assert "only GET is revoked" in lifted.output
    assert policy(project)["orders"]["denied_operations"] == [
        {"path": "/internal/{x}", "methods": ["HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]}
    ]
    # An allowed entry without methods covers the API's methods: the others stay.
    ok("api", "revoke", "orders", "--method", "POST", "--path", "/orders", "--from", "allowed")
    assert policy(project)["orders"]["allowed_operations"][0] == {
        "path": "/orders",
        "methods": ["GET", "HEAD", "PUT", "PATCH", "DELETE"],
    }


def test_limits_set_change_and_clear(project: Path) -> None:
    _commented_policy(project)
    raised = ok("api", "limits", "orders", "--max-calls-per-run", "50")
    assert "This widens access" in raised.output
    assert policy(project)["orders"]["limits"] == {"max_calls_per_run": 50, "rate_per_minute": 120}
    ok("api", "limits", "orders", "--rate-per-minute", "none")
    assert policy(project)["orders"]["limits"] == {"max_calls_per_run": 50}
    ok("api", "limits", "orders", "--max-calls-per-run", "none")
    assert "limits" not in policy(project)["orders"]
    lowered = ok("api", "limits", "orders", "--rate-per-minute", "10")
    assert "narrows or keeps" in lowered.output
    assert cli("api", "limits", "orders").exit_code == 2
    assert cli("api", "limits", "orders", "--rate-per-minute", "0").exit_code == 2
    assert "# end of orders" in (project / "api-policy.yaml").read_text()


@pytest.mark.parametrize(
    ("block", "error"),
    [
        (
            "    approval: required\n",
            "apis.orders.approval: must be a mapping with required_for and approvers",
        ),
        (
            "    approval:\n      required_for: {methods: [POST]}\n      approvers: [admin]\n",
            "apis.orders.approval.approvers[0]: 'admin' is not an approver",
        ),
        (
            "    allowed_operations:\n      - operationId: cancelOrder\n        approval: true\n",
            "apis.orders.allowed_operations[0].approval: not valid on an operation entry; gate "
            "the operation with apis.orders.approval.required_for.operations",
        ),
    ],
)
def test_an_invalid_approval_is_refused(project: Path, block: str, error: str) -> None:
    ok(*ORDERS_ADD)
    path = project / "api-policy.yaml"
    path.write_text(path.read_text() + block)
    for args in (["api", "show"], ["api", "access", "orders", "read-only"]):
        result = cli(*args)
        assert result.exit_code == 3, result.output
        assert error in " ".join(result.output.split())
    # An invalid policy is a configuration error for the check too, as for lint.
    for args in (["api", "check"], ["lint", "--policy-only"]):
        result = cli(*args)
        assert result.exit_code == 3, (args, result.output)
        assert error in " ".join(result.output.split())


APPROVAL_BLOCK = """\
    # every write waits for the caller, or for ops
    approval:
      required_for:
        methods: [POST, DELETE]
        operations:
          - operationId: updateOrder
      approvers: [requester, role:ops]
"""
UPDATE_ORDER_TOOL = """\
API_CALLS = [
    {"api": "orders", "method": "PATCH", "operation_id": "updateOrder", "path": "/orders/{id}"},
]
TOOLS: list = []
"""


def test_show_check_and_lint_report_gated_calls_and_their_approvers(project: Path) -> None:
    ok(*ORDERS_ADD)
    path = project / "api-policy.yaml"
    path.write_text(path.read_text() + APPROVAL_BLOCK)
    (project / "app/tools/orders_read.py").write_text(GET_TOOL)
    (project / "app/tools/orders_write.py").write_text(POST_TOOL)
    (project / "app/tools/orders_update.py").write_text(UPDATE_ORDER_TOOL)

    data = json.loads(ok("api", "show", "--json").output)
    assert data["apis"]["orders"]["approval"] == {
        "required_for": {
            "methods": ["POST", "DELETE"],
            "operations": [{"operationId": "updateOrder"}],
        },
        "approvers": ["requester", "role:ops"],
        "timeout_s": 900,
    }
    calls = {c["tool"]: c for c in data["calls"]}
    assert calls["orders_read.py"]["approval"] is None
    assert calls["orders_write.py"]["status"] == "allowed"
    assert calls["orders_write.py"]["approval"] == {
        "approvers": ["requester", "role:ops"],
        "timeout_s": 900,
        "rule": "approval.required_for.methods ['POST', 'DELETE']",
        "rule_index": None,  # one approval mapping, not a list of rules
        "also_covered_by": [],
    }
    assert [r["rule"] for r in data["apis"]["orders"]["approval_rules"]] == ["approval"]
    assert calls["orders_update.py"]["approval"]["rule"] == (
        "approval.required_for.operations (operationId=updateOrder)"
    )
    assert data["gated"] == 2 and data["violations"] == 0

    text = " ".join(ok("api", "show").output.split())
    assert "approved by requester, role:ops; expires after 900 s" in text
    assert "2 declared call(s) wait for a human approval before they are sent" in text
    for args in (["api", "check"], ["lint", "--policy-only"]):
        checked = " ".join(ok(*args).output.split())
        assert "requester, role:ops (approval.required_for.methods ['POST', 'DELETE']" in checked
        assert "All declared API calls are allowed" in checked

    # The other commands keep the block (and its comment) as it is.
    ok("api", "limits", "orders", "--rate-per-minute", "60")
    ok("api", "deny", "orders", "--method", "DELETE", "--path", "/orders/{id}")
    text = path.read_text()
    assert text.endswith(APPROVAL_BLOCK)
    assert policy(project)["orders"]["approval"]["approvers"] == ["requester", "role:ops"]


def test_an_invalid_result_is_refused_and_nothing_is_written(project: Path) -> None:
    ok(*ORDERS_ADD)
    before = (project / "api-policy.yaml").read_text()
    result = cli("api", "allow", "orders", "--method", "GET", "--path", "/orders?x=1")
    assert result.exit_code == 2
    assert (project / "api-policy.yaml").read_text() == before


# ---------------------------------------------------------------------------
# remove / show / check
# ---------------------------------------------------------------------------


def test_remove_keeps_other_apis_and_the_last_one_removes_the_file(project: Path) -> None:
    ok(*ORDERS_ADD)
    ok(
        *("api", "add", "crm", "--base-url-env", "CRM_API_BASE_URL", "--auth", "bearer"),
        *("--token-env", "CRM_API_TOKEN", "--access", "read-only"),
    )
    (project / "app/tools/orders_read.py").write_text(GET_TOOL)
    removed = ok("api", "remove", "orders")
    assert "now refused: orders_read.py" in removed.output
    assert list(policy(project)) == ["crm"]
    keys = manifest(project)["secrets"]["keys"]
    assert "ORDERS_API_TOKEN" not in keys and "CRM_API_TOKEN" in keys
    env_example = (project / ".env.example").read_text()
    assert "ORDERS_API" not in env_example and "CRM_API_BASE_URL=" in env_example
    values = yaml.safe_load((project / "deployment/helm/shop/values.yaml").read_text())
    assert "ORDERS_API_BASE_URL" not in values["env"] and "CRM_API_BASE_URL" in values["env"]

    last = ok("api", "remove", "crm")
    assert "every outbound call is refused" in last.output
    assert not (project / "api-policy.yaml").exists()
    data = manifest(project)
    assert "api_policy" not in data and "CRM_API_TOKEN" not in data["secrets"]["keys"]
    values_text = (project / "deployment/helm/shop/values.yaml").read_text()
    assert "Base URLs of the APIs" not in values_text


@pytest.mark.parametrize("first", ["orders", "crm"])
def test_removing_apis_that_share_a_variable_leaves_the_project_as_created(
    project: Path, first: str
) -> None:
    fresh = {
        name: (project / name).read_text()
        for name in (
            ".env.example",
            "graph-agents-cli-manifest.yaml",
            "deployment/helm/shop/values.yaml",
        )
    }
    for name in ("orders", "crm"):
        ok(
            *("api", "add", name, "--base-url-env", "SHARED_API_BASE_URL", "--auth", "bearer"),
            *("--token-env", f"{name.upper()}_API_TOKEN", "--access", "read-only"),
        )
    ok("api", "remove", first)
    env_example = (project / ".env.example").read_text()
    assert "SHARED_API_BASE_URL=" in env_example  # still used by the other API
    # The shared variable's header names the API that still uses it, not the one removed.
    other = "crm" if first == "orders" else "orders"
    assert f"# {first} (auth:" not in env_example
    header = env_example.index(f"# {other} (auth: bearer)")
    assert env_example.index("SHARED_API_BASE_URL=") > header
    ok("api", "remove", "crm" if first == "orders" else "orders")
    # No orphaned "# <api> (auth: ...)" header, and the no-policy note is back.
    for name, text in fresh.items():
        assert (project / name).read_text() == text, name


def test_show_reports_the_effective_policy_and_the_declared_calls(project: Path) -> None:
    ok(*ORDERS_ADD)
    ok("api", "allow", "orders", "listOrders", "--methods", "GET")
    (project / "app/tools/orders_read.py").write_text(GET_TOOL)
    (project / "app/tools/orders_write.py").write_text(POST_TOOL)
    text = ok("api", "show").output
    assert "read-write preset" in text and "20 call(s) per run" in text
    assert "orders_write.py" in text
    assert "graph-agents-cli api allow orders createOrder" in text
    data = json.loads(ok("api", "show", "orders", "--json").output)
    orders = data["apis"]["orders"]
    assert orders["preset"] == "read-write"
    assert orders["allowed_operations"] == [{"operationId": "listOrders", "methods": ["GET"]}]
    assert orders["limits"] == {"max_calls_per_run": 20, "rate_per_minute": 120}
    statuses = {(c["tool"], c["status"]) for c in data["calls"]}
    assert statuses == {("orders_read.py", "allowed"), ("orders_write.py", "denied")}
    assert data["violations"] == 1
    assert cli("api", "show", "billing").exit_code == 3


def test_check_is_lint_policy_only(project: Path) -> None:
    ok(*ORDERS_ADD)
    (project / "app/tools/orders_read.py").write_text(GET_TOOL)
    assert "All declared API calls are allowed" in ok("api", "check").output
    ok("api", "deny", "orders", "listOrders")
    refused = cli("api", "check")
    lint = cli("lint", "--policy-only")
    assert refused.exit_code == lint.exit_code == 1
    assert "graph-agents-cli api revoke orders listOrders --from denied" in refused.output


# ---------------------------------------------------------------------------
# The documented lifecycle
# ---------------------------------------------------------------------------

README = Path(__file__).resolve().parents[2] / "README.md"
LIST_TOOL = """\
API_CALLS = [
    {"api": "orders", "method": "GET", "operation_id": "listOrders", "path": "/orders"},
]
TOOLS: list = []
"""
UPDATE_TOOL = """\
API_CALLS = [
    {"api": "orders", "method": "PATCH", "operation_id": "updateOrder", "path": "/orders/{id}"},
]
TOOLS: list = []
"""


def _readme_example() -> list[list[str]]:
    """The `graph-agents-cli api` commands of the README's "working agent" example."""
    text = README.read_text(encoding="utf-8")
    section = text.split("Adding functionality to a working agent", 1)[1]
    block = section.split("```bash\n", 1)[1].split("```", 1)[0]
    commands = []
    for line in block.splitlines():
        if line.startswith("graph-agents-cli api "):
            commands.append(shlex.split(line.split(" #", 1)[0])[1:])
    return commands


def test_the_readme_example_adds_functionality_without_breaking_the_agent(project: Path) -> None:
    """Step 1 of the README (read-only, no allow-list, a GET tool), then its worked example."""
    ok(*[a if a != "read-write" else "read-only" for a in ORDERS_ADD])
    (project / "app/tools/list_orders.py").write_text(LIST_TOOL)
    ok("api", "check")
    commands = _readme_example()
    assert any(c[:2] == ["api", "access"] for c in commands)
    for command in commands:
        result = ok(*command)
        assert "now refused" not in result.output, (command, result.output)
        assert "All declared API calls are allowed" in ok("api", "check").output, command
    (project / "app/tools/update_order.py").write_text(UPDATE_TOOL)
    assert "All declared API calls are allowed" in ok("api", "check").output
    orders = policy(project)["orders"]
    # PATCH reaches the listed operation only.
    assert orders["allowed_methods"] == ["GET", "HEAD", "PATCH"]
    assert [e["operationId"] for e in orders["allowed_operations"]] == ["listOrders", "updateOrder"]


def test_following_the_check_hint_for_a_denied_call_is_enough(project: Path) -> None:
    """The method, the denial and the allow-list: the hint names every change needed."""
    (project / "api-policy.yaml").write_text(
        "apis:\n"
        "  orders:\n"
        "    base_url_env: ORDERS_API_BASE_URL\n"
        "    auth: none\n"
        "    allowed_methods: [GET, POST]\n"
        "    allowed_operations:\n"
        "      - operationId: listOrders\n"
        "    denied_operations:\n"
        "      - operationId: deleteOrder\n"
    )
    (project / "app/tools/delete_order.py").write_text(
        LIST_TOOL.replace('"GET"', '"DELETE"').replace("listOrders", "deleteOrder")
    )
    refused = cli("api", "check")
    assert refused.exit_code == 1, refused.output
    hints = [
        line.strip()
        for line in refused.output.splitlines()
        if line.strip().startswith("graph-agents-cli api ")
    ]
    assert len(hints) == 1, refused.output
    steps = [re.sub(r" \(.*\)$", "", step) for step in hints[0].split("; then ")]
    assert steps == [
        "graph-agents-cli api access orders custom --methods GET,POST,DELETE",
        "graph-agents-cli api revoke orders deleteOrder --from denied",
        "graph-agents-cli api allow orders deleteOrder --method DELETE --path /orders",
    ]
    for step in steps:
        ok(*shlex.split(step)[1:])
    assert "All declared API calls are allowed" in ok("api", "check").output
    # The allow the hint wrote covers exactly the declared call, not the label anywhere.
    from graph_agents_cli._api_policy import refusal_reason

    api = policy(project)["orders"]
    assert refusal_reason(api, "DELETE", "deleteOrder", "/orders") is None
    assert refusal_reason(api, "DELETE", "deleteOrder", "/admin/all") is not None
    assert refusal_reason(api, "POST", "deleteOrder", "/orders") is not None


def test_access_custom_without_methods_names_the_positional_choice(project: Path) -> None:
    ok(*ORDERS_ADD)
    result = cli("api", "access", "orders", "custom")
    assert result.exit_code == 2
    assert "custom access needs --methods" in result.output
    assert "--access custom" not in result.output
    extra = cli("api", "access", "orders", "read-only", "--methods", "GET")
    assert extra.exit_code == 2 and "--methods goes with custom access only" in extra.output


@pytest.mark.parametrize(("target", "secrets_step"), [("kubernetes", True), ("none", False)])
def test_add_names_the_secrets_step_only_for_kubernetes(target: str, secrets_step: bool) -> None:
    from graph_agents_cli._project import ProjectConfig
    from graph_agents_cli.api._files import Plan
    from graph_agents_cli.api.cmd_api import _add_todos

    plan = Plan(Path("."))
    config = ProjectConfig(project_name="shop", create_params={"deployment_target": target})
    api = {"base_url_env": "B", "auth": "bearer", "token_env": "B_TOKEN"}
    _add_todos(plan, config, "billing", api)
    todos = " ".join(plan.left_for_you)
    assert "B_TOKEN" in todos
    assert ("secrets apply" in todos) is secrets_step
    assert (".env.<env>" in todos) is secrets_step


# --- output: copied specs, entries without effect, tables off a terminal -----------


def test_a_copied_spec_is_summarised_after_the_policy_diff(project: Path, tmp_path: Path) -> None:
    spec = tmp_path / "orders-openapi.yaml"
    spec.write_text(SPEC)
    result = ok(*ORDERS_ADD, "--openapi", str(spec), "--dry-run")
    out = result.output
    assert "+++ b/api-policy.yaml" in out
    assert "operationId: listOrders" not in out  # the spec's content is not printed
    summary = (
        f"openapi/orders/orders-openapi.yaml: new file, a copy of {spec} "
        f"({len(SPEC.splitlines())} lines; not shown)"
    )
    assert summary in " ".join(out.split())
    assert out.index("+++ b/api-policy.yaml") < out.index("openapi/orders/orders-openapi.yaml:")
    assert not (project / "openapi").exists()
    ok(*ORDERS_ADD, "--openapi", str(spec))
    assert (project / "openapi/orders/orders-openapi.yaml").read_text() == SPEC


def test_an_allow_the_methods_do_not_cover_has_no_effect_yet(project: Path) -> None:
    ok(*ORDERS_ADD[:-6], "--access", "read-only")  # GET, HEAD
    ok("api", "allow", "orders", "listOrders", "--method", "GET", "--path", "/orders")
    result = ok(
        "api", "allow", "orders", "updateOrder", "--method", "PATCH", "--path", "/orders/{id}"
    )
    assert "PATCH is not in orders's allowed_methods" in result.output
    assert "No effect on access to orders yet" in result.output
    assert "This widens access" not in result.output
    # Once PATCH is allowed, the change that makes it effective is the widening one.
    widened = ok("api", "access", "orders", "custom", "--methods", "GET,HEAD,PATCH")
    assert "This widens access to orders" in widened.output


def test_an_allow_a_denial_still_refuses_has_no_effect_yet(project: Path) -> None:
    ok(*ORDERS_ADD)
    ok("api", "deny", "orders", "deleteOrder", "--method", "DELETE", "--path", "/orders/{id}")
    ok("api", "allow", "orders", "listOrders", "--method", "GET", "--path", "/orders")
    result = ok(
        "api", "allow", "orders", "deleteOrder", "--method", "DELETE", "--path", "/orders/{id}"
    )
    assert "still refuses it (denials win)" in result.output
    assert "No effect on access to orders yet" in result.output


def test_tables_keep_long_cells_whole_off_a_terminal(project: Path) -> None:
    """CI logs and pipes: no COLUMNS, not a terminal; Rich would cut cells at 80 columns."""
    ok(*ORDERS_ADD)
    tools = project / "app" / "tools"
    (tools / "orders_with_a_rather_long_module_name.py").write_text(
        "API_CALLS = [\n"
        '    {"api": "orders", "method": "GET", "operation_id": "listOrdersForACustomer",'
        ' "path": "/customers/{customer_id}/orders/{order_id}/lines"},\n'
        "]\nTOOLS: list = []\n"
    )
    env = {**ENV, "COLUMNS": ""}
    result = CliRunner().invoke(main, ["api", "check"], env=env)
    assert "orders_with_a_rather_long_module_name" in result.output, result.output
    assert "/customers/{customer_id}/orders/{order_id}/lines" in result.output
    assert "…" not in result.output
    shown = CliRunner().invoke(main, ["api", "show", "orders"], env=env)
    assert shown.exit_code == 0, shown.output
    assert "…" not in shown.output


def test_the_generated_readme_never_goes_stale_with_the_policy(project: Path) -> None:
    """The README points at `api show` instead of embedding policy state `api` commands change."""
    readme = project / "README.md"
    before = readme.read_text()
    assert "No policy is declared yet" not in before
    assert "graph-agents-cli api show" in before
    assert "`graph-agents-cli-manifest.yaml` are exported" in before  # secrets.keys, not a copy
    ok(*ORDERS_ADD)
    assert readme.read_text() == before  # nothing in it became wrong
    assert "ORDERS_API_TOKEN" in manifest(project)["secrets"]["keys"]


# ---------------------------------------------------------------------------
# approval
# ---------------------------------------------------------------------------


def test_approval_adds_a_gate_with_a_diff_dry_run_first(project: Path) -> None:
    _commented_policy(project)
    (project / "app/tools/orders_write.py").write_text(POST_TOOL)
    (project / "app/tools/orders_read.py").write_text(GET_TOOL)
    before = (project / "api-policy.yaml").read_text()
    dry = ok(
        "api",
        "approval",
        "orders",
        "--methods",
        "post,DELETE",
        "--approvers",
        "requester",
        "--dry-run",
    )
    assert "+    approval:" in dry.output and "+        methods: [POST, DELETE]" in dry.output
    assert "Dry run: nothing was written." in dry.output
    assert (project / "api-policy.yaml").read_text() == before
    added = ok("api", "approval", "orders", "--methods", "POST,DELETE", "--approvers", "requester")
    text = " ".join(added.output.split())
    assert "now gated: orders_write.py: POST createOrder /orders (approvers: requester)" in text
    assert "orders_read.py" not in text
    assert "This tightens or keeps the approval gate on orders (always safe)" in text
    assert "requester confirms their own calls" in text
    assert policy(project)["orders"]["approval"] == {
        "required_for": {"methods": ["POST", "DELETE"]},
        "approvers": ["requester"],
    }
    written = (project / "api-policy.yaml").read_text()
    assert "# reviewed by the orders team" in written and "# end of orders" in written
    # The file stays valid for the runtime: the shared rules accept it.
    load_policy_document(project / "api-policy.yaml")


def test_approval_changes_parts_keeps_the_rest_and_says_when_it_loosens(project: Path) -> None:
    ok(*ORDERS_ADD)
    ok("api", "approval", "orders", "--methods", "POST", "--approvers", "requester")
    path = project / "api-policy.yaml"
    path.write_text(
        path.read_text().replace("      approvers:", "      # who decides\n      approvers:")
    )
    four_eyes = ok(
        "api", "approval", "orders", "--approvers", "role:ops,role:ops", "--timeout-s", "600"
    )
    text = " ".join(four_eyes.output.split())
    assert "loosens the approval gate on orders (new approver(s) role:ops" in text
    assert "Loosening an approval gate is a reviewed change" in text
    assert "requester is not an approver" in text
    # shared-bearer (the scaffold default) has one principal: say role approvers cannot decide.
    assert "only requester approvals can be decided" in text
    gate = policy(project)["orders"]["approval"]
    assert gate == {
        "required_for": {"methods": ["POST"]},
        "approvers": ["role:ops"],
        "timeout_s": 600,
    }
    assert "# who decides" in path.read_text()
    shorter = ok("api", "approval", "orders", "--timeout-s", "300")
    assert "tightens or keeps" in shorter.output
    longer = ok("api", "approval", "orders", "--timeout-s", "3600")
    assert "pending approvals wait longer (300 -> 3600 s)" in " ".join(longer.output.split())
    same = ok("api", "approval", "orders", "--timeout-s", "3600")
    assert "already says that" in same.output


def test_approval_operations_pin_the_endpoint_from_the_openapi_spec(
    project: Path, tmp_path: Path
) -> None:
    spec = tmp_path / "orders-openapi.yaml"
    spec.write_text(SPEC)
    ok(*ORDERS_ADD, "--openapi", str(spec))
    ok(
        "api",
        "approval",
        "orders",
        "--operations",
        "deleteOrder,updateOrder",
        "--approvers",
        "role:ops",
    )
    assert policy(project)["orders"]["approval"]["required_for"]["operations"] == [
        {"operationId": "deleteOrder", "path": "/orders/{order_id}", "methods": ["DELETE"]},
        {"operationId": "updateOrder", "path": "/orders/{order_id}", "methods": ["PATCH"]},
    ]
    missing = cli("api", "approval", "orders", "--operations", "noSuchOp")
    assert missing.exit_code == 3 and "noSuchOp is not in" in missing.output


def test_approval_operations_without_a_spec_warn_about_labels(project: Path) -> None:
    ok(*ORDERS_ADD)
    (project / "app/tools/orders_update.py").write_text(UPDATE_ORDER_TOOL)
    result = ok(
        "api", "approval", "orders", "--operations", "updateOrder", "--approvers", "requester"
    )
    text = " ".join(result.output.split())
    assert "the gate on updateOrder knows only the operationId" in text
    assert "now gated: orders_update.py: PATCH updateOrder /orders/{id}" in text
    # A hand-pinned entry survives a later --operations that names it again.
    path = project / "api-policy.yaml"
    path.write_text(
        path.read_text().replace(
            "- operationId: updateOrder",
            "- operationId: updateOrder\n            path: /orders/{id}\n            methods: [PATCH]",
        )
    )
    ok("api", "approval", "orders", "--operations", "updateOrder,cancelOrder")
    operations = policy(project)["orders"]["approval"]["required_for"]["operations"]
    assert operations[0] == {
        "operationId": "updateOrder",
        "path": "/orders/{id}",
        "methods": ["PATCH"],
    }
    assert operations[1] == {"operationId": "cancelOrder"}


def test_approval_pins_a_label_only_entry_once_the_spec_is_recorded(
    project: Path, tmp_path: Path
) -> None:
    ok(*ORDERS_ADD)
    ok("api", "approval", "orders", "--operations", "updateOrder", "--approvers", "requester")
    assert policy(project)["orders"]["approval"]["required_for"]["operations"] == [
        {"operationId": "updateOrder"}
    ]
    # Then the API's openapi spec is recorded (as `api add --openapi` would).
    (project / "orders-openapi.yaml").write_text(SPEC)
    path = project / "api-policy.yaml"
    path.write_text(
        path.read_text().replace(
            "    base_url_env: ORDERS_API_BASE_URL",
            "    base_url_env: ORDERS_API_BASE_URL\n    openapi: orders-openapi.yaml",
        )
    )
    assert policy(project)["orders"]["openapi"] == "orders-openapi.yaml"
    result = ok("api", "approval", "orders", "--operations", "updateOrder,deleteOrder")
    text = " ".join(result.output.split())
    assert "the gate on updateOrder is now pinned to its endpoint" in text
    assert "knows only the operationId" not in text
    # Pinning only gates more calls: not a loosening.
    assert "Loosening an approval gate" not in text
    assert policy(project)["orders"]["approval"]["required_for"]["operations"] == [
        # Its methods are kept (none: every method), so every call it gated still waits.
        {"operationId": "updateOrder", "path": "/orders/{order_id}"},
        {"operationId": "deleteOrder", "path": "/orders/{order_id}", "methods": ["DELETE"]},
    ]


def test_approval_repeats_the_label_only_note_for_a_kept_entry(project: Path) -> None:
    ok(*ORDERS_ADD)
    ok("api", "approval", "orders", "--operations", "updateOrder", "--approvers", "requester")
    result = ok("api", "approval", "orders", "--operations", "updateOrder,getOrder")
    text = " ".join(result.output.split())
    assert "the gate on updateOrder, getOrder knows only the operationId" in text


def test_approval_switching_methods_for_operations_and_removing(project: Path) -> None:
    ok(*ORDERS_ADD)
    (project / "app/tools/orders_write.py").write_text(POST_TOOL)
    ok("api", "approval", "orders", "--methods", "*", "--approvers", "requester")
    switched = ok("api", "approval", "orders", "--methods", "none", "--operations", "createOrder")
    text = " ".join(switched.output.split())
    assert "no longer wait as such" in text and "Loosening an approval gate" in text
    assert policy(project)["orders"]["approval"]["required_for"] == {
        "operations": [{"operationId": "createOrder"}]
    }
    nothing = cli("api", "approval", "orders", "--operations", "none")
    assert nothing.exit_code == 3 and "would gate nothing" in nothing.output
    removed = ok("api", "approval", "orders", "--remove")
    text = " ".join(removed.output.split())
    assert "no longer gated: orders_write.py" in text
    assert "no call waits for an approval any more" in text
    assert "approval" not in policy(project)["orders"]
    again = ok("api", "approval", "orders", "--remove")
    assert "has no approval block" in again.output


@pytest.mark.parametrize(
    ("args", "code", "message"),
    [
        ([], 2, "give --methods and/or --operations"),
        (["--remove", "--methods", "POST"], 2, "--remove takes no other option"),
        (["--methods", "POST"], 2, "--approvers is required for a new approval block"),
        (["--methods", "POST", "--approvers", "admin"], 2, "'admin' is not an approver"),
        (["--methods", "POST", "--approvers", "role:"], 2, "'role:' is not an approver"),
        (["--methods", "POST", "--approvers", "role:a b"], 2, "is not an approver"),
        (["--methods", "FETCH", "--approvers", "requester"], 2, "unknown HTTP method"),
        (["--methods", "POST", "--approvers", "requester", "--timeout-s", "29"], 2, "30<=x<=86400"),
        (["--operations", "a b", "--approvers", "requester"], 2, "contains whitespace"),
    ],
)
def test_approval_refuses_bad_input_and_writes_nothing(
    project: Path, args: list[str], code: int, message: str
) -> None:
    ok(*ORDERS_ADD)
    before = (project / "api-policy.yaml").read_text()
    result = cli("api", "approval", "orders", *args)
    assert result.exit_code == code, result.output
    assert message in " ".join(result.output.split())
    assert (project / "api-policy.yaml").read_text() == before


def test_approval_on_an_unknown_api_or_outside_a_project(
    project: Path, tmp_path: Path, monkeypatch
) -> None:
    ok(*ORDERS_ADD)
    unknown = cli("api", "approval", "crm", "--methods", "POST", "--approvers", "requester")
    assert unknown.exit_code == 3 and "'crm' is not declared" in unknown.output
    monkeypatch.chdir(tmp_path)
    outside = cli("api", "approval", "orders", "--methods", "POST", "--approvers", "requester")
    assert outside.exit_code == 3


def test_approval_notes_a_gate_on_a_method_the_api_does_not_allow(project: Path) -> None:
    ok(*[a if a != "read-write" else "read-only" for a in ORDERS_ADD])
    result = ok("api", "approval", "orders", "--methods", "DELETE", "--approvers", "requester")
    assert "approval never widens access" in " ".join(result.output.split())
    assert policy(project)["orders"]["allowed_methods"] == ["GET", "HEAD"]


# ---------------------------------------------------------------------------
# approval rules: other approvers for other calls of one API
# ---------------------------------------------------------------------------

ORDERS_SPEC = (
    SPEC
    + """\
  /orders/{order_id}/cancel:
    post: {operationId: cancelOrder, responses: {"200": {description: cancelled}}}
"""
)
ORDER_WRITE_CALLS = {
    "update_order.py": ("PATCH", "updateOrder", "/orders/{order_id}"),
    "cancel_order.py": ("POST", "cancelOrder", "/orders/{order_id}/cancel"),
    "create_order.py": ("POST", "createOrder", "/orders"),
}
UPDATE_ENTRY = {"operationId": "updateOrder", "path": "/orders/{order_id}", "methods": ["PATCH"]}
CANCEL_ENTRY = {
    "operationId": "cancelOrder",
    "path": "/orders/{order_id}/cancel",
    "methods": ["POST"],
}
CREATE_ENTRY = {"operationId": "createOrder", "path": "/orders", "methods": ["POST"]}
REQUESTER_RULE = ("--operations", "updateOrder,cancelOrder", "--approvers", "requester")
ADMIN_RULE = ("--operations", "createOrder", "--approvers", "role:admin", "--timeout-s", "3600")


def _orders_with_write_tools(project: Path, tmp_path: Path) -> None:
    """The orders API with its OpenAPI spec, and one tool per write call it makes."""
    spec = tmp_path / "orders-openapi.yaml"
    spec.write_text(ORDERS_SPEC)
    ok(*ORDERS_ADD, "--openapi", str(spec))
    for tool, (method, operation_id, path) in ORDER_WRITE_CALLS.items():
        call = {"api": "orders", "method": method, "operation_id": operation_id, "path": path}
        (project / "app/tools" / tool).write_text(
            f"API_CALLS = [\n    {json.dumps(call)},\n]\nTOOLS: list = []\n"
        )


def test_approval_rules_let_other_calls_wait_for_other_approvers(
    project: Path, tmp_path: Path
) -> None:
    """The requester confirms update and cancel, role:admin approves create: one API."""
    _orders_with_write_tools(project, tmp_path)
    ok("api", "approval", "orders", *REQUESTER_RULE)
    path = project / "api-policy.yaml"
    # Comments inside the block survive its conversion to a list of rules.
    path.write_text(
        path.read_text().replace(
            "      approvers: [requester]",
            "      # the customer confirms their own changes\n      approvers: [requester]",
        )
    )
    before = path.read_text()
    dry = ok("api", "approval", "orders", "--add-rule", *ADMIN_RULE, "--dry-run")
    text = " ".join(dry.output.split())
    assert "+      - required_for:" in dry.output and "+    approval:" not in dry.output
    assert (
        "now gated: create_order.py: POST createOrder /orders (approvers: role:admin; rule "
        "approval[1])" in text
    )
    assert "update_order.py" not in text and "cancel_order.py" not in text  # unchanged
    assert "This tightens or keeps the approval gate on orders (always safe)" in text
    assert "Dry run: nothing was written." in text
    assert path.read_text() == before

    ok("api", "approval", "orders", "--add-rule", *ADMIN_RULE)
    written = path.read_text()
    assert written.endswith(
        "    approval:\n"
        "      - required_for:\n"
        "          operations:\n"
        "            - operationId: updateOrder\n"
        "              path: /orders/{order_id}\n"
        "              methods: [PATCH]\n"
        "            - operationId: cancelOrder\n"
        "              path: /orders/{order_id}/cancel\n"
        "              methods: [POST]\n"
        "        # the customer confirms their own changes\n"
        "        approvers: [requester]\n"
        "      - required_for:\n"
        "          operations:\n"
        "            - operationId: createOrder\n"
        "              path: /orders\n"
        "              methods: [POST]\n"
        "        approvers: ['role:admin']\n"
        "        timeout_s: 3600\n"
    ), written
    assert policy(project)["orders"]["approval"] == [
        {"required_for": {"operations": [UPDATE_ENTRY, CANCEL_ENTRY]}, "approvers": ["requester"]},
        {
            "required_for": {"operations": [CREATE_ENTRY]},
            "approvers": ["role:admin"],
            "timeout_s": 3600,
        },
    ]
    load_policy_document(path)  # the shared rules (the runtime's too) accept it

    # show, check and lint name the rule, and the approvers, each declared call waits for.
    data = json.loads(ok("api", "show", "--json").output)
    gates = {c["tool"]: c["approval"] for c in data["calls"]}
    for tool in ("update_order.py", "cancel_order.py"):
        assert (gates[tool]["approvers"], gates[tool]["rule_index"]) == (["requester"], 0)
        assert gates[tool]["rule"].startswith("approval[0].required_for.operations")
    assert gates["create_order.py"] == {
        "approvers": ["role:admin"],
        "timeout_s": 3600,
        "rule": (
            "approval[1].required_for.operations (operationId=createOrder path=/orders "
            "methods=['POST'])"
        ),
        "rule_index": 1,
        "also_covered_by": [],
    }
    assert data["gated"] == 3
    rules = data["apis"]["orders"]["approval_rules"]
    assert [(r["rule"], r["approvers"], r["timeout_s"]) for r in rules] == [
        ("approval[0]", ["requester"], 900),
        ("approval[1]", ["role:admin"], 3600),
    ]
    assert data["apis"]["orders"]["approval"][1]["approvers"] == ["role:admin"]
    shown = " ".join(ok("api", "show", "orders").output.split())
    assert "approval[1]: operations createOrder POST /orders; approved by role:admin" in shown
    assert "a call waits for the first rule in file order that covers it" in shown
    for args in (["api", "check"], ["lint", "--policy-only"]):
        checked = " ".join(ok(*args).output.split())
        assert "role:admin (approval[1].required_for.operations (operationId=createOrder" in checked
        assert "requester (approval[0].required_for.operations (operationId=cancelOrder" in checked
        assert "more than one approval rule" not in checked

    # Adding the same rule again changes nothing.
    again = ok("api", "approval", "orders", "--add-rule", *ADMIN_RULE)
    assert "already has that approval rule (approval[1])" in again.output
    assert path.read_text() == written


def test_a_label_only_rule_before_a_rule_with_other_approvers_refuses_unnamed_calls(
    project: Path,
) -> None:
    """Without a spec, rule 0 knows update and cancel by label only; rule 1 gates every POST.

    A POST that names no operation id cannot be ruled out of rule 0, and rule 1 (other
    approvers) covers it too: it could be either rule's call, so it is refused (fail closed)
    instead of letting the requester approve what role:admin should decide.
    """
    ok(*ORDERS_ADD)
    tools = project / "app/tools"
    (tools / "place_order.py").write_text(
        "API_CALLS = [\n"
        '    {"api": "orders", "method": "POST", "path": "/orders"},\n'
        "]\nTOOLS: list = []\n"
    )
    (tools / "named_order.py").write_text(
        "API_CALLS = [\n"
        '    {"api": "orders", "method": "POST", "operation_id": "createOrder", "path": "/orders"},\n'
        "]\nTOOLS: list = []\n"
    )
    ok("api", "approval", "orders", *REQUESTER_RULE)
    result = ok(
        "api", "approval", "orders", "--add-rule", "--methods", "POST", "--approvers", "role:admin"
    )
    text = " ".join(result.output.split())
    assert "always safe" not in text
    assert "now refused: place_order.py: POST /orders" in text
    assert "now gated: named_order.py: POST createOrder /orders (approvers: role:admin" in text
    assert (
        "apis.orders.approval[0] names operations by operationId alone (operationId=updateOrder; "
        "operationId=cancelOrder), so it cannot rule out a call that names no operation_id, and "
        "approval[1] (approved by role:admin) may also cover such a call: the agent refuses it"
    ) in text
    assert "This tightens the approval gate on orders, and the agent refuses the calls" in text

    for command in (("api", "check"), ("lint", "--policy-only")):
        checked = cli(*command)
        assert checked.exit_code == 1, checked.output
        flat = " ".join(checked.output.split())
        assert "it could be either rule's call" in flat
        assert "name the operation: add operation_id to the call and to API_CALLS" in flat
    shown = json.loads(ok("api", "show", "orders", "--json").output)
    statuses = {c["tool"]: (c["status"], c["approval"]) for c in shown["calls"]}
    assert statuses["place_order.py"][0] == "denied"
    assert statuses["named_order.py"] == (
        "allowed",
        {
            "approvers": ["role:admin"],
            "timeout_s": 900,
            "rule": "approval[1].required_for.methods ['POST']",
            "rule_index": 1,
            "also_covered_by": [],
        },
    )

    # Naming the operation on the call ends the conflict: rule 1 gates it.
    (tools / "place_order.py").write_text(
        (tools / "named_order.py").read_text().replace("named", "place")
    )
    assert cli("api", "check").exit_code == 0
    # Removing rule 1 lets the unnamed call through again (to rule 0's approvers): loosening.
    (tools / "place_order.py").write_text(
        "API_CALLS = [\n"
        '    {"api": "orders", "method": "POST", "path": "/orders"},\n'
        "]\nTOOLS: list = []\n"
    )
    removed = ok("api", "approval", "orders", "--rule", "1", "--remove", "--dry-run")
    text = " ".join(removed.output.split())
    assert "This loosens the approval gate on orders" in text
    assert "now allowed: place_order.py: POST /orders (approvers: requester" in text


def test_approval_rule_changes_name_their_rule(project: Path, tmp_path: Path) -> None:
    _orders_with_write_tools(project, tmp_path)
    ok("api", "approval", "orders", *REQUESTER_RULE)
    ok("api", "approval", "orders", "--add-rule", *ADMIN_RULE)
    path = project / "api-policy.yaml"
    before = path.read_text()
    # With several rules, a change must say which one.
    unnamed = cli("api", "approval", "orders", "--approvers", "role:ops")
    assert unnamed.exit_code == 2
    text = " ".join(unnamed.output.split())
    assert "orders's approval is a list of 2 rules: name the one to change with --rule N" in text
    assert "approval[1]: operations createOrder POST /orders; approved by role:admin" in text
    missing = cli("api", "approval", "orders", "--rule", "2", "--approvers", "role:ops")
    assert missing.exit_code == 3 and "--rule takes 0 to 1" in missing.output
    assert path.read_text() == before

    # Four eyes for create: role:ops may approve it too. It loosens that rule only.
    wider = ok("api", "approval", "orders", "--rule", "1", "--approvers", "role:admin,role:ops")
    text = " ".join(wider.output.split())
    assert "This loosens the approval gate on orders (approval[1]: new approver(s) role:ops" in text
    assert "approvers change (new approver(s) role:ops): create_order.py" in text
    assert policy(project)["orders"]["approval"][1]["approvers"] == ["role:admin", "role:ops"]
    assert policy(project)["orders"]["approval"][0]["approvers"] == ["requester"]
    # Narrowing it back, and a shorter expiry, tighten.
    narrower = ok("api", "approval", "orders", "--rule", "1", "--approvers", "role:admin")
    assert "tightens or keeps" in narrower.output
    shorter = ok("api", "approval", "orders", "--rule", "1", "--timeout-s", "600")
    assert "tightens or keeps" in shorter.output
    assert policy(project)["orders"]["approval"][1]["timeout_s"] == 600
    same = ok("api", "approval", "orders", "--rule", "1", "--timeout-s", "600")
    assert "orders's approval rule approval[1] already says that" in " ".join(same.output.split())
    # Dropping cancel from the requester's rule sends it without a human: a loosening.
    dropped = ok("api", "approval", "orders", "--rule", "0", "--operations", "updateOrder")
    text = " ".join(dropped.output.split())
    assert "approval[0]: no longer gated as written: cancelOrder POST" in text
    assert "no longer gated: cancel_order.py" in text


def test_a_rule_that_covers_more_takes_calls_from_the_rules_after_it(
    project: Path, tmp_path: Path
) -> None:
    """The first rule in file order gates a call: widening an earlier rule moves calls to it."""
    _orders_with_write_tools(project, tmp_path)
    ok("api", "approval", "orders", *REQUESTER_RULE)
    ok("api", "approval", "orders", "--add-rule", *ADMIN_RULE)
    moved = ok(
        "api",
        "approval",
        "orders",
        "--rule",
        "0",
        "--operations",
        "updateOrder,cancelOrder,createOrder",
    )
    text = " ".join(moved.output.split())
    assert (
        "approvers change (new approver(s) requester): create_order.py: POST createOrder "
        "/orders: now requester; rule approval[0], was role:admin; rule approval[1]" in text
    )
    assert "approval[0] now comes first for calls approval[1] may have gated" in text
    assert "This loosens the approval gate on orders" in text
    # approval[1] now never applies: say so, in the command and in lint.
    assert "apis.orders.approval[1] never gates a call" in text
    checked = " ".join(ok("lint", "--policy-only").output.split())
    assert "apis.orders.approval[1] never gates a call" in checked
    assert "also covered by approval[1], which does not apply" in checked
    assert "1 gated call(s) are covered by more than one approval rule" in checked


def test_a_rule_is_removed_alone_and_the_last_one_with_the_block(
    project: Path, tmp_path: Path
) -> None:
    _orders_with_write_tools(project, tmp_path)
    ok("api", "approval", "orders", *REQUESTER_RULE)
    ok("api", "approval", "orders", "--add-rule", *ADMIN_RULE)
    ok("api", "approval", "orders", "--add-rule", "--methods", "DELETE", "--approvers", "role:ops")
    assert len(policy(project)["orders"]["approval"]) == 3
    removed = ok("api", "approval", "orders", "--rule", "1", "--remove")
    text = " ".join(removed.output.split())
    assert "no longer gated: create_order.py" in text
    assert "approval[1] is removed: the calls it gated first now wait for a later rule" in text
    assert "the rules after it move up one place" in text
    rules = policy(project)["orders"]["approval"]
    assert [r["approvers"] for r in rules] == [["requester"], ["role:ops"]]
    ok("api", "approval", "orders", "--rule", "1", "--remove")
    assert [r["approvers"] for r in policy(project)["orders"]["approval"]] == [["requester"]]
    # One rule left, still a list: it is also addressed without --rule.
    ok("api", "approval", "orders", "--timeout-s", "120")
    assert policy(project)["orders"]["approval"] == [
        {
            "required_for": {"operations": [UPDATE_ENTRY, CANCEL_ENTRY]},
            "approvers": ["requester"],
            "timeout_s": 120,
        }
    ]
    last = ok("api", "approval", "orders", "--rule", "0", "--remove")
    assert "no call waits for an approval any more" in " ".join(last.output.split())
    assert "approval" not in policy(project)["orders"]


def test_remove_without_a_rule_removes_every_rule(project: Path, tmp_path: Path) -> None:
    _orders_with_write_tools(project, tmp_path)
    ok("api", "approval", "orders", *REQUESTER_RULE)
    ok("api", "approval", "orders", "--add-rule", *ADMIN_RULE)
    removed = ok("api", "approval", "orders", "--remove")
    text = " ".join(removed.output.split())
    assert "no call waits for an approval any more" in text
    for tool in ORDER_WRITE_CALLS:
        assert f"no longer gated: {tool}" in text
    assert "approval" not in policy(project)["orders"]


def test_replacing_a_single_gate_suggests_adding_a_rule_instead(
    project: Path, tmp_path: Path
) -> None:
    """Naming other operations and approvers still replaces the one block, with a pointer."""
    _orders_with_write_tools(project, tmp_path)
    ok("api", "approval", "orders", *REQUESTER_RULE)
    replaced = ok(
        "api", "approval", "orders", "--operations", "createOrder", "--approvers", "role:admin"
    )
    text = " ".join(replaced.output.split())
    assert "no longer gated as written: updateOrder PATCH" in text
    assert (
        "add a rule instead: graph-agents-cli api approval orders --add-rule --operations "
        "createOrder --approvers role:admin" in text
    )
    assert policy(project)["orders"]["approval"]["approvers"] == ["role:admin"]


def test_add_rule_to_a_one_line_block_and_to_none(project: Path) -> None:
    ok(*ORDERS_ADD)
    path = project / "api-policy.yaml"
    path.write_text(
        path.read_text()
        + "    approval: {required_for: {methods: [DELETE]}, approvers: [requester]}  # deletes\n"
    )
    ok("api", "approval", "orders", "--add-rule", "--methods", "POST", "--approvers", "role:ops")
    text = path.read_text()
    assert re.search(r"\n    approval: +# deletes\n", text), text
    assert "      - {required_for: {methods: [DELETE]}, approvers: [requester]}\n" in text
    assert policy(project)["orders"]["approval"] == [
        {"required_for": {"methods": ["DELETE"]}, "approvers": ["requester"]},
        {"required_for": {"methods": ["POST"]}, "approvers": ["role:ops"]},
    ]
    # Without an approval block, the first rule added is the block itself.
    ok("api", "approval", "orders", "--remove")
    ok("api", "approval", "orders", "--add-rule", "--methods", "POST", "--approvers", "requester")
    assert policy(project)["orders"]["approval"] == {
        "required_for": {"methods": ["POST"]},
        "approvers": ["requester"],
    }


@pytest.mark.parametrize(
    ("args", "code", "message"),
    [
        (["--add-rule", "--rule", "0", "--methods", "POST"], 2, "give one of them"),
        (["--add-rule", "--remove"], 2, "--remove and --add-rule"),
        (["--rule", "0", "--remove", "--methods", "POST"], 2, "--remove takes no other option"),
        (["--add-rule", "--methods", "POST"], 2, "--add-rule needs --approvers"),
        (["--add-rule", "--approvers", "requester"], 2, "--add-rule needs --methods and/or"),
        (["--add-rule", "--timeout-s", "60", "--approvers", "x"], 2, "'x' is not an approver"),
        (["--rule", "-1", "--approvers", "requester"], 2, "-1 is not in the range x>=0"),
        (["--rule", "1", "--approvers", "requester"], 3, "--rule takes 0 to 0"),
        (["--rule", "0", "--approvers", "requester"], 3, "orders has no approval rules"),
    ],
)
def test_approval_rule_options_refuse_bad_input_and_write_nothing(
    project: Path, args: list[str], code: int, message: str
) -> None:
    ok(*ORDERS_ADD)
    if "no approval rules" not in message:
        ok("api", "approval", "orders", "--methods", "DELETE", "--approvers", "requester")
    before = (project / "api-policy.yaml").read_text()
    result = cli("api", "approval", "orders", *args)
    assert result.exit_code == code, result.output
    assert message in " ".join(result.output.split())
    assert (project / "api-policy.yaml").read_text() == before


def _readme_approval_examples() -> list[list[list[str]]]:
    """The `graph-agents-cli api approval` commands of the README's approval section, per block."""
    text = README.read_text(encoding="utf-8")
    section = text.split("### Human approval of calls (`approval`)", 1)[1].split("\n### ", 1)[0]
    blocks = []
    for block in section.split("```bash\n")[1:]:
        joined = block.split("```", 1)[0].replace("\\\n", " ")
        commands = [
            shlex.split(line.strip().split(" #", 1)[0])[1:]
            for line in joined.splitlines()
            if line.strip().startswith("graph-agents-cli api approval ")
        ]
        if commands:
            blocks.append(commands)
    return blocks


def test_the_readme_approval_examples_work(project: Path) -> None:
    """Each example block works on its own, from a policy without approval blocks."""
    blocks = _readme_approval_examples()
    assert [len(commands) for commands in blocks] == [2, 1, 1], blocks
    ok(*ORDERS_ADD)
    ok(
        *("api", "add", "payments", "--base-url-env", "PAYMENTS_API_BASE_URL"),
        *("--auth", "none", "--access", "read-write"),
    )
    path = project / "api-policy.yaml"
    fresh = path.read_text()
    results = []
    for commands in blocks:
        path.write_text(fresh)
        for command in commands:
            ok(*command)
        results.append(policy(project))
    rules, confirmation, four_eyes = results
    # Other approvers for other calls: two rules on one API.
    assert rules["orders"]["approval"] == [
        {
            "required_for": {
                "operations": [{"operationId": "updateOrder"}, {"operationId": "cancelOrder"}]
            },
            "approvers": ["requester"],
        },
        {
            "required_for": {"operations": [{"operationId": "createOrder"}]},
            "approvers": ["role:admin"],
            "timeout_s": 3600,
        },
    ]
    assert confirmation["orders"]["approval"]["approvers"] == ["requester"]
    assert four_eyes["payments"]["approval"] == {
        "required_for": {"operations": [{"operationId": "refundPayment"}]},
        "approvers": ["role:finance-approver"],
        "timeout_s": 3600,
    }
