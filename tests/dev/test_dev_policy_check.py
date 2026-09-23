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

"""The static product-policy check against fixture tools and policies."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
import yaml

from graph_agents_cli._output import Console
from graph_agents_cli.dev import policy_check as pc

TOOL_GET = '''"""Incident tools."""
from something_heavy import model  # never imported by the check

PRODUCT_CALLS = [
    {"method": "GET", "operation_id": "getIncident"},
    {"method": "get", "path": "/sites/{siteId}/topology"},
]


def get_incident(incident_id: str) -> dict:
    return {}
'''

TOOL_POST = """PRODUCT_CALLS: list[dict] = [
    {"method": "POST", "operation_id": "closeIncident"},
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
    (tools / "_private.py").write_text('PRODUCT_CALLS = [{"method": "DELETE", "path": "/x"}]\n')
    (tools / "helpers.py").write_text("def nothing():\n    return 1\n")
    return tmp_path


def write_policy(root: Path, policy: dict, *, wrapped: bool = True) -> None:
    data = {"product_api": policy} if wrapped else policy
    (root / "product-policy.yaml").write_text(yaml.safe_dump(data))


def statuses(report: pc.PolicyReport) -> dict[str, str]:
    return {r.call.operation: r.status for r in report.results}


# ---------------------------------------------------------------------------
# reading declarations
# ---------------------------------------------------------------------------


def test_collect_declared_calls_reads_literals_without_importing(project):
    calls, problems = pc.collect_declared_calls(project / "app" / "tools")
    assert problems == []
    assert [(c.tool, c.method, c.operation_id, c.path) for c in calls] == [
        ("actions.py", "POST", "closeIncident", None),
        ("incidents.py", "GET", "getIncident", None),
        ("incidents.py", "GET", None, "/sites/{siteId}/topology"),
    ]


def test_read_product_calls_reports_unreadable_or_invalid_entries(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("PRODUCT_CALLS = build_calls()\n")
    calls, problems = pc.read_product_calls(bad)
    assert calls == [] and "not a literal list" in problems[0]

    partial = tmp_path / "partial.py"
    partial.write_text('PRODUCT_CALLS = [{"operation_id": "x"}, {"method": "GET"}, "str"]\n')
    calls, problems = pc.read_product_calls(partial)
    assert calls == []
    assert len(problems) == 3


# ---------------------------------------------------------------------------
# policy evaluation
# ---------------------------------------------------------------------------


def test_no_policy_file_is_unrestricted(project):
    report = pc.build_report(project, "app")
    assert report.violations == 0
    assert all(r.status == pc.STATUS_ALLOWED for r in report.results)
    assert any("unrestricted" in n for n in report.notes)


def test_allowed_methods_get_only_denies_post(project):
    write_policy(project, {"allowed_methods": ["GET"]})
    report = pc.build_report(project, "app")
    assert statuses(report) == {
        "closeIncident": pc.STATUS_DENIED,
        "getIncident": pc.STATUS_ALLOWED,
        "/sites/{siteId}/topology": pc.STATUS_ALLOWED,
    }
    assert report.violations == 1


def test_allowed_operations_allow_list_and_methods(project):
    write_policy(
        project,
        {
            "allowed_operations": [
                {"operationId": "getIncident"},
                {"path": "/sites/{siteId}/topology", "methods": ["GET"]},
                {"operationId": "closeIncident", "methods": ["PUT"]},
            ]
        },
        wrapped=False,
    )
    report = pc.build_report(project, "app")
    assert statuses(report)["getIncident"] == pc.STATUS_ALLOWED
    assert statuses(report)["/sites/{siteId}/topology"] == pc.STATUS_ALLOWED
    # closeIncident is allowed only as PUT, the tool declares POST.
    assert statuses(report)["closeIncident"] == pc.STATUS_DENIED


def test_policy_paths_are_templates_like_the_runtime_client(project):
    """`/sites/{siteId}/topology` covers a renamed placeholder and a concrete path."""
    tools = project / "app" / "tools"
    (tools / "topology.py").write_text(
        "PRODUCT_CALLS = [\n"
        '    {"method": "GET", "path": "/sites/{site_id}/topology"},\n'
        '    {"method": "GET", "path": "/sites/42/topology"},\n'
        '    {"method": "GET", "path": "/sites/42/other"},\n'
        "]\n"
    )
    write_policy(
        project,
        {"allowed_operations": [{"path": "/sites/{siteId}/topology", "methods": ["GET"]}]},
    )
    report = pc.build_report(project, "app")
    s = statuses(report)
    assert s["/sites/{siteId}/topology"] == pc.STATUS_ALLOWED
    assert s["/sites/{site_id}/topology"] == pc.STATUS_ALLOWED
    assert s["/sites/42/topology"] == pc.STATUS_ALLOWED
    assert s["/sites/42/other"] == pc.STATUS_DENIED

    # Denials use the same matcher: a concrete declaration under a denied template is denied.
    write_policy(
        project,
        {"allowed_methods": ["GET"], "denied_operations": [{"path": "/sites/{siteId}/topology"}]},
    )
    s = statuses(pc.build_report(project, "app"))
    assert s["/sites/42/topology"] == pc.STATUS_DENIED
    assert s["/sites/42/other"] == pc.STATUS_ALLOWED

    # An entry pinning both operationId and path needs both to match.
    (tools / "topology.py").write_text(
        'PRODUCT_CALLS = [{"method": "GET", "operation_id": "getTopology", "path": "/other"}]\n'
    )
    write_policy(
        project,
        {
            "allowed_operations": [
                {"operationId": "getTopology", "path": "/sites/{siteId}/topology"}
            ]
        },
    )
    assert statuses(pc.build_report(project, "app"))["getTopology"] == pc.STATUS_DENIED


def test_openapi_lookup_matches_concrete_paths_against_spec_templates(project):
    (project / "app" / "tools" / "topology.py").write_text(
        'PRODUCT_CALLS = [{"method": "GET", "path": "/sites/42/topology"}]\n'
    )
    (project / "openapi.json").write_text(json.dumps(OPENAPI))
    write_policy(project, {"openapi": "openapi.json"})
    report = pc.build_report(project, "app")
    result = next(r for r in report.results if r.call.path == "/sites/42/topology")
    assert result.status == pc.STATUS_ALLOWED
    assert result.reason == "spec: GET /sites/{siteId}/topology"


@pytest.mark.parametrize(
    ("template", "path", "expected"),
    [
        ("/sites/{siteId}/topology", "/sites/{siteId}/topology", True),
        ("/sites/{siteId}/topology", "/sites/{site_id}/topology", True),
        ("/sites/{siteId}/topology", "/sites/42/topology", True),
        ("/sites/{siteId}/topology", "/sites/42/topology?expand=1", True),
        ("/sites/{siteId}/topology", "/sites/42/topology/extra", False),
        ("/sites/{siteId}/topology", "/sites/topology", False),
    ],
)
def test_path_matches(template: str, path: str, expected: bool):
    assert pc._path_matches(template, path) is expected


def test_denied_operations_win_over_allows(project):
    write_policy(
        project,
        {"allowed_methods": ["GET", "POST"], "denied_operations": [{"operationId": "getIncident"}]},
    )
    report = pc.build_report(project, "app")
    assert statuses(report)["getIncident"] == pc.STATUS_DENIED
    assert "denied_operations" in next(
        r.reason for r in report.results if r.call.operation == "getIncident"
    )
    assert statuses(report)["closeIncident"] == pc.STATUS_ALLOWED


def test_openapi_lookup_yaml(project):
    (project / "docs").mkdir()
    (project / "docs" / "product-openapi.yaml").write_text(yaml.safe_dump(OPENAPI))
    write_policy(project, {"openapi": "docs/product-openapi.yaml"})
    report = pc.build_report(project, "app")
    assert report.openapi_path == project / "docs" / "product-openapi.yaml"
    assert statuses(report) == {
        "closeIncident": pc.STATUS_ALLOWED,
        "getIncident": pc.STATUS_ALLOWED,
        "/sites/{siteId}/topology": pc.STATUS_ALLOWED,
    }


def test_openapi_lookup_json_flags_unknown_operation_and_method_mismatch(project):
    spec = json.loads(json.dumps(OPENAPI))
    del spec["paths"]["/sites/{siteId}/topology"]
    spec["paths"]["/incidents/{id}"]["post"] = {"operationId": "reopenIncident"}
    spec["paths"]["/incidents/{id}"]["put"] = {"operationId": "closeIncident"}
    (project / "openapi.json").write_text(json.dumps(spec))
    write_policy(project, {"openapi": "openapi.json"})
    report = pc.build_report(project, "app")
    s = statuses(report)
    assert s["getIncident"] == pc.STATUS_ALLOWED
    assert s["/sites/{siteId}/topology"] == pc.STATUS_UNKNOWN
    assert s["closeIncident"] == pc.STATUS_UNKNOWN  # spec says PUT, tool says POST
    assert report.violations == 2


def test_missing_openapi_spec_is_invalid(project):
    write_policy(project, {"openapi": "docs/missing.yaml"})
    report = pc.build_report(project, "app")
    assert report.violations == 1
    assert report.results[0].status == pc.STATUS_INVALID


def test_malformed_policy_is_invalid(project):
    (project / "product-policy.yaml").write_text("- just\n- a list\n")
    report = pc.build_report(project, "app")
    assert report.violations == 1
    assert report.results[0].status == pc.STATUS_INVALID


def test_unreadable_declaration_is_a_violation(project):
    (project / "app" / "tools" / "dynamic.py").write_text("PRODUCT_CALLS = make()\n")
    write_policy(project, {"allowed_methods": ["GET", "POST"]})
    report = pc.build_report(project, "app")
    invalid = [r for r in report.results if r.status == pc.STATUS_INVALID]
    assert len(invalid) == 1 and invalid[0].call.tool == "dynamic.py"


# ---------------------------------------------------------------------------
# printing / driver
# ---------------------------------------------------------------------------


def test_run_policy_check_prints_table_and_returns_violations(project):
    write_policy(project, {"allowed_methods": ["GET"]})
    buf = io.StringIO()
    console = Console(file=buf, width=160, force_terminal=False, color_system=None)
    violations = pc.run_policy_check(project, "app", console=console)
    out = buf.getvalue()
    assert violations == 1
    assert "Product-policy check" in out
    assert "closeIncident" in out and "denied" in out
    assert "getIncident" in out and "allowed" in out
    assert "1 violation(s)" in out


def test_run_policy_check_without_tools_dir(tmp_path):
    buf = io.StringIO()
    console = Console(file=buf, width=120, force_terminal=False, color_system=None)
    assert pc.run_policy_check(tmp_path, "app", console=console) == 0
    assert "nothing to check" in buf.getvalue()
