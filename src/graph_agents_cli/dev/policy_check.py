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

"""Static product-policy check (DECISIONS D28, CONTRACTS section 7).

Every tool module under ``<agent_dir>/tools/*.py`` may declare, at module
level, the product API calls it makes::

    PRODUCT_CALLS = [
        {"method": "GET", "operation_id": "getIncident"},
        {"method": "GET", "path": "/sites/{siteId}/topology"},
    ]

The list is read with :mod:`ast` (``ast.literal_eval`` on the assigned
value), so the check never imports a tool module and therefore never loads a
model SDK or the product client. Each declared call must be allowed by
``product-policy.yaml`` (``allowed_methods``, ``allowed_operations``,
``denied_operations``; denials win) and, when the policy sets ``openapi:``,
must exist in that spec by ``operationId`` or by ``path`` + ``method``.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from rich.table import Table

from graph_agents_cli._output import Console

POLICY_FILENAME = "product-policy.yaml"
PRODUCT_CALLS_NAME = "PRODUCT_CALLS"
TOOLS_SUBDIR = "tools"

STATUS_ALLOWED = "allowed"
STATUS_DENIED = "denied"
STATUS_UNKNOWN = "unknown"
STATUS_INVALID = "invalid"
VIOLATION_STATUSES = frozenset({STATUS_DENIED, STATUS_UNKNOWN, STATUS_INVALID})


@dataclass(frozen=True)
class DeclaredCall:
    """One entry of a tool's ``PRODUCT_CALLS`` list."""

    tool: str
    method: str
    operation_id: str | None = None
    path: str | None = None

    @property
    def operation(self) -> str:
        if self.operation_id:
            return self.operation_id
        return self.path or "?"


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
    openapi_path: Path | None = None

    @property
    def violations(self) -> int:
        return sum(1 for r in self.results if r.is_violation)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_policy(path: Path) -> dict[str, Any]:
    """Load ``product-policy.yaml`` and return its ``product_api`` mapping.

    Accepts both the documented shape (top-level ``product_api:``) and a file
    whose keys are the policy fields directly.
    """
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    policy = data.get("product_api", data)
    if not isinstance(policy, dict):
        raise ValueError(f"{path}: product_api must be a mapping")
    return policy


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


