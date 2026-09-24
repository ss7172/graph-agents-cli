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

"""The static API-policy check (``lint``) against fixture tools and policies."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
import yaml

from graph_agents_cli._api_policy import path_matches
from graph_agents_cli._output import Console
from graph_agents_cli.dev import policy_check as pc

TOOL_GET = '''"""Incident tools."""
from something_heavy import model  # never imported by the check

API_CALLS = [
    {"api": "incidents", "method": "GET", "operation_id": "getIncident"},
    {"api": "incidents", "method": "get", "path": "/sites/{siteId}/topology"},
]


def get_incident(incident_id: str) -> dict:
    return {}
'''

TOOL_POST = """API_CALLS: list[dict] = [
    {"api": "incidents", "method": "POST", "operation_id": "closeIncident"},
]
"""

OPENAPI = {
    "openapi": "3.1.0",
    "paths": {
        "/incidents/{id}": {
            "get": {"operationId": "getIncident"},
            "post": {"operationId": "closeIncident"},
        },
        "/sites/{siteId}/topology": {"get": {"operationId": "getTopology"}},
    },
}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    tools = tmp_path / "app" / "tools"
    tools.mkdir(parents=True)
    (tools / "incidents.py").write_text(TOOL_GET)
    (tools / "actions.py").write_text(TOOL_POST)
    # The package's __init__.py (get_tools) is not a tool module: never read.
    (tools / "__init__.py").write_text(
        'API_CALLS = [{"api": "incidents", "method": "DELETE", "path": "/x"}]\n'
    )
    (tools / "helpers.py").write_text("def nothing():\n    return 1\n")
    return tmp_path


def api(**overrides: object) -> dict:
    """One API's settings: bearer, every method allowed unless overridden."""
    return {
        "base_url_env": "INCIDENTS_API_BASE_URL",
        "auth": "bearer",
        "token_env": "INCIDENTS_API_TOKEN",
        "allowed_methods": ["*"],
        **overrides,
    }


def write_policy(root: Path, **apis: dict) -> None:
    (root / "api-policy.yaml").write_text(yaml.safe_dump({"apis": apis}))


def statuses(report: pc.PolicyReport) -> dict[str, str]:
    return {r.call.operation: r.status for r in report.results}


# ---------------------------------------------------------------------------
# reading declarations
# ---------------------------------------------------------------------------


def test_collect_declared_calls_reads_literals_without_importing(project):
    calls, problems = pc.collect_declared_calls(project / "app" / "tools")
    assert problems == []
    assert {(c.tool, c.api, c.method, c.operation) for c in calls} == {
        ("incidents.py", "incidents", "GET", "getIncident"),
        ("incidents.py", "incidents", "GET", "/sites/{siteId}/topology"),
        ("actions.py", "incidents", "POST", "closeIncident"),
    }


@pytest.mark.parametrize(
    ("source", "fragment"),
    [
        ("API_CALLS = build_calls()\n", "is not a literal list"),
        ("API_CALLS = ['GET /x']\n", "is not a dict"),
        ('API_CALLS = [{"method": "GET", "path": "/x"}]\n', 'has no "api"'),
        ('API_CALLS = [{"api": "a", "path": "/x"}]\n', "has no valid method"),
        ('API_CALLS = [{"api": "a", "method": "FETCH", "path": "/x"}]\n', "has no valid method"),
        ('API_CALLS = [{"api": "a", "method": "GET"}]\n', "neither operation_id nor path"),
        ('API_CALLS = [{"api": "a", "method": "GET", "path": "x"}]\n', "starting with /"),
        ('API_CALLS = [{"api": "a", "method": "GET", "path": "/x?y=1"}]\n', "no query"),
        ('API_CALLS = [{"api": "a", "method": "GET", "path": "/x", "url": "u"}]\n', "unknown key"),
        ("API_CALLS = [\n", "syntax error"),
    ],
)
def test_read_api_calls_reports_unreadable_or_invalid_entries(tmp_path, source, fragment):
    tool = tmp_path / "bad.py"
    tool.write_text(source)
    calls, problems = pc.read_api_calls(tool)
    assert calls == []
    assert len(problems) == 1 and fragment in problems[0], problems


def test_private_modules_are_read_too(project):
    """get_tools() imports _-prefixed modules, so their calls are checked as well."""
    tools = project / "app" / "tools"
    (tools / "_shared.py").write_text(
        'API_CALLS = [{"api": "incidents", "method": "DELETE", "path": "/admin/1"}]\n'
    )
    calls, problems = pc.collect_declared_calls(tools)
    assert problems == []
    assert ("_shared.py", "DELETE", "/admin/1") in {(c.tool, c.method, c.operation) for c in calls}
    assert "__init__.py" not in {c.tool for c in calls}


