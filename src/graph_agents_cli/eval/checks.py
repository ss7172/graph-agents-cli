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

"""Deterministic checks: the ``expect`` block of a case, evaluated in the CLI process.

Every check is a pure function returning ``(passed, reason)``; ``reason`` is
a short human sentence (empty when passed). ``run_checks`` applies the checks a
case declares in ``expect`` to one trace. No model, no network.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from graph_agents_cli.eval.dataset import SCOPE_ALL_TURNS

CHECK_DESCRIPTIONS: dict[str, str] = {
    "contains": (
        "Every listed substring appears in the response (case-insensitive; "
        "`case_insensitive: false` for exact case)."
    ),
    "not_contains": (
        "None of the listed substrings appears in the response (case-insensitive; "
        "`case_insensitive: false` for exact case)."
    ),
    "regex": (
        "The response matches the regular expression (re.search, DOTALL; `(?i)` ignores case)."
    ),
    "json_schema": "The final response parses as JSON and validates against the schema.",
    "tool_calls": "The listed tools were called (name + args_subset); `ordered` enforces order.",
    "no_tool_calls": "The agent made no tool calls.",
    "max_latency_ms": (
        "message.end latency_ms is at or below the limit (every turn's, with scope all_turns)."
    ),
    "max_tokens": (
        "usage.input_tokens + usage.output_tokens is at or below the limit "
        "(summed over the turns with scope all_turns)."
    ),
}

# Keys of `expect` that change how checks read the trace; they are not checks.
MODIFIER_DESCRIPTIONS: dict[str, str] = {
    "ordered": "tool_calls must appear in the listed order (default false).",
    "case_insensitive": "contains / not_contains ignore case (default true).",
    "scope": (
        "final_turn (default): checks read the final turn of a multi-turn case; all_turns: "
        "every turn's replies, tool calls, latency and tokens (json_schema always reads the "
        "final reply)."
    ),
}

CheckResult = tuple[bool, str]


def _short(text: str, limit: int = 80) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _replies(response: str | list[str]) -> list[str]:
    return [response] if isinstance(response, str) else list(response)


def _occurs(needle: str, haystacks: list[str], case_insensitive: bool) -> bool:
    if case_insensitive:
        folded = needle.casefold()
        return any(folded in h.casefold() for h in haystacks)
    return any(needle in h for h in haystacks)


def check_contains(
    expected: list[str], response: str | list[str], *, case_insensitive: bool = True
) -> CheckResult:
    """Every substring occurs in the response (or, given every turn's replies, in one of them)."""
    replies = _replies(response)
    missing = [s for s in expected if not _occurs(s, replies, case_insensitive)]
    if missing:
        subject = "response does not" if isinstance(response, str) else "no reply in any turn"
        verb = " contain " if isinstance(response, str) else " contains "
        mode = "" if case_insensitive else " (exact case)"
        return False, f"{subject}{verb}" + ", ".join(repr(s) for s in missing) + mode
    return True, ""


def check_not_contains(
    forbidden: list[str], response: str | list[str], *, case_insensitive: bool = True
) -> CheckResult:
    """No substring occurs in the response (or in any turn's reply)."""
    replies = _replies(response)
    present = [s for s in forbidden if _occurs(s, replies, case_insensitive)]
    if present:
        subject = "response contains" if isinstance(response, str) else "a turn's reply contains"
        mode = " (ignoring case)" if case_insensitive else ""
        return False, f"{subject} forbidden " + ", ".join(repr(s) for s in present) + mode
    return True, ""


def check_regex(pattern: str, response: str | list[str]) -> CheckResult:
    """``re.search`` matches the response (or at least one turn's reply)."""
    try:
        compiled = re.compile(pattern, re.DOTALL)
    except re.error as exc:
        return False, f"invalid regex {pattern!r}: {exc}"
    if not any(compiled.search(text) is not None for text in _replies(response)):
        subject = "response does not" if isinstance(response, str) else "no reply in any turn"
        verb = " match " if isinstance(response, str) else " matches "
        return False, f"{subject}{verb}/{pattern}/"
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


def check_max_latency_ms(
    limit: float, latency_ms: float | list[float | None] | None
) -> CheckResult:
    """Latency at or below ``limit``; given every turn's latency, each turn must be."""
    if isinstance(latency_ms, list):
        for index, value in enumerate(latency_ms, start=1):
            passed, reason = check_max_latency_ms(limit, value)
            if not passed:
                return False, f"turn {index}: {reason}"
        return True, ""
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


def check_max_tokens(
    limit: float, usage: dict[str, Any] | list[dict[str, Any] | None] | None
) -> CheckResult:
    """Tokens at or below ``limit``; given every turn's usage, their sum must be."""
    if isinstance(usage, list):
        totals = [total_tokens(u) for u in usage]
        if not totals or any(t is None for t in totals):
            return False, "a turn has no usage.input_tokens/output_tokens"
        total = sum(t for t in totals if t is not None)
        if total > limit:
            return False, f"{total} tokens over {len(totals)} turns exceed {limit:.0f}"
        return True, ""
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


def _all_turns(trace: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Every turn record of a multi-turn trace, or None for a single-turn one."""
    turns = trace.get("turns")
    if not isinstance(turns, list) or not turns:
        return None
    return [t if isinstance(t, dict) else {} for t in turns]


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def run_checks(expect: dict[str, Any], trace: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Apply the declared checks to ``trace``; returns ``{name: {passed, reason}}``.

    ``expect.scope`` picks what they read on a multi-turn case: the final turn
    (``final_turn``, the default; the trace's top-level fields) or every turn
    (``all_turns``: every reply, every tool call in order, each turn's latency,
    the summed tokens). ``json_schema`` always reads the final reply.

    A check that raises is reported as failed with the exception text, so a bad
    expectation never aborts grading.
    """
    final_response = _text(trace.get("response"))
    turns = _all_turns(trace) if expect.get("scope") == SCOPE_ALL_TURNS else None
    response: str | list[str]
    latency: Any
    usage: Any
    if turns is None:
        response = final_response
        tool_calls = trace.get("tool_calls") or []
        latency = trace.get("latency_ms")
        usage = trace.get("usage")
    else:
        response = [_text(t.get("response")) for t in turns]
        tool_calls = [c for t in turns for c in (t.get("tool_calls") or [])]
        latency = [t.get("latency_ms") for t in turns]
        usage = [t.get("usage") for t in turns]
    fold = bool(expect.get("case_insensitive", True))
    runners: dict[str, Callable[[], CheckResult]] = {
        "contains": lambda: check_contains(expect["contains"], response, case_insensitive=fold),
        "not_contains": lambda: check_not_contains(
            expect["not_contains"], response, case_insensitive=fold
        ),
        "regex": lambda: check_regex(expect["regex"], response),
        "json_schema": lambda: check_json_schema(expect["json_schema"], final_response),
        "tool_calls": lambda: check_tool_calls(
            expect["tool_calls"], tool_calls, ordered=bool(expect.get("ordered"))
        ),
        "no_tool_calls": lambda: check_no_tool_calls(tool_calls),
        "max_latency_ms": lambda: check_max_latency_ms(expect["max_latency_ms"], latency),
        "max_tokens": lambda: check_max_tokens(expect["max_tokens"], usage),
    }
    results: dict[str, dict[str, Any]] = {}
    for name in declared_checks(expect):
        try:
            passed, reason = runners[name]()
        except Exception as exc:  # a malformed expectation must not abort grading
            passed, reason = False, f"check raised {type(exc).__name__}: {_short(str(exc))}"
        results[name] = {"passed": bool(passed), "reason": reason}
    return results
