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

"""Eval dataset loading and validation (``tests/eval/datasets/*.json``).

Dataset shape::

    {"cases": [{"id": "greeting",
                "messages": [{"role": "user", "content": "hi"}],
                "approvals": [{"decision": "approve", "match": {"operation_id": "cancelOrder"}}],
                "expect": {...}, "judge": {"response_quality": {"threshold": 4}},
                "reference": "...", "context": "...", "metadata": {}}]}

``approvals`` tells ``eval generate`` how to decide each call the agent's
policy gates (an API's ``approval`` block): the first instruction whose
``match`` names the paused call decides it. A gate no instruction matches is
a case error: a case must say what a human would do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from graph_agents_cli._approvals import match_problem
from graph_agents_cli.eval._common import EvalConfigError, canonical_hash, load_json_file

EXPECT_KEYS: tuple[str, ...] = (
    "contains",
    "not_contains",
    "regex",
    "json_schema",
    "tool_calls",
    "ordered",
    "no_tool_calls",
    "max_latency_ms",
    "max_tokens",
    "case_insensitive",
    "scope",
    "approvals",
    "no_approvals",
)

# A case's `approvals` instructions: how generate decides a gated call.
APPROVAL_DECISIONS: tuple[str, ...] = ("approve", "reject")
APPROVAL_INSTRUCTION_KEYS: tuple[str, ...] = ("decision", "match")
# `expect.approvals` items: which gate the run hit, and how it ended.
APPROVAL_OUTCOMES: tuple[str, ...] = ("gated", "approved", "rejected")
APPROVAL_EXPECT_KEYS: tuple[str, ...] = ("match", "status")

# Which turns of a multi-turn case the expect checks read: the final turn (the
# reply being graded) or every turn. A single-turn case is the same either way.
SCOPE_FINAL_TURN = "final_turn"
SCOPE_ALL_TURNS = "all_turns"
EXPECT_SCOPES: tuple[str, ...] = (SCOPE_FINAL_TURN, SCOPE_ALL_TURNS)

EXPECT_DEFAULTS: dict[str, Any] = {
    "contains": [],
    "not_contains": [],
    "regex": None,
    "json_schema": None,
    "tool_calls": None,
    "ordered": False,
    "no_tool_calls": False,
    "max_latency_ms": None,
    "max_tokens": None,
    # contains / not_contains compare case-insensitively unless this is false:
    # a refusal check such as not_contains ["deleted"] must also catch "Deleted".
    "case_insensitive": True,
    "scope": SCOPE_FINAL_TURN,
    "approvals": None,
    "no_approvals": False,
}

MESSAGE_ROLES: tuple[str, ...] = ("user", "assistant", "system")


@dataclass
class EvalCase:
    id: str
    messages: list[dict[str, Any]]
    expect: dict[str, Any] = field(default_factory=lambda: dict(EXPECT_DEFAULTS))
    judge: dict[str, dict[str, Any]] = field(default_factory=dict)
    reference: str | None = None
    context: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    # How generate decides the gated calls this case reaches ({decision, match}).
    approvals: list[dict[str, Any]] = field(default_factory=list)

    def user_messages(self) -> list[str]:
        return [str(m.get("content", "")) for m in self.messages if m.get("role") == "user"]

    def conversation_text(self) -> str:
        return "\n".join(f"{m.get('role', '?')}: {m.get('content', '')}" for m in self.messages)


@dataclass
class Dataset:
    cases: list[EvalCase]
    hash: str
    sources: list[Path] = field(default_factory=list)

    @property
    def case_ids(self) -> list[str]:
        return [c.id for c in self.cases]

    def by_id(self) -> dict[str, EvalCase]:
        return {c.id: c for c in self.cases}


def _where(source: Path | None, index: int) -> str:
    return f"{source}: cases[{index}]" if source else f"cases[{index}]"


def parse_case(raw: Any, index: int = 0, source: Path | None = None) -> EvalCase:
    """Validate one raw case dict into an :class:`EvalCase`.

    Raises :class:`EvalConfigError` (exit 3) for a malformed case.
    """
    where = _where(source, index)
    if not isinstance(raw, dict):
        raise EvalConfigError(f"{where}: a case must be an object")
    case_id = raw.get("id")
    if not isinstance(case_id, str) or not case_id.strip():
        raise EvalConfigError(f"{where}: 'id' must be a non-empty string")

    messages = raw.get("messages")
    if not isinstance(messages, list) or not messages:
        raise EvalConfigError(f"{where} ({case_id}): 'messages' must be a non-empty list")
    for i, message in enumerate(messages):
        if not isinstance(message, dict):
            raise EvalConfigError(f"{where} ({case_id}): messages[{i}] must be an object")
        role = message.get("role")
        if role not in MESSAGE_ROLES:
            raise EvalConfigError(
                f"{where} ({case_id}): messages[{i}].role must be one of {', '.join(MESSAGE_ROLES)}"
            )
        if not isinstance(message.get("content"), str):
            raise EvalConfigError(f"{where} ({case_id}): messages[{i}].content must be a string")
    if not any(m.get("role") == "user" for m in messages):
        raise EvalConfigError(f"{where} ({case_id}): 'messages' must contain a user message")

    expect_raw = raw.get("expect")
    expect = dict(EXPECT_DEFAULTS)
    if expect_raw is not None:
        if not isinstance(expect_raw, dict):
            raise EvalConfigError(f"{where} ({case_id}): 'expect' must be an object")
        unknown = sorted(set(expect_raw) - set(EXPECT_KEYS))
        if unknown:
            raise EvalConfigError(
                f"{where} ({case_id}): unknown expect key(s) {', '.join(unknown)}; "
                f"known: {', '.join(EXPECT_KEYS)}"
            )
        for key, value in expect_raw.items():
            if value is not None:
                expect[key] = value
    _validate_expect(expect, f"{where} ({case_id})")

    judge_raw = raw.get("judge")
    judge: dict[str, dict[str, Any]] = {}
    if judge_raw is not None:
        if not isinstance(judge_raw, dict):
            raise EvalConfigError(f"{where} ({case_id}): 'judge' must be an object")
        for name, spec in judge_raw.items():
            if spec is None:
                spec = {}
            elif isinstance(spec, int | float) and not isinstance(spec, bool):
                spec = {"threshold": spec}
            if not isinstance(spec, dict):
                raise EvalConfigError(
                    f"{where} ({case_id}): judge.{name} must be an object like {{'threshold': 4}}"
                )
            threshold = spec.get("threshold")
            if threshold is not None and (
                isinstance(threshold, bool) or not isinstance(threshold, int | float)
            ):
                raise EvalConfigError(
                    f"{where} ({case_id}): judge.{name}.threshold must be a number"
                )
            judge[str(name)] = dict(spec)

    for text_key in ("reference", "context"):
        value = raw.get(text_key)
        if value is not None and not isinstance(value, str):
            raise EvalConfigError(f"{where} ({case_id}): '{text_key}' must be a string")

    metadata = raw.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise EvalConfigError(f"{where} ({case_id}): 'metadata' must be an object")

    return EvalCase(
        id=case_id,
        messages=messages,
        expect=expect,
        judge=judge,
        reference=raw.get("reference"),
        context=raw.get("context"),
        metadata=metadata,
        raw=raw,
        approvals=_parse_approval_instructions(raw.get("approvals"), f"{where} ({case_id})"),
    )


def _match_error(match: Any, where: str) -> None:
    problem = match_problem(match)
    if problem:
        raise EvalConfigError(f"{where}.match: {problem}")


def _parse_approval_instructions(value: Any, where: str) -> list[dict[str, Any]]:
    """A case's ``approvals``: ``[{"decision": "approve"|"reject", "match": {...}}]``."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise EvalConfigError(f"{where}: 'approvals' must be a list of {{decision, match}}")
    instructions = []
    for index, item in enumerate(value):
        at = f"{where}: approvals[{index}]"
        if not isinstance(item, dict):
            raise EvalConfigError(f"{at} must be an object with decision and match")
        unknown = sorted(set(item) - set(APPROVAL_INSTRUCTION_KEYS), key=str)
        if unknown:
            raise EvalConfigError(
                f"{at}: unknown key(s) {', '.join(map(str, unknown))} "
                f"(known: {', '.join(APPROVAL_INSTRUCTION_KEYS)})"
            )
        if item.get("decision") not in APPROVAL_DECISIONS:
            raise EvalConfigError(f"{at}.decision must be one of {', '.join(APPROVAL_DECISIONS)}")
        _match_error(item.get("match"), at)
        instructions.append({"decision": item["decision"], "match": dict(item["match"])})
    return instructions