def test_subpackages_are_read_like_get_tools_imports_them(project):
    """get_tools() walks pkgutil.iter_modules, which yields subpackages too: lint
    reads their __init__.py and every module inside them (the top-level
    __init__.py stays the registry), and labels each by its path under tools/."""
    tools = project / "app" / "tools"
    sub = tools / "billing"
    (sub / "deep").mkdir(parents=True)
    (sub / "__init__.py").write_text(
        'API_CALLS = [{"api": "incidents", "method": "GET", "path": "/admin/1"}]\nTOOLS = []\n'
    )
    (sub / "deep" / "calls.py").write_text(
        'API_CALLS = [{"api": "incidents", "method": "DELETE", "path": "/x"}]\n'
    )
    (sub / "deep" / "bad.py").write_text("API_CALLS = make()\n")
    # Caches and hidden directories are not importable tool modules.
    (sub / "__pycache__").mkdir()
    (sub / "__pycache__" / "junk.py").write_text("API_CALLS = nope()\n")
    (tools / ".hidden").mkdir()
    (tools / ".hidden" / "x.py").write_text("API_CALLS = nope()\n")

    calls, problems = pc.collect_declared_calls(tools)
    found = {(c.tool, c.method, c.operation) for c in calls}
    assert ("billing/__init__.py", "GET", "/admin/1") in found
    assert ("billing/deep/calls.py", "DELETE", "/x") in found
    assert "__init__.py" not in {c.tool for c in calls}
    assert len(problems) == 1 and problems[0].startswith("billing/deep/bad.py:"), problems


def test_an_unreadable_tool_module_is_a_problem_not_silence(project):
    tools = project / "app" / "tools"
    (tools / "latin1.py").write_bytes(b"# \xe9\nAPI_CALLS = []\n")
    _calls, problems = pc.collect_declared_calls(tools)
    assert any(p.startswith("latin1.py: cannot be read") for p in problems), problems


def test_subpackage_calls_are_judged_against_the_policy(project):
    write_policy(project, incidents=api(allowed_methods=["GET"]))
    sub = project / "app" / "tools" / "sub"
    sub.mkdir()
    (sub / "__init__.py").write_text(
        'API_CALLS = [{"api": "incidents", "method": "DELETE", "path": "/admin/1"}]\n'
    )
    report = pc.build_report(project, "app")
    denied = [r for r in report.results if r.call.tool == "sub/__init__.py"]
    assert [r.status for r in denied] == [pc.STATUS_DENIED]
    assert report.violations >= 1


def test_symlinked_tool_directories_are_followed_once(project, tmp_path):
    outside = tmp_path / "shared_tools"
    outside.mkdir()
    (outside / "__init__.py").write_text(
        'API_CALLS = [{"api": "incidents", "method": "GET", "path": "/linked"}]\n'
    )
    tools = project / "app" / "tools"
    (tools / "linked").symlink_to(outside, target_is_directory=True)
    # A link back to the package itself must not loop.
    (outside / "loop").symlink_to(tools, target_is_directory=True)
    calls, _problems = pc.collect_declared_calls(tools)
    assert ("linked/__init__.py", "/linked") in {(c.tool, c.operation) for c in calls}


@pytest.mark.parametrize(
    ("source", "line"),
    [
        # Each case adds a DELETE the literal does not show.
        ('API_CALLS += [{"api": "a", "method": "DELETE", "path": "/admin/1"}]\n', 2),
        ('API_CALLS.append({"api": "a", "method": "DELETE", "path": "/admin/1"})\n', 2),
        ('API_CALLS.extend([{"api": "a", "method": "DELETE", "path": "/x"}])\n', 2),
        ('API_CALLS[0]["method"] = "DELETE"\n', 2),
        ('API_CALLS = [{"api": "a", "method": "DELETE", "path": "/x"}]\n', 2),
        ('if True:\n    API_CALLS = [{"api": "a", "method": "DELETE", "path": "/x"}]\n', 3),
        ("from other import API_CALLS\n", 2),
        ("def f():\n    global API_CALLS\n    API_CALLS = []\n", 4),
        ("for API_CALLS in [[]]:\n    pass\n", 2),
        ("del API_CALLS\n", 2),
    ],
)
def test_changes_outside_the_literal_are_reported(tmp_path, source, line):
    tool = tmp_path / "tool.py"
    tool.write_text('API_CALLS = [{"api": "a", "method": "GET", "path": "/items"}]\n' + source)
    calls, problems = pc.read_api_calls(tool)
    assert [(c.method, c.operation) for c in calls] == [("GET", "/items")]
    assert len(problems) == 1, problems
    assert f"line {line} binds or changes API_CALLS" in problems[0]


def test_reads_and_annotations_of_api_calls_are_not_changes(tmp_path):
    tool = tmp_path / "tool.py"
    tool.write_text(
        "API_CALLS: list[dict[str, str]]\n"
        'API_CALLS: list[dict[str, str]] = [{"api": "a", "method": "GET", "path": "/x"}]\n'
        "NAMES = [call['path'] for call in API_CALLS]\n"
        "COUNT = len(API_CALLS)\n"
        "FIRST = API_CALLS[0].get('path')\n"
    )
    calls, problems = pc.read_api_calls(tool)
    assert problems == []
    assert [c.operation for c in calls] == ["/x"]


