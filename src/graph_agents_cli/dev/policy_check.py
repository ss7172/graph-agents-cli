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

"""Static API-policy check run by ``graph-agents-cli lint``.

Every tool module under ``<agent_dir>/tools/*.py`` declares, at module level,
the external API calls it makes::

    API_CALLS = [
        {"api": "example", "method": "GET", "operation_id": "getItem"},
        {"api": "example", "method": "GET", "path": "/items/{item_id}"},
    ]

The list is read with :mod:`ast` (``ast.literal_eval`` on the assigned value),
so the check never imports a tool module and therefore never loads a model SDK
or the API client. Each declared call must name an API declared in
``api-policy.yaml`` and be allowed by that API's rules. The file is validated
with the same strict schema, and calls are matched with the same rules, as the
runtime client of the scaffolded project (``graph_agents_cli._api_policy``
holds the shared copy). When an API sets ``openapi:``, every call must also
exist in that spec by ``operationId`` or by ``path`` + ``method``.

Fail closed: without a policy file every declared call is refused, as the
runtime would refuse it. A module that still declares the retired
``PRODUCT_CALLS`` is an error with a rename hint, and ``auth: forward`` is an
error under the ``langgraph-server`` runtime.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from rich.markup import escape
from rich.table import Table

from graph_agents_cli._api_policy import (
    CALLS_NAME,
    HTTP_METHODS,
    LEGACY_CALLS_NAME,
    POLICY_FILENAME,
    ApiPolicyFileError,
    forward_runtime_problem,
    load_policy_document,
    path_matches,
    path_template_problem,
    refusal_reason,
    summarize,
)
from graph_agents_cli._output import Console

TOOLS_SUBDIR = "tools"
_CALL_KEYS = ("api", "method", "operation_id", "path")

STATUS_ALLOWED = "allowed"
STATUS_DENIED = "denied"
STATUS_UNKNOWN = "unknown"
STATUS_INVALID = "invalid"
VIOLATION_STATUSES = frozenset({STATUS_DENIED, STATUS_UNKNOWN, STATUS_INVALID})


@dataclass(frozen=True)
class DeclaredCall:
    """One entry of a tool's ``API_CALLS`` list (or a problem's location)."""

    tool: str
    method: str
    api: str = ""
    operation_id: str | None = None
    path: str | None = None

    @property
    def operation(self) -> str:
        if self.operation_id and self.path:
            return f"{self.operation_id} {self.path}"
        return self.operation_id or self.path or "-"


@dataclass(frozen=True)
class CheckResult:
    call: DeclaredCall
    status: str
    reason: str = ""

    @property
    def is_violation(self) -> bool:
        return self.status in VIOLATION_STATUSES


@dataclass
class PolicyReport:
    """Everything the check found, ready to print."""

    results: list[CheckResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    policy_path: Path | None = None
    openapi_paths: dict[str, Path] = field(default_factory=dict)

    @property
    def violations(self) -> int:
        return sum(1 for r in self.results if r.is_violation)

    def invalid(self, where: str, reason: str) -> None:
        self.results.append(
            CheckResult(DeclaredCall(tool=where, method="-"), STATUS_INVALID, reason)
        )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_openapi(path: Path) -> dict[str, Any]:
    """Load an OpenAPI document from YAML or JSON."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected an OpenAPI mapping")
    return data


def _literal(node: ast.AST) -> Any:
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        return None


def _assigned_names(node: ast.stmt) -> tuple[list[str], ast.expr | None]:
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)], node.value
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id], node.value
    return [], None


def _entry_problem(entry: Any) -> str | None:
    """Why an ``API_CALLS`` entry is malformed, or None."""
    if not isinstance(entry, dict):
        return "is not a dict"
    unknown = sorted(set(entry) - set(_CALL_KEYS), key=str)
    if unknown:
        return (
            f"has unknown key(s) {', '.join(map(repr, unknown))} (allowed: {', '.join(_CALL_KEYS)})"
        )
    if not isinstance(entry.get("api"), str) or not entry.get("api"):
        return 'has no "api" (the name of an API in api-policy.yaml)'
    method = entry.get("method")
    if not isinstance(method, str) or method.upper() not in HTTP_METHODS:
        return f"has no valid method (one of {', '.join(HTTP_METHODS)})"
    operation_id, path = entry.get("operation_id"), entry.get("path")
    if not operation_id and not path:
        return "has neither operation_id nor path"
    if operation_id is not None and not isinstance(operation_id, str):
        return "has a non-string operation_id"
    if path is not None:
        problem = path_template_problem(path)
        if problem:
            return f"path {problem}"
    return None