def _validate_expect(expect: dict[str, Any], where: str) -> None:
    for key in ("contains", "not_contains"):
        value = expect[key]
        if isinstance(value, str):
            expect[key] = [value]
        elif not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise EvalConfigError(f"{where}: expect.{key} must be a list of strings")
    if expect["regex"] is not None and not isinstance(expect["regex"], str):
        raise EvalConfigError(f"{where}: expect.regex must be a string")
    if expect["json_schema"] is not None and not isinstance(expect["json_schema"], dict | bool):
        raise EvalConfigError(f"{where}: expect.json_schema must be a JSON schema object")
    tool_calls = expect["tool_calls"]
    if tool_calls is not None:
        if not isinstance(tool_calls, list):
            raise EvalConfigError(f"{where}: expect.tool_calls must be a list")
        for i, call in enumerate(tool_calls):
            if isinstance(call, str):
                tool_calls[i] = {"name": call}
                continue
            if not isinstance(call, dict) or not isinstance(call.get("name"), str):
                raise EvalConfigError(
                    f"{where}: expect.tool_calls[{i}] must be {{'name': ..., 'args_subset': {{...}}}}"
                )
            if call.get("args_subset") is not None and not isinstance(call["args_subset"], dict):
                raise EvalConfigError(
                    f"{where}: expect.tool_calls[{i}].args_subset must be an object"
                )
    for key in ("ordered", "no_tool_calls", "case_insensitive"):
        if not isinstance(expect[key], bool):
            raise EvalConfigError(f"{where}: expect.{key} must be true or false")
    if expect["scope"] not in EXPECT_SCOPES:
        raise EvalConfigError(
            f"{where}: expect.scope must be one of {', '.join(EXPECT_SCOPES)} "
            f"(got {expect['scope']!r})"
        )
    for key in ("max_latency_ms", "max_tokens"):
        value = expect[key]
        if value is not None and (isinstance(value, bool) or not isinstance(value, int | float)):
            raise EvalConfigError(f"{where}: expect.{key} must be a number")
    if expect["no_tool_calls"] and expect["tool_calls"]:
        raise EvalConfigError(f"{where}: expect.no_tool_calls and expect.tool_calls conflict")
    if not isinstance(expect["no_approvals"], bool):
        raise EvalConfigError(f"{where}: expect.no_approvals must be true or false")
    approvals = expect["approvals"]
    if approvals is not None:
        if not isinstance(approvals, list):
            raise EvalConfigError(f"{where}: expect.approvals must be a list of {{match, status}}")
        if not approvals:
            # An empty list would check nothing and always pass.
            raise EvalConfigError(
                f"{where}: expect.approvals is empty (it would check nothing); list the gated "
                "calls, or use expect.no_approvals: true"
            )
        for i, item in enumerate(approvals):
            at = f"{where}: expect.approvals[{i}]"
            if not isinstance(item, dict):
                raise EvalConfigError(f"{at} must be an object with match (and status)")
            unknown = sorted(set(item) - set(APPROVAL_EXPECT_KEYS), key=str)
            if unknown:
                raise EvalConfigError(
                    f"{at}: unknown key(s) {', '.join(map(str, unknown))} "
                    f"(known: {', '.join(APPROVAL_EXPECT_KEYS)})"
                )
            if item.get("status", "gated") not in APPROVAL_OUTCOMES:
                raise EvalConfigError(f"{at}.status must be one of {', '.join(APPROVAL_OUTCOMES)}")
            _match_error(item.get("match"), at)
    if expect["no_approvals"] and expect["approvals"]:
        raise EvalConfigError(f"{where}: expect.no_approvals and expect.approvals conflict")