def test_a_rebinding_without_a_literal_declaration_is_still_reported(tmp_path):
    tool = tmp_path / "tool.py"
    tool.write_text("API_CALLS, OTHER = [], 1\n")
    calls, problems = pc.read_api_calls(tool)
    assert calls == []
    assert len(problems) == 1 and "line 1 binds or changes API_CALLS" in problems[0]


def test_leftover_product_calls_is_an_error_with_a_rename_hint(tmp_path):
    tool = tmp_path / "old.py"
    tool.write_text('PRODUCT_CALLS = [{"method": "GET", "path": "/x"}]\n')
    calls, problems = pc.read_api_calls(tool)
    assert calls == []
    assert "PRODUCT_CALLS was renamed to API_CALLS" in problems[0]
    assert '"api"' in problems[0]


# ---------------------------------------------------------------------------
# the policy
# ---------------------------------------------------------------------------


def test_no_policy_file_refuses_every_declared_call(project):
    report = pc.build_report(project, "app")
    assert report.policy_path is None
    assert set(statuses(report).values()) == {pc.STATUS_DENIED}
    assert report.violations == 3
    assert "refused" in report.results[0].reason
    assert any("no api-policy.yaml" in n for n in report.notes)


def test_no_policy_and_no_calls_is_clean(tmp_path):
    (tmp_path / "app" / "tools").mkdir(parents=True)
    (tmp_path / "app" / "tools" / "weather.py").write_text("API_CALLS = []\n")
    report = pc.build_report(tmp_path, "app")
    assert report.results == [] and report.violations == 0


def test_a_declared_but_missing_policy_file_is_invalid(project):
    report = pc.build_report(project, "app", policy_declared=True)
    assert report.results[0].status == pc.STATUS_INVALID
    assert "does not exist" in report.results[0].reason


def test_undeclared_api_is_denied(project):
    write_policy(project, billing=api(base_url_env="B", token_env="T"))
    report = pc.build_report(project, "app")
    assert set(statuses(report).values()) == {pc.STATUS_DENIED}
    assert "not declared" in report.results[0].reason


def test_allowed_methods_get_only_denies_post(project):
    write_policy(project, incidents=api(allowed_methods=["GET"]))
    report = pc.build_report(project, "app")
    result = statuses(report)
    assert result["getIncident"] == pc.STATUS_ALLOWED
    assert result["/sites/{siteId}/topology"] == pc.STATUS_ALLOWED
    assert result["closeIncident"] == pc.STATUS_DENIED
    assert report.violations == 1
    denied = next(r for r in report.results if r.status == pc.STATUS_DENIED)
    assert "not in allowed_methods" in denied.reason


def test_allowed_operations_allow_list_and_methods(project):
    write_policy(
        project,
        incidents=api(
            allowed_operations=[
                {"operationId": "getIncident"},
                {"path": "/sites/{siteId}/topology", "methods": ["GET"]},
            ]
        ),
    )
    result = statuses(pc.build_report(project, "app"))
    assert result["getIncident"] == pc.STATUS_ALLOWED
    assert result["/sites/{siteId}/topology"] == pc.STATUS_ALLOWED
    assert result["closeIncident"] == pc.STATUS_DENIED


def test_an_entry_pinning_operation_and_path_needs_both(tmp_path):
    tools = tmp_path / "app" / "tools"
    tools.mkdir(parents=True)
    (tools / "t.py").write_text(
        "API_CALLS = [\n"
        '    {"api": "a", "method": "GET", "operation_id": "getItem", "path": "/items/{id}"},\n'
        '    {"api": "a", "method": "GET", "operation_id": "getItem", "path": "/admin"},\n'
        '    {"api": "a", "method": "GET", "operation_id": "getItem"},\n'
        "]\n"
    )
    write_policy(
        tmp_path,
        a=api(allowed_operations=[{"operationId": "getItem", "path": "/items/{item_id}"}]),
    )
    result = statuses(pc.build_report(tmp_path, "app"))
    assert result["getItem /items/{id}"] == pc.STATUS_ALLOWED
    assert result["getItem /admin"] == pc.STATUS_DENIED
    # Without a path the pinned path cannot match: declare it.
    assert result["getItem"] == pc.STATUS_DENIED


def test_denied_operations_win_over_allows(project):
    write_policy(
        project,
        incidents=api(
            allowed_operations=[{"operationId": "getIncident"}, {"operationId": "closeIncident"}],
            denied_operations=[{"operationId": "closeIncident"}],
        ),
    )
    report = pc.build_report(project, "app")
    denied = next(r for r in report.results if r.call.operation == "closeIncident")
    assert denied.status == pc.STATUS_DENIED
    assert "denied by denied_operations" in denied.reason


