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
        {"api": "orders", "method": "POST", "operation_id": "createOrder", "path": "/orders"},
        {"api": "orders", "method": "GET", "path": "/orders/{order_id}"},
    ]

The list is read with :mod:`ast` (``ast.literal_eval`` on the assigned value),
so the check never imports a tool module and therefore never loads a model SDK
or the API client. Each declared call must name an API declared in
``api-policy.yaml`` and be allowed by that API's rules. The file is validated
with the same strict schema, and calls are matched with the same rules, as the
runtime client of the scaffolded project (``graph_agents_cli._api_policy``
holds the shared copy). When an API sets ``openapi:``, every call must also
exist in that spec by ``operationId`` or by ``path`` + ``method``; a declared
``operation_id`` must be the one the spec gives that method and path (the id is
a label the tool chooses, so a typo or a relabelled call must not pass as
another operation), and a call declared by ``operation_id`` alone is judged
with the path the spec gives it (the client always sends one), so path denials
apply to it.

Fail closed: without a policy file every declared call is refused, as the
runtime would refuse it. A module that still declares the retired
``PRODUCT_CALLS`` is an error with a rename hint, and ``auth: forward`` is an
error under the ``langgraph-server`` runtime.

Every refused call carries a hint: the ``graph-agents-cli api`` command that
would allow it (a reviewed change to ``api-policy.yaml``), or what to change in
the tool. An allowed call that the API's ``approval`` block gates (``gated``,
the runtime's rule) carries its gate: the report says who must approve it
before it is sent. Approval never widens access: a refused call stays refused.

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

import click
import yaml
from rich.markup import escape
from rich.table import Table

from graph_agents_cli._api_policy import (
    ANY_METHOD,
    CALLS_NAME,
    HTTP_METHODS,
    LEGACY_CALLS_NAME,
    POLICY_FILENAME,
    ApiPolicyFileError,
    ApprovalGate,
    ExampleCall,
    approval_notes,
    denial_match,
    describe_gate,
    forward_runtime_problem,
    gated,
    load_policy_document,
    operation_matches,
    path_matches,
    path_template_problem,
    refusal_reason,
    summarize,
)
from graph_agents_cli._output import Console, print_table

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
    # For a refused call: the `graph-agents-cli api` command that would allow it,
    # or what to change in the tool.
    hint: str = ""
    # For a call the policy allows: the human approval it needs before it is
    # sent (the API's `approval` block), or None.
    gate: ApprovalGate | None = None

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
    # The policy file itself is unusable (schema, a spec it names, a declared
    # file that is missing): a configuration error, not a refused call.
    policy_invalid: bool = False

    @property
    def violations(self) -> int:
        return sum(1 for r in self.results if r.is_violation)

    @property
    def gated(self) -> int:
        """Declared calls the policy allows only after a human approves them."""
        return sum(1 for r in self.results if r.gate is not None)

    def invalid(self, where: str, reason: str) -> None:
        self.results.append(
            CheckResult(DeclaredCall(tool=where, method="-"), STATUS_INVALID, reason)
        )

    def invalid_policy(self, where: str, reason: str) -> None:
        self.invalid(where, reason)
        self.policy_invalid = True


class InvalidPolicyFile(click.ClickException):
    """``lint`` / ``api check``: the policy file is invalid (a configuration error, exit 3)."""

    exit_code = 3


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
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        # Unreadable is not "declares no calls": the check cannot vouch for it.
        return [], [f"{name}: cannot be read as UTF-8 Python source: {exc}"]

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


def _spec_ids(call: DeclaredCall, by_id: Mapping[str, tuple[str, str]]) -> list[str]:
    """The operation ids the spec gives the call's method and path (none without a path)."""
    if not call.path:
        return []
    return sorted(
        op_id
        for op_id, (spec_path, spec_method) in by_id.items()
        if spec_method == call.method and path_matches(spec_path, call.path)
    )


def _spec_operation_hint(call: DeclaredCall, by_id: Mapping[str, tuple[str, str]]) -> str:
    """For a refused call declared without an operation id: the spec's id(s) for its path.

    The operation id is not filled in for the policy decision: the client
    judges a call by the ``operation_id`` the tool passes, so a check that
    assumed the spec's id would pass calls the client refuses.
    """
    ids = [] if call.operation_id else _spec_ids(call, by_id)
    if not ids:
        return ""
    return f" (the OpenAPI spec names this operation {' or '.join(ids)})"


