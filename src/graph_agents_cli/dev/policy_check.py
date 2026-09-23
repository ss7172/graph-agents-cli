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

Every tool module under ``<agent_dir>/tools/`` (subpackages included) declares, at module level,
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
exist in that spec by ``operationId`` or by ``path`` + ``method``, and a call
declared by ``operation_id`` alone is judged with the path the spec gives it
(the client always sends one), so path denials apply to it.

Fail closed: without a policy file every declared call is refused, as the
runtime would refuse it. A module that still declares the retired
``PRODUCT_CALLS`` is an error with a rename hint, and ``auth: forward`` is an
error under the ``langgraph-server`` runtime.

``example_call`` uses the same judgement to pick the call that ``create``
renders into the example tool, so a fresh project passes this check.
"""

from __future__ import annotations

import ast
import json
import keyword
import os
import re
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
    ExampleCall,
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


# Methods that change a list or a dict in place.
_MUTATING_METHODS = frozenset(
    {
        "append",
        "clear",
        "extend",
        "insert",
        "pop",
        "popitem",
        "remove",
        "reverse",
        "setdefault",
        "sort",
        "update",
        "__delitem__",
        "__iadd__",
        "__setitem__",
    }
)


def _root_name(node: ast.AST) -> str | None:
    """``API_CALLS`` for ``API_CALLS``, ``API_CALLS[0]["path"]`` or ``API_CALLS.append``."""
    while isinstance(node, ast.Attribute | ast.Subscript):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _is_declaration(node: ast.stmt) -> bool:
    """A plain ``API_CALLS = ...`` or ``API_CALLS: ... = ...`` (one target, a value)."""
    if isinstance(node, ast.Assign):
        target = node.targets[0] if len(node.targets) == 1 else None
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
        target = node.target
    else:
        return False
    return isinstance(target, ast.Name) and target.id == CALLS_NAME


def _unread_changes(tree: ast.Module, declaration: ast.stmt | None) -> list[int]:
    """Lines outside ``declaration`` that bind or change ``API_CALLS``, in order.

    The check reads one literal. ``+=``, ``.append()``, an item assignment, a
    second or conditional assignment, an import or a ``def`` of the name could
    change the calls the tool makes without the check seeing them, so each is
    reported instead of trusted. (Changes through another name, such as an
    alias or ``globals()``, are out of reach of a static check; the runtime
    client still refuses every call outside the policy.)
    """
    skip: set[int] = set()  # Name nodes that are not a change: the declaration's target
    if isinstance(declaration, ast.Assign):
        skip.add(id(declaration.targets[0]))
    elif isinstance(declaration, ast.AnnAssign):
        skip.add(id(declaration.target))
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and node.value is None:
            skip.add(id(node.target))  # an annotation alone binds nothing
    for node in ast.walk(tree):
        changed = False
        if isinstance(node, ast.Name):
            changed = (
                node.id == CALLS_NAME
                and isinstance(node.ctx, ast.Store | ast.Del)
                and id(node) not in skip
            )
        elif isinstance(node, ast.Attribute | ast.Subscript):
            changed = isinstance(node.ctx, ast.Store | ast.Del) and _root_name(node) == CALLS_NAME
        elif isinstance(node, ast.Call):
            func = node.func
            changed = (
                isinstance(func, ast.Attribute)
                and func.attr in _MUTATING_METHODS
                and _root_name(func.value) == CALLS_NAME
            )
        elif isinstance(node, ast.alias):
            changed = (node.asname or node.name.split(".")[0]) == CALLS_NAME
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            changed = node.name == CALLS_NAME
        elif isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar):
            changed = node.name == CALLS_NAME
        elif isinstance(node, ast.MatchMapping):
            changed = node.rest == CALLS_NAME
        if changed:
            lines.add(getattr(node, "lineno", 0))
    return sorted(lines)


def read_api_calls(
    tool_path: Path, label: str | None = None
) -> tuple[list[DeclaredCall], list[str]]:
    """Return the ``API_CALLS`` declared in ``tool_path`` plus any problems.

    ``label`` names the module in the report (default: the file name); the
    tools directory walk passes the path relative to ``tools/``.

    A tool without the name declares no calls. The declaration must be one
    module-level assignment of a literal list of dicts: anything the check
    cannot read (a non-literal value, ``+=``, ``.append()``, a second or
    conditional assignment) is reported as a problem rather than trusted, as
    are a malformed entry and a leftover ``PRODUCT_CALLS``.
    """
    name = label or tool_path.name
    problems: list[str] = []
    try:
        tree = ast.parse(tool_path.read_text(encoding="utf-8"), filename=str(tool_path))
    except SyntaxError as exc:
        return [], [f"{name}: syntax error: {exc}"]

    calls: list[DeclaredCall] = []
    declaration: ast.stmt | None = None
    for node in tree.body:
        names, value = _assigned_names(node)
        if LEGACY_CALLS_NAME in names:
            problems.append(
                f"{name}: {LEGACY_CALLS_NAME} was renamed to {CALLS_NAME}; rename it "
                'and add "api": "<name of an API in api-policy.yaml>" to every entry'
            )
            continue
        if declaration is not None or not _is_declaration(node) or value is None:
            continue
        declaration = node
        literal = _literal(value)
        if not isinstance(literal, list | tuple):
            problems.append(
                f"{name}: {CALLS_NAME} is not a literal list; "
                "declare calls as plain dict literals so the check can read them"
            )
            continue
        for index, entry in enumerate(literal):
            problem = _entry_problem(entry)
            if problem:
                problems.append(f"{name}: {CALLS_NAME}[{index}] {problem}")
                continue
            calls.append(
                DeclaredCall(
                    tool=name,
                    api=str(entry["api"]),
                    method=str(entry["method"]).upper(),
                    operation_id=entry.get("operation_id") or None,
                    path=entry.get("path") or None,
                )
            )
    for line in _unread_changes(tree, declaration):
        problems.append(
            f"{name}: line {line} binds or changes {CALLS_NAME} outside its "
            "module-level literal (for example +=, .append() or an assignment inside a "
            "block), so the check cannot read those calls; declare every call in the "
            "one literal list"
        )
    return calls, problems


def collect_declared_calls(tools_dir: Path) -> tuple[list[DeclaredCall], list[str]]:
    """Read every ``*.py`` under ``tools_dir``, subpackages included, except its ``__init__.py``.

    The walk covers at least what ``tools.get_tools()`` imports (every module and
    subpackage of the package, ``_``-prefixed ones too) plus the modules those
    subpackages hold, so no module that can make calls escapes the check. A
    subpackage's own ``__init__.py`` is read; the top-level one is the registry.
    Cache and hidden directories are skipped.
    """
    calls: list[DeclaredCall] = []
    problems: list[str] = []
    if not tools_dir.is_dir():
        return calls, problems
    for relative in _tool_module_paths(tools_dir):
        found, found_problems = read_api_calls(tools_dir / relative, relative)
        calls.extend(found)
        problems.extend(found_problems)
    return calls, problems


def _tool_module_paths(tools_dir: Path) -> list[str]:
    """``*.py`` paths under ``tools_dir`` relative to it, sorted, top ``__init__.py`` excluded.

    Symlinked directories are followed (the import system follows them too),
    each real directory once, so a link loop cannot hang the walk.
    """
    found: list[str] = []
    seen: set[str] = set()
    for root, dirs, files in os.walk(tools_dir, followlinks=True):
        real = os.path.realpath(root)
        if real in seen:
            dirs[:] = []
            continue
        seen.add(real)
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "__pycache__")
        base = Path(root).relative_to(tools_dir)
        for filename in files:
            if not filename.endswith(".py"):
                continue
            relative = (base / filename).as_posix()
            if relative != "__init__.py" and (Path(root) / filename).is_file():
                found.append(relative)
    return sorted(found)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _spec_operations(spec: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    """Every operation of an OpenAPI spec as ``(path, METHOD, operationId or None)``, in order."""
    operations: list[tuple[str, str, str | None]] = []
    paths = spec.get("paths") or {}
    if not isinstance(paths, dict):
        return operations
    http_methods = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if not isinstance(method, str) or method.lower() not in http_methods:
                continue
            if not isinstance(op, dict):
                continue
            op_id = op.get("operationId")
            operations.append((str(path), method.upper(), str(op_id) if op_id else None))
    return operations


def _index_openapi(spec: dict[str, Any]) -> tuple[dict[str, tuple[str, str]], set[tuple[str, str]]]:
    """Map operationId -> (path, METHOD) and the set of (path, METHOD) pairs."""
    by_id: dict[str, tuple[str, str]] = {}
    pairs: set[tuple[str, str]] = set()
    for path, method, op_id in _spec_operations(spec):
        pairs.add((path, method))
        if op_id:
            by_id[op_id] = (path, method)
    return by_id, pairs


def _spec_operation_hint(call: DeclaredCall, by_id: Mapping[str, tuple[str, str]]) -> str:
    """For a refused call declared without an operation id: the spec's id(s) for its path.

    The operation id is not filled in for the policy decision: the client
    judges a call by the ``operation_id`` the tool passes, so a check that
    assumed the spec's id would pass calls the client refuses.
    """
    if call.operation_id or not call.path:
        return ""
    ids = sorted(
        op_id
        for op_id, (spec_path, spec_method) in by_id.items()
        if spec_method == call.method and path_matches(spec_path, call.path)
    )
    if not ids:
        return ""
    return f" (the OpenAPI spec names this operation {' or '.join(ids)})"


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
    spec = (specs or {}).get(call.api)
    by_id, pairs = _index_openapi(spec) if spec is not None else ({}, set())
    path = call.path
    if path is None and call.operation_id in by_id and by_id[call.operation_id][1] == call.method:
        # The client always sends a path: judge the one the spec gives the operation,
        # so path denials apply to a call declared by operation_id alone.
        path = by_id[call.operation_id][0]
    reason = refusal_reason(api, call.method, call.operation_id, path)
    if reason:
        return CheckResult(call, STATUS_DENIED, reason + _spec_operation_hint(call, by_id))

    if spec is not None:
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
# The example tool's call
# ---------------------------------------------------------------------------

EXAMPLE_TOOL = "example_api.py"
# The last resort when the policy leaves every operation open.
DEFAULT_EXAMPLE_OPERATION = ("getItem", "/items/{item_id}")
# The example is rendered into Python source: operation ids and paths outside
# these character sets are skipped rather than escaped.
_EXAMPLE_OPERATION_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,99}$")
_EXAMPLE_PATH_RE = re.compile(r"^/[A-Za-z0-9_.~{}/-]{0,199}$")
# Each path placeholder becomes a parameter of the example tool, so it must not
# shadow a name the tool's body uses, or one LangChain or pydantic treat specially.
_EXAMPLE_RESERVED_PARAMS = frozenset(
    {
        "Any",
        "ToolRuntime",
        "callbacks",
        "client",
        "config",
        "construct",
        "context",
        "copy",
        "data",
        "dict",
        "fields",
        "get_client",
        "getattr",
        "isinstance",
        "json",
        "run_manager",
        "runtime",
        "schema",
        "str",
        "tool",
        "validate",
    }
)


def _example_param_ok(name: str) -> bool:
    return (
        name.isidentifier()
        and not keyword.iskeyword(name)
        and not name.startswith(("_", "model_"))
        and name not in _EXAMPLE_RESERVED_PARAMS
    )


def _example_renderable(operation_id: str | None, path: str) -> bool:
    if operation_id is not None and not _EXAMPLE_OPERATION_ID_RE.match(operation_id):
        return False
    if not _EXAMPLE_PATH_RE.match(path) or path_template_problem(path) is not None:
        return False
    return all(_example_param_ok(name) for name in ExampleCall(api="x", path=path).params)


def _load_example_spec(api: Mapping[str, Any], base_dir: Path | None) -> dict[str, Any] | None:
    reference = api.get("openapi")
    path = Path(str(reference))
    if not path.is_absolute():
        if base_dir is None:
            return None
        path = base_dir / path
    try:
        return load_openapi(path)
    except (OSError, ValueError, yaml.YAMLError):
        return None


def _example_candidates(
    api: Mapping[str, Any], spec: dict[str, Any] | None
) -> list[tuple[str | None, str]]:
    """``(operation_id, path)`` GET candidates for the example, best first."""
    operations = _spec_operations(spec) if spec is not None else []
    spec_gets = [(path, op_id) for path, method, op_id in operations if method == "GET"]
    candidates: list[tuple[str | None, str]] = []
    for entry in api.get("allowed_operations") or []:
        methods = entry.get("methods")
        if methods and "GET" not in {str(m).upper() for m in methods}:
            continue
        operation_id = entry.get("operationId")
        path = entry.get("path")
        if path is None:
            # An entry by operationId alone: only the spec knows its path.
            path = next((p for p, op_id in spec_gets if op_id == operation_id), None)
        elif operation_id is None:
            # Name the operation when the spec does, so denials by operationId pass.
            operation_id = next(
                (op_id for p, op_id in spec_gets if op_id and path_matches(p, path)), None
            )
        if path is not None:
            candidates.append((operation_id, path))
    candidates.extend((op_id, path) for path, op_id in spec_gets)
    if spec is not None or not api.get("openapi"):
        # When the API names a spec that cannot be read here, lint would judge the
        # default against a spec this check never saw, so it is not offered.
        candidates.append(DEFAULT_EXAMPLE_OPERATION)
    return list(dict.fromkeys(candidates))


def example_call(
    document: Mapping[str, Any], *, base_dir: Path | None = None
) -> ExampleCall | None:
    """The GET the example tool makes on the policy's first API; None when it allows none.

    Candidates, in order: each ``allowed_operations`` entry that admits GET
    (a missing path or operationId taken from the API's OpenAPI spec), each GET
    of that spec, then ``GET getItem /items/{item_id}``. The first one this
    check accepts wins (``check_call``: the runtime client's rules plus the
    spec), so the rendered example passes ``lint`` and the project's policy
    test. ``base_dir`` resolves a relative ``openapi:`` path (the directory
    holding the policy file).
    """
    apis = document.get("apis") or {}
    if not apis:
        return None
    name, api = next(iter(apis.items()))
    spec = _load_example_spec(api, base_dir) if api.get("openapi") else None
    specs = {name: spec} if spec is not None else {}
    for operation_id, path in _example_candidates(api, spec):
        if not _example_renderable(operation_id, path):
            continue
        call = DeclaredCall(
            tool=EXAMPLE_TOOL, api=name, method="GET", operation_id=operation_id, path=path
        )
        if check_call(call, document, specs).status == STATUS_ALLOWED:
            return ExampleCall(api=name, path=path, operation_id=operation_id)
    return None


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