@pytest.mark.parametrize(
    ("template", "path", "expected"),
    [
        ("/sites/{siteId}/topology", "/sites/{siteId}/topology", True),
        ("/sites/{siteId}/topology", "/sites/{site_id}/topology", True),
        ("/sites/{siteId}/topology", "/sites/42/topology", True),
        ("/sites/{siteId}/topology", "/sites/42/topology/extra", False),
        ("/sites/{siteId}/topology", "/sites/topology", False),
        ("/items/{id}.json", "/items/7.json", True),
        # One trailing slash is not a different path.
        ("/items", "/items/", True),
        ("/items/{id}", "/%69tems/7", True),
        ("/items/{id}", "/Items/7", False),
    ],
)
def test_path_matches(template: str, path: str, expected: bool):
    assert path_matches(template, path) is expected


def test_an_operation_id_denial_refuses_calls_that_name_no_operation_id(tmp_path):
    """Fail closed: the client cannot tell such a call apart from the denied operation."""
    tools = tmp_path / "app" / "tools"
    tools.mkdir(parents=True)
    (tools / "t.py").write_text(
        "API_CALLS = [\n"
        '    {"api": "a", "method": "DELETE", "path": "/items/{item_id}"},\n'
        '    {"api": "a", "method": "DELETE", "operation_id": "archiveItem", "path": "/items/{id}"},\n'
        '    {"api": "a", "method": "GET", "path": "/items/{item_id}"},\n'
        "]\n"
    )
    write_policy(
        tmp_path, a=api(denied_operations=[{"operationId": "deleteItem", "methods": ["DELETE"]}])
    )
    report = pc.build_report(tmp_path, "app")
    by_call = {(r.call.method, r.call.operation): r for r in report.results}
    refused = by_call[("DELETE", "/items/{item_id}")]
    assert refused.status == pc.STATUS_DENIED
    assert "the call names no operation_id" in refused.reason
    assert by_call[("DELETE", "archiveItem /items/{id}")].status == pc.STATUS_ALLOWED
    # The denial pins DELETE: other methods are unaffected.
    assert by_call[("GET", "/items/{item_id}")].status == pc.STATUS_ALLOWED


def test_the_spec_names_the_operation_of_a_call_declared_by_path(tmp_path):
    """The auditor's case: the spec maps DELETE /items/{item_id} to the denied deleteItem."""
    tools = tmp_path / "app" / "tools"
    tools.mkdir(parents=True)
    (tools / "t.py").write_text(
        'API_CALLS = [{"api": "a", "method": "DELETE", "path": "/items/{item_id}"}]\n'
    )
    spec = {"paths": {"/items/{item_id}": {"delete": {"operationId": "deleteItem"}}}}
    (tmp_path / "openapi.yaml").write_text(yaml.safe_dump(spec))
    write_policy(
        tmp_path, a=api(openapi="openapi.yaml", denied_operations=[{"operationId": "deleteItem"}])
    )
    report = pc.build_report(tmp_path, "app")
    assert report.violations == 1
    reason = report.results[0].reason
    assert "denied by denied_operations (operationId=deleteItem)" in reason
    assert "the OpenAPI spec names this operation deleteItem" in reason


def test_a_call_declared_by_operation_id_is_judged_with_the_spec_path(tmp_path):
    """The client always sends a path, so a path denial applies to an id-only declaration."""
    tools = tmp_path / "app" / "tools"
    tools.mkdir(parents=True)
    (tools / "t.py").write_text(
        "API_CALLS = [\n"
        '    {"api": "a", "method": "GET", "operation_id": "getAdmin"},\n'
        '    {"api": "a", "method": "GET", "operation_id": "getItem"},\n'
        "]\n"
    )
    spec = {
        "paths": {
            "/admin/{section}": {"get": {"operationId": "getAdmin"}},
            "/items/{id}": {"get": {"operationId": "getItem"}},
        }
    }
    (tmp_path / "openapi.yaml").write_text(yaml.safe_dump(spec))
    write_policy(
        tmp_path, a=api(openapi="openapi.yaml", denied_operations=[{"path": "/Admin/{x}"}])
    )
    result = statuses(pc.build_report(tmp_path, "app"))
    assert result == {"getAdmin": pc.STATUS_DENIED, "getItem": pc.STATUS_ALLOWED}


def test_a_repeated_policy_key_is_invalid(project):
    (project / "api-policy.yaml").write_text(
        "apis:\n  incidents:\n    base_url_env: X\n    auth: none\n    allowed_methods: [GET, POST]\n"
        "    allowed_methods: ['*']\n"
    )
    report = pc.build_report(project, "app")
    assert [r.status for r in report.results] == [pc.STATUS_INVALID]
    assert "found duplicate key 'allowed_methods'" in report.results[0].reason