def _spec_mismatch(
    call: DeclaredCall,
    by_id: Mapping[str, tuple[str, str]],
    pairs: set[tuple[str, str]],
) -> str | None:
    """Why the API's OpenAPI spec does not define the call as declared, or None.

    A declared ``operation_id`` must be one the spec defines, for the call's
    method and path. It is a label the tool chooses, and the allow-list can
    match on it: a typo, or a relabelled call, must not reach an endpoint
    under a name the spec gives another operation.
    """
    if call.operation_id:
        if call.operation_id not in by_id:
            ids = _spec_ids(call, by_id)
            named = f"; the spec names {call.method} {call.path} {' or '.join(ids)}" if ids else ""
            return f"operationId {call.operation_id} is not in the OpenAPI spec{named}"
        spec_path, spec_method = by_id[call.operation_id]
        if spec_method != call.method:
            return (
                f"operationId {call.operation_id} is {spec_method} {spec_path} in the spec, "
                f"not {call.method}"
            )
        if call.path and not path_matches(spec_path, call.path):
            return (
                f"operationId {call.operation_id} is {spec_method} {spec_path} in the spec, "
                f"not {call.path}"
            )
        return None
    if call.path and any(
        spec_method == call.method and path_matches(spec_path, call.path)
        for spec_path, spec_method in pairs
    ):
        return None
    return "not found in the OpenAPI spec"


def _spec_fix_hint(call: DeclaredCall, by_id: Mapping[str, tuple[str, str]]) -> str:
    """What to change in the tool when its declared operation id disagrees with the spec."""
    ids = _spec_ids(call, by_id)
    if len(ids) == 1:
        return (
            f'declare the call as the OpenAPI spec does: "operation_id": "{ids[0]}" for '
            f"{call.method} {call.path}, in {CALLS_NAME} and on the call"
        )
    return (
        f"declare an operation_id and path the OpenAPI spec defines, in {CALLS_NAME} and on "
        "the call"
    )


def check_call(
    call: DeclaredCall,
    document: Mapping[str, Any] | None,
    specs: Mapping[str, dict[str, Any]] | None = None,
    *,
    policy_file: str = POLICY_FILENAME,
) -> CheckResult:
    """Evaluate one declared call against the policy and (optionally) the API's spec.

    A call the policy refuses is ``denied`` (with the ``graph-agents-cli api``
    command that would allow it). With a spec, a call it does not define as
    declared is ``unknown`` (a declared ``operation_id`` must be the spec's
    one for that method and path); a call that is both says so, and its hint
    fixes the declaration first. A call the policy allows carries the
    approval its API requires before sending it (``gate``), if any.
    """
    if document is None:
        return CheckResult(
            call,
            STATUS_DENIED,
            f"no {policy_file}: outbound API calls are refused (fail closed)",
            add_hint(call.api),
        )
    api = document["apis"].get(call.api)
    if api is None:
        declared = ", ".join(sorted(document["apis"])) or "none"
        return CheckResult(
            call,
            STATUS_DENIED,
            f"API {call.api!r} is not declared in {policy_file} (declared: {declared})",
            add_hint(call.api),
        )
    spec = (specs or {}).get(call.api)
    by_id, pairs = _index_openapi(spec) if spec is not None else ({}, set())
    path = call.path
    if path is None and call.operation_id in by_id and by_id[call.operation_id][1] == call.method:
        # The client always sends a path: judge the one the spec gives the operation,
        # so path denials apply to a call declared by operation_id alone.
        path = by_id[call.operation_id][0]
    reason = refusal_reason(api, call.method, call.operation_id, path)
    mismatch = _spec_mismatch(call, by_id, pairs) if spec is not None else None
    # A declared operation_id the spec does not give this call: fix the declaration first.
    id_mismatch = mismatch is not None and call.operation_id is not None
    if reason:
        return CheckResult(
            call,
            STATUS_DENIED,
            reason
            + _spec_operation_hint(call, by_id)
            + (f"; also, {mismatch}" if id_mismatch else ""),
            _spec_fix_hint(call, by_id) if id_mismatch else refusal_hint(call, api, path),
        )
    # Only now, with the call allowed: approval never widens access.
    gate = gated(api, call.method, call.operation_id, path)
    if mismatch is not None:
        return CheckResult(
            call,
            STATUS_UNKNOWN,
            mismatch,
            _spec_fix_hint(call, by_id) if id_mismatch else "",
            gate=gate,
        )
    if spec is not None:
        if call.operation_id:
            spec_path, spec_method = by_id[call.operation_id]
            return CheckResult(call, STATUS_ALLOWED, f"spec: {spec_method} {spec_path}", gate=gate)
        spec_path = next(
            p for p, m in sorted(pairs) if m == call.method and path_matches(p, str(call.path))
        )
        return CheckResult(call, STATUS_ALLOWED, f"spec: {call.method} {spec_path}", gate=gate)
    return CheckResult(call, STATUS_ALLOWED, "", gate=gate)


# ---------------------------------------------------------------------------
# Hints: the `graph-agents-cli api` command that would allow a refused call
# ---------------------------------------------------------------------------

API_COMMAND = "graph-agents-cli api"