def dataset_hash(raw_cases: list[dict[str, Any]]) -> str:
    """The dataset hash recorded in traces and results: over the canonical cases list."""
    return canonical_hash({"cases": raw_cases})


def load_dataset(paths: list[Path]) -> Dataset:
    """Load and merge one or more dataset files; ids must be unique across them."""
    if not paths:
        raise EvalConfigError("no dataset files given")
    cases: list[EvalCase] = []
    raw_cases: list[dict[str, Any]] = []
    seen: dict[str, Path] = {}
    for path in paths:
        data = load_json_file(path, "dataset")
        if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
            raise EvalConfigError(f"dataset {path} must be an object with a 'cases' list")
        if not data["cases"]:
            raise EvalConfigError(f"dataset {path} has no cases")
        for index, raw in enumerate(data["cases"]):
            case = parse_case(raw, index, path)
            if case.id in seen:
                raise EvalConfigError(
                    f"duplicate case id {case.id!r} in {path} (first seen in {seen[case.id]})"
                )
            seen[case.id] = path
            cases.append(case)
            raw_cases.append(raw)
    return Dataset(cases=cases, hash=dataset_hash(raw_cases), sources=list(paths))


def cases_from_raw(raw_cases: list[dict[str, Any]]) -> Dataset:
    """Build a :class:`Dataset` from case dicts embedded in a trace file."""
    cases = [parse_case(raw, i) for i, raw in enumerate(raw_cases)]
    return Dataset(cases=cases, hash=dataset_hash(raw_cases))