def test_strict_policy_errors_are_reported(project):
    (project / "api-policy.yaml").write_text(
        "apis:\n  incidents:\n    base_url_env: X\n    auth: bearer\n    allowed_methods: [GET, POST]\n"
        "    allowed_method: [POST]\n"
    )
    report = pc.build_report(project, "app")
    reasons = [r.reason for r in report.results]
    assert all(r.status == pc.STATUS_INVALID for r in report.results)
    assert any("unknown key 'allowed_method'" in r for r in reasons)
    assert any("token_env: required when auth is bearer" in r for r in reasons)


def test_legacy_policy_shape_gets_the_migration_hint(project):
    (project / "api-policy.yaml").write_text("product_api:\n  auth: none\n")
    report = pc.build_report(project, "app")
    assert "retired single-API format" in report.results[0].reason


def test_malformed_policy_is_invalid(project):
    (project / "api-policy.yaml").write_text("apis: [unclosed\n")
    report = pc.build_report(project, "app")
    assert report.results[0].status == pc.STATUS_INVALID
    assert "not valid YAML" in report.results[0].reason


def test_forward_is_refused_under_langgraph_server(project):
    (project / "api-policy.yaml").write_text(
        "apis:\n  incidents:\n    base_url_env: X\n    auth: forward\n    allowed_methods: [GET, POST]\n"
    )
    fastapi = pc.build_report(project, "app", runtime="fastapi")
    assert not any(r.status == pc.STATUS_INVALID for r in fastapi.results)
    server = pc.build_report(project, "app", runtime="langgraph-server")
    invalid = [r for r in server.results if r.status == pc.STATUS_INVALID]
    assert len(invalid) == 1 and "persists the run context" in invalid[0].reason


# ---------------------------------------------------------------------------
# OpenAPI
# ---------------------------------------------------------------------------


def test_openapi_lookup_yaml(project):
    (project / "docs").mkdir()
    (project / "docs" / "openapi.yaml").write_text(yaml.safe_dump(OPENAPI))
    write_policy(project, incidents=api(openapi="docs/openapi.yaml"))
    report = pc.build_report(project, "app")
    assert report.openapi_paths["incidents"] == project / "docs" / "openapi.yaml"
    assert report.violations == 0
    assert set(statuses(report).values()) == {pc.STATUS_ALLOWED}


def test_openapi_lookup_json_flags_unknown_operation_and_method_mismatch(project):
    spec = json.loads(json.dumps(OPENAPI))
    spec["paths"]["/incidents/{id}"]["get"]["operationId"] = "fetchIncident"
    spec["paths"]["/incidents/{id}"]["put"] = spec["paths"]["/incidents/{id}"].pop("post")
    (project / "spec.json").write_text(json.dumps(spec))
    write_policy(project, incidents=api(openapi="spec.json"))
    report = pc.build_report(project, "app")
    result = statuses(report)
    assert result["getIncident"] == pc.STATUS_UNKNOWN
    assert result["closeIncident"] == pc.STATUS_UNKNOWN
    assert result["/sites/{siteId}/topology"] == pc.STATUS_ALLOWED
    mismatch = next(r for r in report.results if r.call.operation == "closeIncident")
    assert "is PUT" in mismatch.reason


def test_openapi_lookup_matches_concrete_paths_against_spec_templates(tmp_path):
    tools = tmp_path / "app" / "tools"
    tools.mkdir(parents=True)
    (tools / "t.py").write_text(
        'API_CALLS = [{"api": "a", "method": "GET", "path": "/sites/42/topology"}]\n'
    )
    (tmp_path / "spec.yaml").write_text(yaml.safe_dump(OPENAPI))
    write_policy(tmp_path, a=api(openapi="spec.yaml"))
    assert statuses(pc.build_report(tmp_path, "app")) == {"/sites/42/topology": pc.STATUS_ALLOWED}


def test_missing_openapi_spec_is_invalid(project):
    write_policy(project, incidents=api(openapi="docs/missing.yaml"))
    report = pc.build_report(project, "app")
    assert report.results[0].status == pc.STATUS_INVALID
    assert "cannot load" in report.results[0].reason


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------


def test_unreadable_declaration_is_a_violation(project):
    (project / "app" / "tools" / "dynamic.py").write_text("API_CALLS = make()\n")
    write_policy(project, incidents=api())
    report = pc.build_report(project, "app")
    invalid = [r for r in report.results if r.status == pc.STATUS_INVALID]
    assert len(invalid) == 1 and invalid[0].call.tool == "dynamic.py"


def test_run_policy_check_prints_table_and_returns_violations(project):
    write_policy(project, incidents=api(allowed_methods=["GET"]))
    buf = io.StringIO()
    console = Console(file=buf, width=200)
    assert pc.run_policy_check(project, "app", console=console) == 1
    out = buf.getvalue()
    assert "API policy check" in out and "closeIncident" in out and "1 violation" in out


def test_run_policy_check_without_tools_dir(tmp_path):
    buf = io.StringIO()
    assert pc.run_policy_check(tmp_path, "app", console=Console(file=buf, width=200)) == 0
    assert "nothing to check" in buf.getvalue()


# ---------------------------------------------------------------------------
# the example tool's call
# ---------------------------------------------------------------------------


