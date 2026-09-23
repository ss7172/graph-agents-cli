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

"""Deterministic checks (CONTRACTS.md section 11, DECISIONS.md D24).

Every check is a pure function returning ``(passed, reason)``; ``reason`` is
a short human sentence (empty when passed). ``run_checks`` applies the checks a
case declares in ``expect`` to one trace. No model, no network.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

CHECK_DESCRIPTIONS: dict[str, str] = {
    "contains": "Every listed substring appears in the response (case-sensitive).",
    "not_contains": "None of the listed substrings appears in the response.",
    "regex": "The response matches the regular expression (re.search, DOTALL).",
    "json_schema": "The response parses as JSON and validates against the schema.",
    "tool_calls": "The listed tools were called (name + args_subset); `ordered` enforces order.",
    "no_tool_calls": "The agent made no tool calls.",
    "max_latency_ms": "message.end latency_ms is at or below the limit.",
    "max_tokens": "usage.input_tokens + usage.output_tokens is at or below the limit.",
}

CheckResult = tuple[bool, str]


def _short(text: str, limit: int = 80) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def check_contains(expected: list[str], response: str) -> CheckResult:
    missing = [s for s in expected if s not in response]
    if missing:
        return False, "response does not contain " + ", ".join(repr(s) for s in missing)
    return True, ""


def check_not_contains(forbidden: list[str], response: str) -> CheckResult:
    present = [s for s in forbidden if s in response]
    if present:
        return False, "response contains forbidden " + ", ".join(repr(s) for s in present)
    return True, ""


def check_regex(pattern: str, response: str) -> CheckResult:
    try:
        compiled = re.compile(pattern, re.DOTALL)
    except re.error as exc:
        return False, f"invalid regex {pattern!r}: {exc}"
    if compiled.search(response) is None:
        return False, f"response does not match /{pattern}/"
    return True, ""


# --- json_schema -------------------------------------------------------------


def _type_matches(value: Any, type_name: str) -> bool:
    match type_name:
        case "object":
            return isinstance(value, dict)
        case "array":
            return isinstance(value, list)
        case "string":
            return isinstance(value, str)
        case "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        case "number":
            return isinstance(value, int | float) and not isinstance(value, bool)
        case "boolean":
            return isinstance(value, bool)
        case "null":
            return value is None
    return True


def minimal_validate(schema: Any, value: Any, path: str = "$") -> list[str]:
    """A small JSON-schema subset validator used when ``jsonschema`` is not installed.

    Supports: type (string or list), enum, const, properties, required,
    additionalProperties (bool), items, minItems/maxItems, minLength/maxLength,
    minimum/maximum, pattern, anyOf/oneOf/allOf, not. Returns error strings.
    """
    if schema is True or schema == {}:
        return []
    if schema is False:
        return [f"{path}: schema forbids any value"]
    if not isinstance(schema, dict):
        return [f"{path}: unsupported schema {schema!r}"]
    errors: list[str] = []

    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_type_matches(value, t) for t in types):
            errors.append(f"{path}: expected type {'/'.join(types)}, got {type(value).__name__}")
            return errors
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in enum {schema['enum']!r}")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}")

    if isinstance(value, dict):
        for key in schema.get("required", []) or []:
            if key not in value:
                errors.append(f"{path}: missing required property {key!r}")
        props = schema.get("properties", {}) or {}
        for key, sub in props.items():
            if key in value:
                errors.extend(minimal_validate(sub, value[key], f"{path}.{key}"))
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(props))
            if extra:
                errors.append(f"{path}: additional properties not allowed: {', '.join(extra)}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        items = schema.get("items")
        if isinstance(items, dict | bool):
            for i, item in enumerate(value):
                errors.extend(minimal_validate(items, item, f"{path}[{i}]"))
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']}")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            errors.append(f"{path}: does not match pattern {schema['pattern']!r}")
    if isinstance(value, int | float) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} > maximum {schema['maximum']}")

    if "allOf" in schema:
        for sub in schema["allOf"]:
            errors.extend(minimal_validate(sub, value, path))
    if "anyOf" in schema and not any(not minimal_validate(s, value, path) for s in schema["anyOf"]):
        errors.append(f"{path}: matches none of anyOf")
    if "oneOf" in schema:
        matches = sum(1 for s in schema["oneOf"] if not minimal_validate(s, value, path))
        if matches != 1:
            errors.append(f"{path}: matches {matches} of oneOf, expected exactly 1")
    if "not" in schema and not minimal_validate(schema["not"], value, path):
        errors.append(f"{path}: matches forbidden schema")
    return errors


def validate_json_schema(schema: Any, value: Any) -> list[str]:
    """Validate with ``jsonschema`` when installed, else the minimal validator."""
    try:
        import jsonschema
    except ImportError:
        return minimal_validate(schema, value)
    validator_cls = jsonschema.validators.validator_for(schema)
    validator = validator_cls(schema)
    return [
        f"$.{'.'.join(str(p) for p in err.absolute_path)}: {err.message}"
        if err.absolute_path
        else f"$: {err.message}"
        for err in sorted(validator.iter_errors(value), key=lambda e: list(e.absolute_path))
    ]


def _extract_json(response: str) -> Any:
    text = response.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        return json.loads(fence.group(1).strip())
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start >= 0:
        return json.loads(text[start:])
    raise json.JSONDecodeError("no JSON found", text, 0)


def check_json_schema(schema: Any, response: str) -> CheckResult:
    try:
        parsed = _extract_json(response)
    except json.JSONDecodeError as exc:
        return False, f"response is not valid JSON: {exc.msg}"
    errors = validate_json_schema(schema, parsed)
    if errors:
        return False, "schema violation: " + "; ".join(errors[:3])
    return True, ""


# --- tool calls -------------------------------------------------------------


def args_subset_matches(subset: dict[str, Any] | None, args: Any) -> bool:
    """True when every key in ``subset`` equals the same key in ``args`` (recursively)."""
    if not subset:
        return True
    if not isinstance(args, dict):
        return False
    for key, expected in subset.items():
        if key not in args:
            return False
        actual = args[key]
        if isinstance(expected, dict) and isinstance(actual, dict):
            if not args_subset_matches(expected, actual):
                return False
        elif expected != actual:
            return False
    return True


def _describe_call(call: dict[str, Any]) -> str:
    subset = call.get("args_subset")
    return f"{call.get('name')}({json.dumps(subset, sort_keys=True) if subset else ''})"


def check_tool_calls(
    expected: list[dict[str, Any]], actual: list[dict[str, Any]], *, ordered: bool = False
) -> CheckResult:
    """Every expected call matches a distinct actual call; ordered = as a subsequence."""
    actual_names = [str(c.get("name")) for c in actual]
    if ordered:
        position = 0
        for exp in expected:
            while position < len(actual):
                cand = actual[position]
                position += 1
                if cand.get("name") == exp.get("name") and args_subset_matches(
                    exp.get("args_subset"), cand.get("args")
                ):
                    break
            else:
                return False, (
                    f"expected tool call {_describe_call(exp)} not found in order; "
                    f"actual calls: {actual_names or 'none'}"
                )
        return True, ""

    remaining = list(actual)
    for exp in expected:
        for i, cand in enumerate(remaining):
            if cand.get("name") == exp.get("name") and args_subset_matches(
                exp.get("args_subset"), cand.get("args")
            ):
                del remaining[i]
                break
        else:
            return False, (
                f"expected tool call {_describe_call(exp)} not found; "
                f"actual calls: {actual_names or 'none'}"
            )
    return True, ""


def check_no_tool_calls(actual: list[dict[str, Any]]) -> CheckResult:
    if actual:
        return False, "unexpected tool calls: " + ", ".join(str(c.get("name")) for c in actual)
    return True, ""


def check_max_latency_ms(limit: float, latency_ms: float | None) -> CheckResult:
    if latency_ms is None:
        return False, "trace has no latency_ms"
    if latency_ms > limit:
        return False, f"latency {latency_ms:.0f} ms exceeds {limit:.0f} ms"
    return True, ""


def total_tokens(usage: dict[str, Any] | None) -> int | None:
    if not isinstance(usage, dict):
        return None
    if isinstance(usage.get("total_tokens"), int | float):
        return int(usage["total_tokens"])
    parts = [usage.get("input_tokens"), usage.get("output_tokens")]
    if all(isinstance(p, int | float) for p in parts):
        return int(parts[0]) + int(parts[1])
    return None


def check_max_tokens(limit: float, usage: dict[str, Any] | None) -> CheckResult:
    total = total_tokens(usage)
    if total is None:
        return False, "trace has no usage.input_tokens/output_tokens"
    if total > limit:
        return False, f"{total} tokens exceed {limit:.0f}"
    return True, ""


# --- driver -----------------------------------------------------------------


def declared_checks(expect: dict[str, Any]) -> list[str]:
    """The check names a case's ``expect`` block activates (``ordered`` is a modifier)."""
    names: list[str] = []
    if expect.get("contains"):
        names.append("contains")
    if expect.get("not_contains"):
        names.append("not_contains")
    if expect.get("regex") is not None:
        names.append("regex")
    if expect.get("json_schema") is not None:
        names.append("json_schema")
    if expect.get("tool_calls") is not None:
        names.append("tool_calls")
    if expect.get("no_tool_calls"):
        names.append("no_tool_calls")
    if expect.get("max_latency_ms") is not None:
        names.append("max_latency_ms")
    if expect.get("max_tokens") is not None:
        names.append("max_tokens")
    return names


