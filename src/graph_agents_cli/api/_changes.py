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

"""What an ``api`` command changes in the policy, as data (no files, no output).

Access is always an explicit choice: ``read-only`` and ``read-write`` are
shorthands for a list of methods that is written into the file in full (the
file never stores a preset name), and ``custom`` takes the list itself.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import yaml

from graph_agents_cli._api_policy import (
    ANY_METHOD,
    HTTP_METHODS,
    normalize_path,
    path_matches,
    path_template_problem,
)

READ_ONLY = "read-only"
READ_WRITE = "read-write"
CUSTOM = "custom"
ACCESS_CHOICES = (READ_ONLY, READ_WRITE, CUSTOM)
PRESETS: dict[str, tuple[str, ...]] = {
    READ_ONLY: ("GET", "HEAD"),
    READ_WRITE: ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"),
}
ALLOWED = "allowed_operations"
DENIED = "denied_operations"


class ApiCommandError(click.ClickException):
    """The change cannot be made: an invalid result or a configuration problem (exit 3)."""

    exit_code = 3


# ---------------------------------------------------------------------------
# Methods
# ---------------------------------------------------------------------------


def parse_methods(value: str | None) -> list[str]:
    """``get, Post`` -> ``["GET", "POST"]`` (canonical order); ``*`` alone -> ``["*"]``."""
    if value is None:
        return []
    items = [item.strip().upper() for item in value.split(",") if item.strip()]
    if not items:
        raise click.UsageError("--methods needs at least one HTTP method")
    if ANY_METHOD in items:
        if len(items) != 1:
            raise click.UsageError('--methods "*" must stand alone (every method)')
        return [ANY_METHOD]
    unknown = [item for item in items if item not in HTTP_METHODS]
    if unknown:
        raise click.UsageError(
            f"unknown HTTP method(s) {', '.join(unknown)} (allowed: {', '.join(HTTP_METHODS)}, "
            'or "*" alone)'
        )
    return [m for m in HTTP_METHODS if m in items]


def access_methods(access: str, methods: list[str]) -> list[str]:
    """The ``allowed_methods`` an access choice writes into the file."""
    if access == CUSTOM:
        if not methods:
            raise click.UsageError("custom access needs --methods M,... (the methods to allow)")
        return list(methods)
    if methods:
        raise click.UsageError(f"--methods goes with custom access only (not {access})")
    return list(PRESETS[access])


def preset_name(methods: Sequence[str]) -> str | None:
    """``read-only`` / ``read-write`` when ``methods`` is exactly that preset, else None."""
    normalized = [str(m).upper() for m in methods]
    for name, preset in PRESETS.items():
        if sorted(normalized) == sorted(preset):
            return name
    return None


def effective_methods(methods: Sequence[str]) -> list[str]:
    normalized = [str(m).upper() for m in methods]
    return list(HTTP_METHODS) if ANY_METHOD in normalized else normalized


def describe_methods(methods: Sequence[str]) -> str:
    normalized = [str(m).upper() for m in methods]
    if ANY_METHOD in normalized:
        return '"*" (every method)'
    text = ", ".join(normalized)
    preset = preset_name(normalized)
    return f"{text} (the {preset} preset)" if preset else text


# ---------------------------------------------------------------------------
# Operation entries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OperationRef:
    """How a command names an operation: an operationId, a method and a path, or all three."""

    operation_id: str | None = None
    method: str | None = None
    path: str | None = None

    @classmethod
    def from_options(
        cls,
        operation_id: str | None,
        method: str | None,
        path: str | None,
        *,
        combine: bool = False,
    ) -> OperationRef:
        """Read OPERATION_ID / ``--method`` / ``--path``.

        With ``combine`` (``allow``, ``deny``) OPERATION_ID may come with
        ``--method M --path P``, the endpoint it names: the entry then pins all
        three.
        """
        if operation_id and (method or path) and not combine:
            raise click.UsageError(
                "name the operation by OPERATION_ID or by --method/--path, not both"
            )
        if operation_id and any(c.isspace() for c in operation_id):
            raise click.UsageError("OPERATION_ID must not contain whitespace")
        if operation_id and not (method or path):
            return cls(operation_id=operation_id)
        if not (method and path):
            if operation_id:
                raise click.UsageError(
                    "with OPERATION_ID, give both --method and --path (the endpoint it names), "
                    "or neither"
                )
            raise click.UsageError("name the operation: OPERATION_ID, or --method M --path P")
        method = method.strip().upper()
        if method not in HTTP_METHODS:
            raise click.UsageError(
                f"unknown HTTP method {method} (allowed: {', '.join(HTTP_METHODS)})"
            )
        problem = path_template_problem(path)
        if problem:
            raise click.UsageError(f"--path {path}: {problem}")
        return cls(operation_id=operation_id or None, method=method, path=path)

    def describe(self) -> str:
        endpoint = f"{self.method} {self.path}" if self.path else ""
        return " ".join(part for part in (self.operation_id, endpoint) if part)

    def args(self) -> str:
        """The command-line form (for suggestions)."""
        endpoint = f"--method {self.method} --path {self.path}" if self.path else ""
        return " ".join(part for part in (self.operation_id, endpoint) if part)


def spec_operations(spec: Mapping[str, Any]) -> list[tuple[str, str, str | None]]:
    """``(path, METHOD, operationId)`` of every operation of an OpenAPI spec."""
    from graph_agents_cli.dev.policy_check import _spec_operations

    return _spec_operations(dict(spec))


def build_entry(
    ref: OperationRef,
    methods: list[str],
    spec: Mapping[str, Any] | None,
    spec_name: str,
) -> tuple[dict[str, Any], list[str]]:
    """The ``allowed_operations`` / ``denied_operations`` entry for ``ref``, and warnings.

    With an OpenAPI spec, an operationId must exist in it, and its method and
    path are filled in (all three must then match: AND semantics), so the entry
    is exact; a ``--method``/``--path`` given with it must agree with the
    spec. Without a spec, an operationId given with ``--method M --path P``
    pins all three.
    """
    warnings: list[str] = []
    if ANY_METHOD in methods:
        raise click.UsageError('an operation entry lists its methods; "*" is not allowed there')
    wanted = [m for m in HTTP_METHODS if m in {*methods, *([ref.method] if ref.method else [])}]
    if ref.operation_id:
        entry: dict[str, Any] = {"operationId": ref.operation_id}
        if spec is not None:
            found = [(p, m) for p, m, op_id in spec_operations(spec) if op_id == ref.operation_id]
            if not found:
                raise ApiCommandError(
                    f"operationId {ref.operation_id} is not in {spec_name} (the API's openapi "
                    "spec); check the id, or update the spec"
                )
            spec_path, spec_method = found[0]
            if wanted and wanted != [spec_method]:
                raise ApiCommandError(
                    f"{ref.operation_id} is {spec_method} {spec_path} in {spec_name}, not "
                    f"{', '.join(wanted)}"
                )
            if ref.path is not None and not path_matches(spec_path, ref.path):
                raise ApiCommandError(
                    f"{ref.operation_id} is {spec_method} {spec_path} in {spec_name}, not "
                    f"{ref.path}"
                )
            entry["path"] = spec_path
            entry["methods"] = [spec_method]
            return entry, warnings
        if ref.path is not None:
            entry["path"] = ref.path
        if wanted:
            entry["methods"] = wanted
        return entry, warnings
    assert ref.method is not None and ref.path is not None
    entry = {"path": ref.path, "methods": wanted}
    if spec is not None:
        known = {(p, m) for p, m, _ in spec_operations(spec)}
        for method in wanted:
            if not any(m == method and path_matches(p, ref.path) for p, m in known):
                warnings.append(f"{method} {ref.path} is not in {spec_name}")
    return entry, warnings


def _normalized(entry: Mapping[str, Any]) -> tuple[Any, ...]:
    path = entry.get("path")
    return (
        entry.get("operationId"),
        normalize_path(str(path)) if path is not None else None,
        tuple(sorted(str(m).upper() for m in entry.get("methods") or [])),
    )


def same_entry(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    return _normalized(a) == _normalized(b)


def describe_entry(entry: Mapping[str, Any]) -> str:
    parts = []
    if entry.get("operationId") is not None:
        parts.append(str(entry["operationId"]))
    methods = entry.get("methods")
    parts.append(",".join(str(m).upper() for m in methods) if methods else "any method")
    if entry.get("path") is not None:
        parts.append(str(entry["path"]))
    return " ".join(parts)


def matching_indexes(entries: Sequence[Mapping[str, Any]], ref: OperationRef) -> list[int]:
    """The entries ``ref`` names: by operationId, or by path covering the method."""
    found = []
    for index, entry in enumerate(entries):
        if ref.operation_id is not None:
            if entry.get("operationId") == ref.operation_id:
                found.append(index)
            continue
        path = entry.get("path")
        if path is None or normalize_path(str(path)) != normalize_path(str(ref.path)):
            continue
        methods = [str(m).upper() for m in entry.get("methods") or []]
        if not methods or ref.method in methods:
            found.append(index)
    return found


@dataclass
class Revocation:
    """One entry ``api revoke`` changes: removed, or only one of its methods removed."""

    index: int
    entry: dict[str, Any]
    remaining_methods: list[str] | None  # None: the whole entry goes

    def describe(self) -> str:
        if self.remaining_methods is None:
            return f"remove {describe_entry(self.entry)}"
        named = " ".join(
            str(self.entry[key]) for key in ("operationId", "path") if self.entry.get(key)
        )
        return f"keep {named} for {', '.join(self.remaining_methods)} only"


def plan_revocations(
    entries: Sequence[Mapping[str, Any]], ref: OperationRef, every_method: Sequence[str]
) -> list[Revocation]:
    """What revoking ``ref`` does to each matching entry, last entry first.

    ``--method M --path P`` takes M out of an entry and nothing else. An entry
    without ``methods`` covers every method, ``every_method`` (the API's
    allowed methods for an allowed entry, every HTTP method for a denial): it
    keeps the others, listed explicitly, so revoking GET from a denial of every
    method never lifts it for DELETE.
    """
    plans = []
    for index in reversed(matching_indexes(entries, ref)):
        entry = dict(entries[index])
        if ref.method is None:
            plans.append(Revocation(index, entry, None))
            continue
        methods = [str(m).upper() for m in entry.get("methods") or []] or list(every_method)
        remaining = [m for m in methods if m != ref.method]
        plans.append(Revocation(index, entry, remaining or None))
    return plans


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


def parse_limit(value: str | None, option: str) -> int | str | None:
    """``--max-calls-per-run 20`` -> 20, ``none`` -> None; not given -> ``"keep"``."""
    if value is None:
        return "keep"
    if value.strip().lower() == "none":
        return None
    try:
        number = int(value)
    except ValueError:
        raise click.UsageError(f"{option} takes an integer >= 1, or none") from None
    if number < 1:
        raise click.UsageError(f"{option} takes an integer >= 1, or none")
    return number


def new_limits(
    current: Mapping[str, Any] | None, max_calls: int | str | None, rate: int | str | None
) -> dict[str, int] | None:
    """The ``limits`` mapping after the change; None when no limit is left."""
    limits = dict(current or {})
    for key, value in (("max_calls_per_run", max_calls), ("rate_per_minute", rate)):
        if value == "keep":
            continue
        if value is None:
            limits.pop(key, None)
        else:
            limits[key] = value
    ordered = {k: limits[k] for k in ("max_calls_per_run", "rate_per_minute") if k in limits}
    return ordered or None


def describe_limits(limits: Mapping[str, Any] | None) -> str:
    if not limits:
        return "none"
    parts = []
    if "max_calls_per_run" in limits:
        parts.append(f"{limits['max_calls_per_run']} call(s) per run")
    if "rate_per_minute" in limits:
        parts.append(f"{limits['rate_per_minute']} per minute per replica")
    return ", ".join(parts)


def limits_widen(before: Mapping[str, Any] | None, after: Mapping[str, Any] | None) -> bool:
    """True when a limit is removed or raised."""
    before, after = before or {}, after or {}
    return any(key not in after or after[key] > before[key] for key in before)


# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------


def load_spec(root: Path, reference: str) -> dict[str, Any]:
    """The OpenAPI spec an API names (relative to the project root); exit 3 when unreadable."""
    from graph_agents_cli.dev.policy_check import load_openapi

    path = Path(reference)
    if not path.is_absolute():
        path = root / path
    try:
        return load_openapi(path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ApiCommandError(f"cannot read the OpenAPI spec {reference}: {exc}") from exc


def with_api(document: Mapping[str, Any] | None, name: str, api: Mapping[str, Any]) -> dict:
    new = copy.deepcopy(dict(document)) if document else {"apis": {}}
    new["apis"] = dict(new.get("apis") or {})
    new["apis"][name] = copy.deepcopy(dict(api))
    return new
