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

"""Unit tests for every deterministic check in graph_agents_cli.eval.checks."""

from __future__ import annotations

import pytest

from graph_agents_cli.eval import checks
from graph_agents_cli.eval.checks import (
    CHECK_DESCRIPTIONS,
    args_subset_matches,
    check_contains,
    check_json_schema,
    check_max_latency_ms,
    check_max_tokens,
    check_no_tool_calls,
    check_not_contains,
    check_regex,
    check_tool_calls,
    declared_checks,
    minimal_validate,
    run_checks,
)

CALLS = [
    {"name": "search", "args": {"query": "SF", "limit": 3}},
    {"name": "get_weather", "args": {"query": "SF", "units": {"temp": "c"}}},
    {"name": "search", "args": {"query": "LA"}},
]


def test_contains_and_not_contains() -> None:
    assert check_contains(["hello", "there"], "hello there") == (True, "")
    passed, reason = check_contains(["hello", "bye"], "hello there")
    assert not passed and "'bye'" in reason
    assert check_not_contains(["bye"], "hello there") == (True, "")
    passed, reason = check_not_contains(["hello"], "hello there")
    assert not passed and "forbidden" in reason


def test_regex() -> None:
    assert check_regex(r"^hel+o", "hello\nworld") == (True, "")
    assert check_regex(r"hello.*world", "hello\nworld")[0]  # DOTALL
    passed, reason = check_regex(r"\d{3}", "no digits")
    assert not passed and "does not match" in reason
    passed, reason = check_regex(r"(unclosed", "x")
    assert not passed and "invalid regex" in reason


def test_json_schema_with_minimal_validator() -> None:
    schema = {
        "type": "object",
        "required": ["city", "temp"],
        "properties": {
            "city": {"type": "string", "minLength": 1},
            "temp": {"type": "number", "minimum": -100},
            "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
        },
        "additionalProperties": False,
    }
    assert check_json_schema(schema, '{"city": "SF", "temp": 20.5}') == (True, "")
    assert check_json_schema(schema, 'Sure! ```json\n{"city": "SF", "temp": 1}\n```')[0]
    passed, reason = check_json_schema(schema, '{"city": "", "temp": "hot", "x": 1}')
    assert not passed and "schema violation" in reason
    passed, reason = check_json_schema(schema, "not json at all")
    assert not passed and "not valid JSON" in reason
    assert minimal_validate({"enum": [1, 2]}, 3)
    assert not minimal_validate({"anyOf": [{"type": "string"}, {"type": "null"}]}, None)
    assert minimal_validate({"oneOf": [{"type": "number"}, {"type": "integer"}]}, 1)
    assert minimal_validate({"type": ["string", "null"]}, 5)
    assert minimal_validate({"type": "integer"}, True)  # bool is not an integer


def test_args_subset_matches_is_recursive() -> None:
    assert args_subset_matches(None, {"a": 1})
    assert args_subset_matches({"query": "SF"}, {"query": "SF", "extra": 1})
    assert args_subset_matches({"units": {"temp": "c"}}, {"units": {"temp": "c", "wind": "kph"}})
    assert not args_subset_matches({"units": {"temp": "f"}}, {"units": {"temp": "c"}})
    assert not args_subset_matches({"missing": 1}, {"query": "SF"})
    assert not args_subset_matches({"query": "SF"}, None)


def test_tool_calls_unordered_matches_distinct_calls() -> None:
    expected = [{"name": "get_weather", "args_subset": {"query": "SF"}}, {"name": "search"}]
    assert check_tool_calls(expected, CALLS) == (True, "")
    # Two expected `search` calls need two distinct actual calls.
    assert check_tool_calls([{"name": "search"}, {"name": "search"}], CALLS)[0]
    passed, reason = check_tool_calls([{"name": "search"}] * 3, CALLS)
    assert not passed and "search" in reason
    passed, reason = check_tool_calls(
        [{"name": "get_weather", "args_subset": {"query": "LA"}}], CALLS
    )
    assert not passed and '"query": "LA"' in reason


def test_tool_calls_ordered_is_a_subsequence_match() -> None:
    assert check_tool_calls([{"name": "search"}, {"name": "get_weather"}], CALLS, ordered=True)[0]
    assert check_tool_calls([{"name": "search"}, {"name": "search"}], CALLS, ordered=True)[0]
    passed, reason = check_tool_calls(
        [{"name": "get_weather"}, {"name": "search", "args_subset": {"query": "SF"}}],
        CALLS,
        ordered=True,
    )
    assert not passed and "in order" in reason


def test_no_tool_calls_and_limits() -> None:
    assert check_no_tool_calls([]) == (True, "")
    passed, reason = check_no_tool_calls(CALLS)
    assert not passed and "search" in reason
    assert check_max_latency_ms(500, 499) == (True, "")
    assert not check_max_latency_ms(500, 501)[0]
    assert not check_max_latency_ms(500, None)[0]
    assert check_max_tokens(100, {"input_tokens": 40, "output_tokens": 60}) == (True, "")
    assert not check_max_tokens(99, {"input_tokens": 40, "output_tokens": 60})[0]
    assert check_max_tokens(10, {"total_tokens": 10})[0]
    assert not check_max_tokens(10, None)[0]


def test_run_checks_only_runs_declared_checks_and_never_raises() -> None:
    expect = {
        "contains": ["hello"],
        "not_contains": [],
        "regex": "(unclosed",
        "json_schema": None,
        "tool_calls": None,
        "ordered": False,
        "no_tool_calls": True,
        "max_latency_ms": 100,
        "max_tokens": None,
    }
    trace = {"response": "hello", "tool_calls": [], "latency_ms": 50, "usage": None}
    results = run_checks(expect, trace)
    assert set(results) == {"contains", "regex", "no_tool_calls", "max_latency_ms"}
    assert results["contains"]["passed"] and results["no_tool_calls"]["passed"]
    assert not results["regex"]["passed"]
    assert declared_checks({"contains": []}) == []


def test_run_checks_reports_a_raising_check_as_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(checks, "check_contains", boom)
    results = run_checks({"contains": ["x"]}, {"response": "x"})
    assert results["contains"]["passed"] is False
    assert "RuntimeError" in results["contains"]["reason"]


def test_every_check_has_a_description() -> None:
    assert set(CHECK_DESCRIPTIONS) == {
        "contains",
        "not_contains",
        "regex",
        "json_schema",
        "tool_calls",
        "no_tool_calls",
        "max_latency_ms",
        "max_tokens",
    }