def read_api_calls(tool_path: Path) -> tuple[list[DeclaredCall], list[str]]:
    """Return the ``API_CALLS`` declared in ``tool_path`` plus any problems.

    A tool without the name declares no calls. An ``API_CALLS`` that is not a
    literal list of dicts is reported as a problem (the check cannot vouch for
    it), as is a malformed entry and a leftover ``PRODUCT_CALLS``.
    """
    problems: list[str] = []
    try:
        tree = ast.parse(tool_path.read_text(encoding="utf-8"), filename=str(tool_path))
    except SyntaxError as exc:
        return [], [f"{tool_path.name}: syntax error: {exc}"]

    calls: list[DeclaredCall] = []
    for node in tree.body:
        names, value = _assigned_names(node)
        if LEGACY_CALLS_NAME in names:
            problems.append(
                f"{tool_path.name}: {LEGACY_CALLS_NAME} was renamed to {CALLS_NAME}; rename it "
                'and add "api": "<name of an API in api-policy.yaml>" to every entry'
            )
            continue
        if CALLS_NAME not in names or value is None:
            continue
        literal = _literal(value)
        if not isinstance(literal, list | tuple):
            problems.append(
                f"{tool_path.name}: {CALLS_NAME} is not a literal list; "
                "declare calls as plain dict literals so the check can read them"
            )
            continue
        for index, entry in enumerate(literal):
            problem = _entry_problem(entry)
            if problem:
                problems.append(f"{tool_path.name}: {CALLS_NAME}[{index}] {problem}")
                continue
            calls.append(
                DeclaredCall(
                    tool=tool_path.name,
                    api=str(entry["api"]),
                    method=str(entry["method"]).upper(),
                    operation_id=entry.get("operation_id") or None,
                    path=entry.get("path") or None,
                )
            )
    return calls, problems


def collect_declared_calls(tools_dir: Path) -> tuple[list[DeclaredCall], list[str]]:
    """Read every ``*.py`` under ``tools_dir`` (non-recursive)."""
    calls: list[DeclaredCall] = []
    problems: list[str] = []
    if not tools_dir.is_dir():
        return calls, problems
    for tool_path in sorted(tools_dir.glob("*.py")):
        if tool_path.name.startswith("_"):
            continue
        found, found_problems = read_api_calls(tool_path)
        calls.extend(found)
        problems.extend(found_problems)
    return calls, problems


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _index_openapi(spec: dict[str, Any]) -> tuple[dict[str, tuple[str, str]], set[tuple[str, str]]]:
    """Map operationId -> (path, METHOD) and the set of (path, METHOD) pairs."""
    by_id: dict[str, tuple[str, str]] = {}
    pairs: set[tuple[str, str]] = set()
    paths = spec.get("paths") or {}
    if not isinstance(paths, dict):
        return by_id, pairs
    http_methods = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.lower() not in http_methods or not isinstance(op, dict):
                continue
            pairs.add((str(path), method.upper()))
            op_id = op.get("operationId")
            if op_id:
                by_id[str(op_id)] = (str(path), method.upper())
    return by_id, pairs