def _example(tmp_path: Path | None = None, **apis: dict) -> pc.ExampleCall | None:
    return pc.example_call({"apis": apis}, base_dir=tmp_path)


def test_example_uses_the_first_allowed_operation():
    orders = api(
        allowed_methods=["GET"],
        allowed_operations=[{"operationId": "listOrders", "path": "/orders"}],
    )
    call = _example(orders=orders)
    assert call == pc.ExampleCall(
        api="orders", method="GET", path="/orders", operation_id="listOrders"
    )
    assert call.params == ()
    assert not call.has_body


def test_example_takes_the_first_allowed_operation_whatever_its_method():
    orders = api(
        allowed_methods=["GET", "POST"],
        allowed_operations=[
            {"operationId": "createOrder", "path": "/orders", "methods": ["POST"]},
            {"operationId": "getOrder", "path": "/orders/{order_id}", "methods": ["GET"]},
        ],
    )
    call = _example(orders=orders)
    assert call == pc.ExampleCall(
        api="orders", method="POST", path="/orders", operation_id="createOrder"
    )
    assert call.has_body
    assert call.as_context()["has_body"] is True
    # An entry without pinned methods takes the API's methods in the file's order.
    patcher = api(
        allowed_methods=["PATCH", "GET"],
        allowed_operations=[{"operationId": "updateOrder", "path": "/orders/{order_id}"}],
    )
    assert _example(orders=patcher) == pc.ExampleCall(
        api="orders", method="PATCH", path="/orders/{order_id}", operation_id="updateOrder"
    )


def test_example_skips_operations_it_cannot_make():
    orders = api(
        allowed_methods=["GET", "POST"],
        allowed_operations=[
            {"operationId": "outside", "path": "/o", "methods": ["DELETE"]},  # not allowed
            {"operationId": "byClass", "path": "/classes/{class}"},  # a Python keyword
            {"operationId": "own", "path": "/runs/{runtime}"},  # the tool's own parameter
            {"operationId": "withBody", "path": "/b/{body}"},  # the body parameter's name
            {"operationId": "priv", "path": "/p/{_id}"},  # pydantic refuses _ fields
            {"operationId": "quoted", "path": '/q/"x"'},  # not safe to render
            {"operationId": "noPath"},  # no spec to say where it lives
            {"operationId": "getOrder", "path": "/orders/{order_id}", "methods": ["GET"]},
        ],
    )
    call = _example(orders=orders)
    assert call == pc.ExampleCall(
        api="orders", method="GET", path="/orders/{order_id}", operation_id="getOrder"
    )
    assert call.params == ("order_id",)


def test_example_falls_back_to_a_generic_operation_of_an_allowed_method():
    assert _example(open=api()) == pc.ExampleCall(
        api="open", method="GET", path="/items/{item_id}", operation_id="getItem"
    )
    assert _example(writer=api(allowed_methods=["POST"])) == pc.ExampleCall(
        api="writer", method="POST", path="/items", operation_id="createItem"
    )
    # The file's order decides, not a preference for any method.
    assert _example(w=api(allowed_methods=["DELETE", "GET"])) == pc.ExampleCall(
        api="w", method="DELETE", path="/items/{item_id}", operation_id="deleteItem"
    )
    denied = api(denied_operations=[{"path": "/items/{id}"}])
    assert _example(open=denied) == pc.ExampleCall(
        api="open", method="POST", path="/items", operation_id="createItem"
    )
    closed = api(denied_operations=[{"path": "/items/{id}"}, {"path": "/items"}])
    assert _example(open=closed) is None


def test_example_is_none_when_the_first_api_allows_nothing_it_can_make():
    unplaceable = api(allowed_methods=["POST"], allowed_operations=[{"operationId": "noPath"}])
    assert _example(writer=unplaceable) is None
    # Only the first API is used, as the template documents.
    assert _example(writer=unplaceable, reader=api()) is None


def test_example_honours_fail_closed_denials_by_operation_id():
    orders = api(
        allowed_operations=[{"path": "/orders"}],
        denied_operations=[{"operationId": "deleteOrder"}],
    )
    # A call without operation_id cannot be told apart from deleteOrder, and the
    # generic fallbacks are not in allowed_operations: no example.
    assert _example(orders=orders) is None


def test_example_takes_paths_and_ids_from_the_openapi_spec(tmp_path):
    (tmp_path / "spec.yaml").write_text(yaml.safe_dump(OPENAPI))
    by_id = api(allowed_operations=[{"operationId": "getIncident"}], openapi="spec.yaml")
    assert _example(tmp_path, incidents=by_id) == pc.ExampleCall(
        api="incidents", method="GET", path="/incidents/{id}", operation_id="getIncident"
    )
    by_path = api(allowed_operations=[{"path": "/sites/{siteId}/topology"}], openapi="spec.yaml")
    assert _example(tmp_path, incidents=by_path) == pc.ExampleCall(
        api="incidents",
        method="GET",
        path="/sites/{siteId}/topology",
        operation_id="getTopology",
    )
    # No allowed_operations: the spec's first operation, never an operation it lacks.
    assert _example(tmp_path, incidents=api(openapi="spec.yaml")) == pc.ExampleCall(
        api="incidents", method="GET", path="/incidents/{id}", operation_id="getIncident"
    )
    writer = api(allowed_methods=["POST"], openapi="spec.yaml")
    assert _example(tmp_path, incidents=writer) == pc.ExampleCall(
        api="incidents", method="POST", path="/incidents/{id}", operation_id="closeIncident"
    )


