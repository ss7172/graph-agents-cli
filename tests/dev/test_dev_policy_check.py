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
    (tools / "_private.py").write_text(
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
        "apis:\n  incidents:\n    base_url_env: X\n    auth: none\n    allowed_methods: [GET]\n"
        "    allowed_methods: ['*']\n"
    )
    report = pc.build_report(project, "app")
    assert [r.status for r in report.results] == [pc.STATUS_INVALID]
    assert "found duplicate key 'allowed_methods'" in report.results[0].reason


def test_strict_policy_errors_are_reported(project):
    (project / "api-policy.yaml").write_text(
        "apis:\n  incidents:\n    base_url_env: X\n    auth: bearer\n    allowed_methods: [GET]\n"
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
        "apis:\n  incidents:\n    base_url_env: X\n    auth: forward\n    allowed_methods: [GET]\n"
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