def check_call(
    call: DeclaredCall,
    document: Mapping[str, Any] | None,
    specs: Mapping[str, dict[str, Any]] | None = None,
    *,
    policy_file: str = POLICY_FILENAME,
) -> CheckResult:
    """Evaluate one declared call against the policy and (optionally) the API's spec."""
    if document is None:
        return CheckResult(
            call, STATUS_DENIED, f"no {policy_file}: outbound API calls are refused (fail closed)"
        )
    api = document["apis"].get(call.api)
    if api is None:
        declared = ", ".join(sorted(document["apis"])) or "none"
        return CheckResult(
            call,
            STATUS_DENIED,
            f"API {call.api!r} is not declared in {policy_file} (declared: {declared})",
        )
    reason = refusal_reason(api, call.method, call.operation_id, call.path)
    if reason:
        return CheckResult(call, STATUS_DENIED, reason)

    spec = (specs or {}).get(call.api)
    if spec is not None:
        by_id, pairs = _index_openapi(spec)
        if call.operation_id and call.operation_id in by_id:
            spec_path, spec_method = by_id[call.operation_id]
            if spec_method != call.method:
                return CheckResult(
                    call,
                    STATUS_UNKNOWN,
                    f"operationId {call.operation_id} is {spec_method} {spec_path} in the spec, "
                    f"not {call.method}",
                )
            if call.path and not path_matches(spec_path, call.path):
                return CheckResult(
                    call,
                    STATUS_UNKNOWN,
                    f"operationId {call.operation_id} is {spec_method} {spec_path} in the spec, "
                    f"not {call.path}",
                )
            return CheckResult(call, STATUS_ALLOWED, f"spec: {spec_method} {spec_path}")
        if call.path:
            for spec_path, spec_method in sorted(pairs):
                if spec_method == call.method and path_matches(spec_path, call.path):
                    return CheckResult(call, STATUS_ALLOWED, f"spec: {call.method} {spec_path}")
        return CheckResult(call, STATUS_UNKNOWN, "not found in the OpenAPI spec")

    return CheckResult(call, STATUS_ALLOWED, "")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def build_report(
    project_root: Path,
    agent_dir: str,
    *,
    policy_file: str = POLICY_FILENAME,
    runtime: str = "fastapi",
    policy_declared: bool = False,
) -> PolicyReport:
    """Run the check for a project and return the report (nothing printed).

    ``policy_declared`` is True when the manifest names the policy file: a
    missing file is then an error rather than a note.
    """
    report = PolicyReport()
    document: dict[str, Any] | None = None
    specs: dict[str, dict[str, Any]] = {}

    policy_path = project_root / policy_file
    if policy_path.is_file():
        report.policy_path = policy_path
        try:
            document = load_policy_document(policy_path)
        except ApiPolicyFileError as exc:
            for error in exc.errors:
                report.invalid(policy_file, error)
            return report
        problem = forward_runtime_problem(summarize(document), runtime)
        if problem:
            report.invalid(policy_file, problem)
        for name, api in document["apis"].items():
            openapi_ref = api.get("openapi")
            if not openapi_ref:
                continue
            openapi_path = Path(openapi_ref)
            if not openapi_path.is_absolute():
                openapi_path = project_root / openapi_path
            report.openapi_paths[name] = openapi_path
            try:
                specs[name] = load_openapi(openapi_path)
            except (OSError, ValueError, yaml.YAMLError) as exc:
                report.invalid(
                    policy_file, f"apis.{name}.openapi: cannot load {openapi_ref}: {exc}"
                )
    elif policy_declared:
        report.invalid(
            policy_file,
            f"the manifest declares {policy_file} but the file does not exist; every API call "
            "would be refused at runtime",
        )
    else:
        report.notes.append(f"no {policy_file} at the project root: outbound API calls are refused")

    tools_dir = project_root / agent_dir / TOOLS_SUBDIR
    calls, problems = collect_declared_calls(tools_dir)
    for problem in problems:
        report.invalid(problem.split(":", 1)[0], problem)
    for call in calls:
        report.results.append(check_call(call, document, specs, policy_file=policy_file))
    if not calls and not problems:
        report.notes.append(f"no {CALLS_NAME} declarations under {agent_dir}/{TOOLS_SUBDIR}/")
    return report


def print_report(report: PolicyReport, console: Console | None = None) -> None:
    console = console or Console()
    for note in report.notes:
        console.print(f"[dim]policy check: {escape(note)}[/]")
    if not report.results:
        console.print("[green]API policy check: nothing to check.[/]")
        return
    table = Table(title="API policy check", show_lines=False)
    table.add_column("Tool")
    table.add_column("API")
    table.add_column("Method")
    table.add_column("Operation")
    table.add_column("Status")
    table.add_column("Reason")
    styles = {
        STATUS_ALLOWED: "green",
        STATUS_DENIED: "red",
        STATUS_UNKNOWN: "yellow",
        STATUS_INVALID: "red",
    }
    for result in report.results:
        style = styles.get(result.status, "")
        table.add_row(
            escape(result.call.tool),
            escape(result.call.api or "-"),
            escape(result.call.method),
            escape(result.call.operation),
            f"[{style}]{result.status}[/]" if style else result.status,
            escape(result.reason),
        )
    console.print(table)
    if report.violations:
        console.print(f"[red]{report.violations} violation(s).[/]")
    else:
        console.print("[green]All declared API calls are allowed.[/]")


def run_policy_check(
    project_root: Path,
    agent_dir: str,
    *,
    policy_file: str = POLICY_FILENAME,
    runtime: str = "fastapi",
    policy_declared: bool = False,
    console: Console | None = None,
) -> int:
    """Run the check, print the table, and return the number of violations."""
    report = build_report(
        project_root,
        agent_dir,
        policy_file=policy_file,
        runtime=runtime,
        policy_declared=policy_declared,
    )
    print_report(report, console)
    return report.violations
