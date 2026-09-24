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
import shutil
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
    lifted = ok("api", "revoke", "orders", "--method", "GET", "--path", "/orders")
    assert "now allowed: orders_read.py" in lifted.output
    assert "This widens access to orders" in lifted.output
    assert policy(project)["orders"]["denied_operations"] == [{"operationId": "deleteOrder"}]
    ok("api", "revoke", "orders", "deleteOrder")
    assert policy(project)["orders"]["denied_operations"] == []
    missing = cli("api", "revoke", "orders", "deleteOrder")
    assert missing.exit_code == 3 and "names deleteOrder" in missing.output
    assert "# end of orders" in (project / "api-policy.yaml").read_text()


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


def test_the_approval_key_is_reserved_and_refused(project: Path) -> None:
    ok(*ORDERS_ADD)
    path = project / "api-policy.yaml"
    path.write_text(path.read_text() + "    approval: required\n")
    for args in (["api", "show"], ["api", "access", "orders", "read-only"]):
        result = cli(*args)
        assert result.exit_code == 3, result.output
        assert "approval gates are not supported yet (planned); remove the approval key" in (
            result.output
        )
    check = cli("api", "check")
    assert check.exit_code != 0 and "approval gates are not supported yet" in check.output


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