def read_product_calls(tool_path: Path) -> tuple[list[DeclaredCall], list[str]]:
    """Return the ``PRODUCT_CALLS`` declared in ``tool_path`` plus any problems.

    A tool without the name declares no calls. A ``PRODUCT_CALLS`` that is not a
    literal list of dicts is reported as a problem (the check cannot vouch for
    it), as is an entry without a method or without both operation_id and path.
    """
    problems: list[str] = []
    try:
        tree = ast.parse(tool_path.read_text(encoding="utf-8"), filename=str(tool_path))
    except SyntaxError as exc:
        return [], [f"{tool_path.name}: syntax error: {exc}"]

    calls: list[DeclaredCall] = []
    for node in tree.body:
        targets: list[ast.expr]
        value: ast.expr | None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        if value is None or not any(
            isinstance(t, ast.Name) and t.id == PRODUCT_CALLS_NAME for t in targets
        ):
            continue
        literal = _literal(value)
        if not isinstance(literal, list | tuple):
            problems.append(
                f"{tool_path.name}: {PRODUCT_CALLS_NAME} is not a literal list; "
                "declare calls as plain dict literals so the check can read them"
            )
            continue
        for index, entry in enumerate(literal):
            if not isinstance(entry, dict):
                problems.append(f"{tool_path.name}: {PRODUCT_CALLS_NAME}[{index}] is not a dict")
                continue
            method = str(entry.get("method") or "").upper()
            operation_id = entry.get("operation_id") or entry.get("operationId")
            path = entry.get("path")
            if not method:
                problems.append(f"{tool_path.name}: {PRODUCT_CALLS_NAME}[{index}] has no method")
                continue
            if not operation_id and not path:
                problems.append(
                    f"{tool_path.name}: {PRODUCT_CALLS_NAME}[{index}] has neither "
                    "operation_id nor path"
                )
                continue
            calls.append(
                DeclaredCall(
                    tool=tool_path.name,
                    method=method,
                    operation_id=str(operation_id) if operation_id else None,
                    path=str(path) if path else None,
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
        found, found_problems = read_product_calls(tool_path)
        calls.extend(found)
        problems.extend(found_problems)
    return calls, problems


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _path_matches(template: str, path: str) -> bool:
    """Mirror of the template's ``product_client._path_matches``: ``{param}`` segments are wildcards.

    ``/sites/{siteId}/topology`` matches ``/sites/42/topology``,
    ``/sites/{site_id}/topology`` and itself, so lint and the runtime client
    agree on what a policy path covers. Kept in sync by hand (the template
    module is cookiecutter data and is never imported here).
    """
    if template == path:
        return True
    pattern = re.sub(
        r"\{[^/]+\}", r"[^/]+", re.escape(template).replace(r"\{", "{").replace(r"\}", "}")
    )
    return re.fullmatch(pattern, path.split("?", 1)[0]) is not None


def _entry_matches(entry: Any, call: DeclaredCall) -> bool:
    """Whether an ``allowed_operations``/``denied_operations`` entry covers ``call``.

    An entry that pins both ``operationId`` and ``path`` needs the id to match
    and, when the declaration carries a concrete path, that path to match the
    template too (the runtime client applies the same rule).
    """
    if isinstance(entry, str):
        return entry == call.operation_id or bool(call.path and _path_matches(entry, call.path))
    if not isinstance(entry, dict):
        return False
    methods = entry.get("methods")
    if methods and call.method not in {str(m).upper() for m in methods}:
        return False
    op_id = entry.get("operationId") or entry.get("operation_id")
    path = entry.get("path")
    id_ok = bool(op_id and call.operation_id and op_id == call.operation_id)
    path_ok = bool(path and call.path and _path_matches(str(path), call.path))
    if op_id and path:
        return id_ok and (call.path is None or path_ok)
    return id_ok or path_ok


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
    policy: dict[str, Any] | None,
    spec: dict[str, Any] | None = None,
) -> CheckResult:
    """Evaluate one declared call against the policy and (optionally) the spec."""
    if policy is None:
        return CheckResult(call, STATUS_ALLOWED, "no product-policy.yaml (unrestricted)")

    denied = policy.get("denied_operations") or []
    for entry in denied:
        if _entry_matches(entry, call):
            return CheckResult(call, STATUS_DENIED, "listed in denied_operations")

    allowed_methods = policy.get("allowed_methods") or []
    if allowed_methods:
        methods = {str(m).upper() for m in allowed_methods}
        if call.method not in methods:
            return CheckResult(
                call,
                STATUS_DENIED,
                f"method {call.method} not in allowed_methods {sorted(methods)}",
            )

    allowed_ops = policy.get("allowed_operations")
    if allowed_ops:
        if not any(_entry_matches(entry, call) for entry in allowed_ops):
            return CheckResult(call, STATUS_DENIED, "not listed in allowed_operations")

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
            return CheckResult(call, STATUS_ALLOWED, f"spec: {spec_method} {spec_path}")
        if call.path:
            for spec_path, spec_method in sorted(pairs):
                if spec_method == call.method and _path_matches(spec_path, call.path):
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
) -> PolicyReport:
    """Run the check for a project and return the report (nothing printed)."""
    report = PolicyReport()
    policy: dict[str, Any] | None = None
    spec: dict[str, Any] | None = None

    policy_path = project_root / policy_file
    if policy_path.is_file():
        report.policy_path = policy_path
        try:
            policy = load_policy(policy_path)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            report.notes.append(f"invalid {policy_file}: {exc}")
            report.results.append(
                CheckResult(DeclaredCall(tool=policy_file, method="-"), STATUS_INVALID, str(exc))
            )
            return report
        openapi_ref = policy.get("openapi")
        if openapi_ref:
            openapi_path = Path(openapi_ref)
            if not openapi_path.is_absolute():
                openapi_path = project_root / openapi_path
            report.openapi_path = openapi_path
            try:
                spec = load_openapi(openapi_path)
            except (OSError, ValueError, yaml.YAMLError) as exc:
                report.notes.append(f"cannot load OpenAPI spec {openapi_ref}: {exc}")
                report.results.append(
                    CheckResult(
                        DeclaredCall(tool=policy_file, method="-", path=str(openapi_ref)),
                        STATUS_INVALID,
                        f"openapi spec unreadable: {exc}",
                    )
                )
                return report
    else:
        report.notes.append(
            f"no {policy_file} at the project root: product API access is unrestricted"
        )

    tools_dir = project_root / agent_dir / TOOLS_SUBDIR
    calls, problems = collect_declared_calls(tools_dir)
    for problem in problems:
        tool_name = problem.split(":", 1)[0]
        report.results.append(
            CheckResult(DeclaredCall(tool=tool_name, method="-"), STATUS_INVALID, problem)
        )
    for call in calls:
        report.results.append(check_call(call, policy, spec))
    if not calls and not problems:
        report.notes.append(
            f"no {PRODUCT_CALLS_NAME} declarations under {agent_dir}/{TOOLS_SUBDIR}/"
        )
    return report


def print_report(report: PolicyReport, console: Console | None = None) -> None:
    console = console or Console()
    for note in report.notes:
        console.print(f"[dim]policy check: {note}[/]")
    if not report.results:
        console.print("[green]Product-policy check: nothing to check.[/]")
        return
    table = Table(title="Product-policy check", show_lines=False)
    table.add_column("Tool")
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
            result.call.tool,
            result.call.method,
            result.call.operation,
            f"[{style}]{result.status}[/]" if style else result.status,
            result.reason,
        )
    console.print(table)
    if report.violations:
        console.print(f"[red]{report.violations} violation(s).[/]")
    else:
        console.print("[green]All declared product API calls are allowed.[/]")


def run_policy_check(
    project_root: Path,
    agent_dir: str,
    *,
    policy_file: str = POLICY_FILENAME,
    console: Console | None = None,
) -> int:
    """Run the check, print the table, and return the number of violations."""
    report = build_report(project_root, agent_dir, policy_file=policy_file)
    print_report(report, console)
    return report.violations