def test_example_without_its_spec_does_not_guess(tmp_path):
    missing = api(openapi="missing.yaml")
    assert _example(tmp_path, incidents=missing) is None
    pinned = api(openapi="missing.yaml", allowed_operations=[{"operationId": "a", "path": "/a"}])
    assert _example(tmp_path, incidents=pinned) == pc.ExampleCall(
        api="incidents", method="GET", path="/a", operation_id="a"
    )


@pytest.mark.parametrize(
    "policy",
    [
        {"allowed_operations": [{"operationId": "listOrders", "path": "/orders"}]},
        {"allowed_operations": [{"path": "/orders/{order_id}"}]},
        {"allowed_methods": ["GET"], "denied_operations": [{"path": "/admin/{x}"}]},
        {"allowed_methods": ["*"]},
        {"allowed_methods": ["POST", "PUT"]},
        {"allowed_methods": ["PATCH"], "denied_operations": [{"operationId": "x"}]},
    ],
)
def test_every_example_passes_lint(tmp_path, policy):
    """What create renders is what lint (and the runtime's shared rules) accept."""
    document = {"apis": {"svc": api(**policy)}}
    call = pc.example_call(document)
    assert call is not None
    declared = pc.DeclaredCall(
        tool="example_api.py",
        api=call.api,
        method=call.method,
        operation_id=call.operation_id,
        path=call.path,
    )
    assert pc.check_call(declared, document).status == pc.STATUS_ALLOWED


# ---------------------------------------------------------------------------
# hints: the command that would allow a refused call
# ---------------------------------------------------------------------------


def test_refused_calls_carry_the_api_command_that_would_allow_them():
    document = {
        "apis": {
            "orders": api(
                allowed_methods=["GET"],
                allowed_operations=[{"operationId": "listOrders"}],
                denied_operations=[
                    {
                        "operationId": "deleteOrder",
                        "path": "/orders/{order_id}",
                        "methods": ["DELETE"],
                    },
                    {"operationId": "purgeOrders", "methods": ["DELETE"]},
                    {"path": "/admin/{section}"},
                ],
            )
        }
    }

    def hint(method: str, operation_id: str | None, path: str | None, name: str = "orders"):
        call = pc.DeclaredCall(
            tool="t.py", api=name, method=method, operation_id=operation_id, path=path
        )
        result = pc.check_call(call, document)
        assert result.status == pc.STATUS_DENIED
        return result.hint

    # An allow pins the call's method, and its path when it names one: exactly that call.
    assert hint("GET", "getOrder", "/orders/{order_id}") == (
        "graph-agents-cli api allow orders getOrder --method GET --path /orders/{order_id}"
    )
    assert hint("POST", "createOrder", "/orders") == (
        "graph-agents-cli api access orders custom --methods GET,POST; then "
        "graph-agents-cli api allow orders createOrder --method POST --path /orders"
    )
    assert hint("GET", "getReport", None) == (
        'name the path: add "path" to the call\'s API_CALLS entry (a denial by path '
        "refuses declared calls that name none; the client always sends one)"
    )
    assert hint("GET", None, "/reports") == (
        "graph-agents-cli api allow orders --method GET --path /reports"
    )
    # Unnamed, and covered by a denial by operationId alone: fixed in the tool.
    assert hint("DELETE", None, "/carts/7").startswith("name the operation")
    assert hint("GET", "adminReport", "/admin/{section}").startswith(
        "graph-agents-cli api revoke orders --method GET --path /admin/{section} --from denied"
    )
    assert hint("GET", "x", "/x", name="billing").startswith(
        "graph-agents-cli api add billing --base-url-env"
    )
    assert "--access <read-only|read-write|custom>" in hint("GET", "x", "/x", name="billing")
    # A denied call outside the method and the allow-list: every step it needs, in order.
    assert hint("DELETE", "deleteOrder", "/orders/{order_id}") == (
        "graph-agents-cli api access orders custom --methods GET,DELETE; then "
        "graph-agents-cli api revoke orders deleteOrder --from denied (lifts a deliberate "
        "denial: make sure it should go); then graph-agents-cli api allow orders deleteOrder "
        "--method DELETE --path /orders/{order_id}"
    )
    # A relabelled call to the denied endpoint is refused by that denial all the same.
    assert "revoke orders deleteOrder --from denied" in hint(
        "DELETE", "cancelOrder", "/orders/{order_id}"
    )
    # A listed operation that is also denied needs the revoke only.
    orders = document["apis"]["orders"]
    orders["allowed_methods"] = ["GET", "DELETE"]
    orders["allowed_operations"].append({"operationId": "cancelOrder", "methods": ["DELETE"]})
    assert hint("DELETE", "cancelOrder", "/orders/{order_id}") == (
        "graph-agents-cli api revoke orders deleteOrder --from denied (lifts a deliberate "
        "denial: make sure it should go)"
    )