def add_hint(api_name: str) -> str:
    """How to declare an API the policy does not know (every choice is explicit)."""
    return (
        f"{API_COMMAND} add {api_name} --base-url-env <NAME>_API_BASE_URL "
        "--auth <none|bearer|forward> --access <read-only|read-write|custom>"
    )


def _allow_args(call: DeclaredCall) -> str:
    """``api allow`` arguments for an entry that covers exactly the declared call.

    The method is always pinned, and the path whenever the call names one, so
    the entry never allows the label on another method or path.
    """
    if call.operation_id and call.path:
        return f"{call.operation_id} --method {call.method} --path {call.path}"
    if call.operation_id:
        return f"{call.operation_id} --methods {call.method}"
    return f"--method {call.method} --path {call.path}"


def _allowed_methods(api: Mapping[str, Any]) -> list[str]:
    methods = [str(m).upper() for m in api.get("allowed_methods") or []]
    return list(HTTP_METHODS) if ANY_METHOD in methods else list(dict.fromkeys(methods))


def refusal_hint(call: DeclaredCall, api: Mapping[str, Any], path: str | None) -> str:
    """What would make ``api`` accept ``call``: the exact ``graph-agents-cli api`` commands.

    Each is a reviewed change to api-policy.yaml (CODEOWNERS covers it), and
    together they are every change the call needs: the method, the denial and
    the allow-list (an entry pinning the call's method, and its path when it
    names one). A call refused only because it leaves out what a denial knows
    the operation by is fixed in the tool instead.
    """
    steps: list[str] = []
    allowed = _allowed_methods(api)
    if call.method not in allowed:
        methods = ",".join(m for m in HTTP_METHODS if m in {*allowed, call.method})
        steps.append(f"{API_COMMAND} access {call.api} custom --methods {methods}")
    for entry in api.get("denied_operations") or []:
        unnamed = denial_match(entry, call.method, call.operation_id, path)
        if unnamed == "operation_id":
            return (
                f"name the operation: add operation_id to the call and to {CALLS_NAME} "
                "(a denial by operationId alone refuses calls that name none)"
            )
        if unnamed == "path":
            return (
                f'name the path: add "path" to the call\'s {CALLS_NAME} entry (a denial by path '
                "refuses declared calls that name none; the client always sends one)"
            )
        if unnamed is not None:
            revoke = (
                f"--method {call.method} --path {entry['path']}"
                if entry.get("operationId") is None and entry.get("path") is not None
                else str(entry.get("operationId"))
            )
            step = (
                f"{API_COMMAND} revoke {call.api} {revoke} --from denied (lifts a deliberate "
                "denial: make sure it should go)"
            )
            if step not in steps:
                steps.append(step)
    allowed_operations = api.get("allowed_operations")
    if allowed_operations is not None and not any(
        operation_matches(entry, call.method, call.operation_id, path)
        for entry in allowed_operations
    ):
        if call.operation_id or call.path:
            steps.append(f"{API_COMMAND} allow {call.api} {_allow_args(call)}")
    return "; then ".join(steps)


# ---------------------------------------------------------------------------
# The example tool's call
# ---------------------------------------------------------------------------

EXAMPLE_TOOL = "example_api.py"
# The last resort when the policy leaves every operation open: one per method,
# tried in the order of the API's allowed_methods.
DEFAULT_EXAMPLE_OPERATIONS = {
    "GET": ("getItem", "/items/{item_id}"),
    "HEAD": ("checkItem", "/items/{item_id}"),
    "POST": ("createItem", "/items"),
    "PUT": ("replaceItem", "/items/{item_id}"),
    "PATCH": ("updateItem", "/items/{item_id}"),
    "DELETE": ("deleteItem", "/items/{item_id}"),
    "OPTIONS": ("describeItems", "/items"),
}
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
        "body",
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
    params = ExampleCall(api="x", method="GET", path=path).params
    return all(_example_param_ok(name) for name in params)


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
) -> list[tuple[str, str | None, str]]:
    """``(METHOD, operation_id, path)`` candidates for the example, best first.

    Every method counts: the policy's own order decides (its entries first,
    each with its pinned methods or else ``allowed_methods`` in the order the
    file lists them).
    """
    operations = _spec_operations(spec) if spec is not None else []
    allowed = _allowed_methods(api)
    candidates: list[tuple[str, str | None, str]] = []
    for entry in api.get("allowed_operations") or []:
        pinned = [str(m).upper() for m in entry.get("methods") or []]
        for method in [m for m in (pinned or allowed) if m in allowed]:
            operation_id = entry.get("operationId")
            path = entry.get("path")
            if path is None:
                # An entry by operationId alone: only the spec knows its path.
                path = next(
                    (p for p, m, op_id in operations if op_id == operation_id and m == method),
                    None,
                )
            elif operation_id is None:
                # Name the operation when the spec does, so denials by operationId pass.
                operation_id = next(
                    (
                        op_id
                        for p, m, op_id in operations
                        if op_id and m == method and path_matches(p, path)
                    ),
                    None,
                )
            if path is not None:
                candidates.append((method, operation_id, path))
    candidates.extend((m, op_id, p) for p, m, op_id in operations if m in allowed)
    if spec is not None or not api.get("openapi"):
        # When the API names a spec that cannot be read here, lint would judge the
        # default against a spec this check never saw, so it is not offered.
        for method in allowed:
            operation_id, path = DEFAULT_EXAMPLE_OPERATIONS[method]
            candidates.append((method, operation_id, path))
    return list(dict.fromkeys(candidates))