def run_checks(expect: dict[str, Any], trace: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Apply the declared checks to ``trace``; returns ``{name: {passed, reason}}``.

    A check that raises is reported as failed with the exception text, so a bad
    expectation never aborts grading.
    """
    response = trace.get("response")
    response = "" if response is None else str(response)
    tool_calls = trace.get("tool_calls") or []
    runners: dict[str, Callable[[], CheckResult]] = {
        "contains": lambda: check_contains(expect["contains"], response),
        "not_contains": lambda: check_not_contains(expect["not_contains"], response),
        "regex": lambda: check_regex(expect["regex"], response),
        "json_schema": lambda: check_json_schema(expect["json_schema"], response),
        "tool_calls": lambda: check_tool_calls(
            expect["tool_calls"], tool_calls, ordered=bool(expect.get("ordered"))
        ),
        "no_tool_calls": lambda: check_no_tool_calls(tool_calls),
        "max_latency_ms": lambda: check_max_latency_ms(
            expect["max_latency_ms"], trace.get("latency_ms")
        ),
        "max_tokens": lambda: check_max_tokens(expect["max_tokens"], trace.get("usage")),
    }
    results: dict[str, dict[str, Any]] = {}
    for name in declared_checks(expect):
        try:
            passed, reason = runners[name]()
        except Exception as exc:  # a malformed expectation must not abort grading
            passed, reason = False, f"check raised {type(exc).__name__}: {_short(str(exc))}"
        results[name] = {"passed": bool(passed), "reason": reason}
    return results