def test_a_declared_operation_id_must_be_the_one_the_spec_gives_the_call(tmp_path):
    """With a spec, a typo or relabelled operation_id is refused, not trusted."""
    tools = tmp_path / "app" / "tools"
    tools.mkdir(parents=True)
    (tools / "t.py").write_text(
        "API_CALLS = [\n"
        '    {"api": "a", "method": "POST", "operation_id": "createOrdr", "path": "/orders"},\n'
        '    {"api": "a", "method": "POST", "operation_id": "listOrders", "path": "/orders"},\n'
        '    {"api": "a", "method": "POST", "operation_id": "createOrder", "path": "/orders"},\n'
        '    {"api": "a", "method": "POST", "operation_id": "shipOrder"},\n'
        '    {"api": "a", "method": "DELETE", "operation_id": "cancelOrder",\n'
        '     "path": "/orders/{order_id}"},\n'
        "]\n"
    )
    spec = {
        "paths": {
            "/orders": {
                "get": {"operationId": "listOrders"},
                "post": {"operationId": "createOrder"},
            },
            "/orders/{order_id}": {"delete": {"operationId": "deleteOrder"}},
        }
    }
    (tmp_path / "openapi.yaml").write_text(yaml.safe_dump(spec))
    write_policy(
        tmp_path,
        a=api(
            allowed_methods=["GET", "POST", "DELETE"],
            openapi="openapi.yaml",
            denied_operations=[
                {"operationId": "deleteOrder", "path": "/orders/{order_id}", "methods": ["DELETE"]}
            ],
        ),
    )
    report = pc.build_report(tmp_path, "app")
    by_id = {r.call.operation_id: r for r in report.results}
    typo = by_id["createOrdr"]
    assert typo.status == pc.STATUS_UNKNOWN
    assert typo.reason == (
        "operationId createOrdr is not in the OpenAPI spec; the spec names POST /orders createOrder"
    )
    assert '"operation_id": "createOrder" for POST /orders' in typo.hint
    assert by_id["listOrders"].status == pc.STATUS_UNKNOWN
    assert "is GET /orders in the spec, not POST" in by_id["listOrders"].reason
    assert by_id["createOrder"].status == pc.STATUS_ALLOWED
    assert by_id["shipOrder"].status == pc.STATUS_UNKNOWN
    # Relabelled and aimed at the denied endpoint: denied, and the label is flagged too.
    relabelled = by_id["cancelOrder"]
    assert relabelled.status == pc.STATUS_DENIED
    assert "denied by denied_operations (operationId=deleteOrder" in relabelled.reason
    assert "also, operationId cancelOrder is not in the OpenAPI spec" in relabelled.reason
    assert '"operation_id": "deleteOrder"' in relabelled.hint
    assert report.violations == 4


def test_an_invalid_policy_file_is_a_configuration_error(tmp_path):
    (tmp_path / "api-policy.yaml").write_text(
        "apis:\n  orders:\n    base_url_env: X\n    auth: none\n    allowed_methods: [GET]\n"
        "    approval: required\n"
    )
    buf = io.StringIO()
    with pytest.raises(pc.InvalidPolicyFile) as raised:
        pc.run_policy_check(tmp_path, "app", console=Console(file=buf, width=300))
    assert raised.value.exit_code == 3
    assert "approval gates are not supported yet" in buf.getvalue()
    # The manifest names the file, but it is missing: the same.
    (tmp_path / "api-policy.yaml").unlink()
    with pytest.raises(pc.InvalidPolicyFile):
        pc.run_policy_check(
            tmp_path, "app", policy_declared=True, console=Console(file=io.StringIO())
        )


def test_the_report_prints_each_hint_once(tmp_path):
    write_policy(tmp_path, orders=api(allowed_methods=["GET"]))
    tools = tmp_path / "app" / "tools"
    tools.mkdir(parents=True)
    call = '{"api": "orders", "method": "PUT", "operation_id": "replaceOrder", "path": "/o"}'
    (tools / "a.py").write_text(f"API_CALLS = [{call}]\n")
    (tools / "b.py").write_text(f"API_CALLS = [{call}]\n")
    buf = io.StringIO()
    assert pc.run_policy_check(tmp_path, "app", console=Console(file=buf, width=300)) == 2
    out = buf.getvalue()
    assert out.count("graph-agents-cli api access orders custom --methods GET,PUT") == 1
    assert "reviewed pull request" in out