def example_call(
    document: Mapping[str, Any], *, base_dir: Path | None = None
) -> ExampleCall | None:
    """The call the example tool makes on the policy's first API; None when it allows none.

    The first operation that API allows, whatever its method. Candidates, in
    order: each ``allowed_operations`` entry (with its pinned methods, else
    each of ``allowed_methods``; a missing path or operationId taken from the
    API's OpenAPI spec), each operation of that spec, then one generic
    operation per allowed method (``GET getItem /items/{item_id}``,
    ``POST createItem /items``, ...). The first one this check accepts wins
    (``check_call``: the runtime client's rules plus the spec), so the rendered
    example passes ``lint`` and the project's policy test. ``base_dir``
    resolves a relative ``openapi:`` path (the directory holding the policy
    file).
    """
    apis = document.get("apis") or {}
    if not apis:
        return None
    name, api = next(iter(apis.items()))
    spec = _load_example_spec(api, base_dir) if api.get("openapi") else None
    specs = {name: spec} if spec is not None else {}
    for method, operation_id, path in _example_candidates(api, spec):
        if not _example_renderable(operation_id, path):
            continue
        call = DeclaredCall(
            tool=EXAMPLE_TOOL, api=name, method=method, operation_id=operation_id, path=path
        )
        if check_call(call, document, specs).status == STATUS_ALLOWED:
            return ExampleCall(api=name, method=method, path=path, operation_id=operation_id)
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
                report.invalid_policy(policy_file, error)
            return report
        problem = forward_runtime_problem(summarize(document), runtime)
        if problem:
            report.invalid_policy(policy_file, problem)
        for name, api in document["apis"].items():
            report.notes.extend(approval_notes(name, api))
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
                report.invalid_policy(
                    policy_file, f"apis.{name}.openapi: cannot load {openapi_ref}: {exc}"
                )
    elif policy_declared:
        report.invalid_policy(
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
    # The Approval column (who approves a gated call) only when some call is gated.
    headers = ["Tool", "API", "Method", "Operation", "Status", "Reason"]
    if report.gated:
        headers.append("Approval")
    # fold: a narrow terminal wraps a long tool or path onto more lines, never cuts it.
    for header in headers:
        table.add_column(header, overflow="fold")
    styles = {
        STATUS_ALLOWED: "green",
        STATUS_DENIED: "red",
        STATUS_UNKNOWN: "yellow",
        STATUS_INVALID: "red",
    }
    for result in report.results:
        style = styles.get(result.status, "")
        row = [
            escape(result.call.tool),
            escape(result.call.api or "-"),
            escape(result.call.method),
            escape(result.call.operation),
            f"[{style}]{result.status}[/]" if style else result.status,
            escape(result.reason),
        ]
        if report.gated:
            row.append(escape(describe_gate(result.gate)) if result.gate else "-")
        table.add_row(*row)
    print_table(console, table)
    hints = list(dict.fromkeys(r.hint for r in report.results if r.is_violation and r.hint))
    if hints:
        console.print(
            "To fix a refused call, change the tool as shown, or change api-policy.yaml in a "
            "reviewed pull request (CODEOWNERS covers it), for example:"
        )
        for hint in hints:
            console.print(f"  {escape(hint)}", style="cyan", highlight=False)
    if report.gated:
        console.print(
            f"[yellow]{report.gated} declared call(s) wait for a human approval before they are "
            "sent (the Approval column: who approves, and why).[/]"
        )
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
    """Run the check, print the table, and return the number of violations.

    An invalid policy file raises :class:`InvalidPolicyFile` (exit 3) after the
    table: like an invalid manifest, it is a configuration error, and the
    ``api`` commands refuse the same file with the same code.
    """
    report = build_report(
        project_root,
        agent_dir,
        policy_file=policy_file,
        runtime=runtime,
        policy_declared=policy_declared,
    )
    print_report(report, console)
    if report.policy_invalid:
        raise InvalidPolicyFile(
            f"API policy check failed: {policy_file} is invalid (see above); fix it first "
            "(a configuration error)."
        )
    return report.violations
