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

"""What a judge sees: every turn of a multi-turn case, and tool results cut only visibly."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from conftest import FakeJudge, make_trace, read_results, write_traces

from graph_agents_cli.eval import _paths, gate
from graph_agents_cli.eval._common import EvalConfigError, write_json_file
from graph_agents_cli.eval.cmd_grade import cmd_grade
from graph_agents_cli.eval.config import parse_eval_config
from graph_agents_cli.eval.dataset import dataset_hash, parse_case
from graph_agents_cli.eval.transcript import (
    DEFAULT_MAX_TOOL_RESULT_CHARS,
    cut_tool_result,
    render_case,
)

MULTI_TURN_CASE: dict[str, Any] = {
    "id": "follow-up",
    "messages": [
        {"role": "system", "content": "The user is a returning customer."},
        {"role": "user", "content": "What is the status of order 7?"},
        {"role": "user", "content": "And where does it ship?"},
    ],
    "judge": {"task_success": {"threshold": 4}, "groundedness": {"threshold": 4}},
}


def _turn(response: str, calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "status": "ok",
        "response": response,
        "tool_calls": calls or [],
        "usage": {"input_tokens": 3, "output_tokens": 4},
        "latency_ms": 10,
        "error": None,
    }


def _multi_turn_trace() -> dict[str, Any]:
    first = _turn(
        "UNIQUE_TURN1_MARKER: order 7 has shipped.",
        [
            {
                "id": "c1",
                "name": "get_order",
                "args": {"id": 7},
                "result": "TURN1_TOOL_RESULT status=shipped",
                "is_error": False,
            }
        ],
    )
    second = _turn(
        "It ships to 99 Lake Rd.",
        [
            {
                "id": "c2",
                "name": "get_address",
                "args": {"id": 7},
                "result": "99 Lake Rd",
                "is_error": False,
            }
        ],
    )
    trace = make_trace(
        "follow-up",
        response=second["response"],
        tool_calls=second["tool_calls"],
        case=MULTI_TURN_CASE,
    )
    trace["turns"] = [first, second]
    return trace


def _prompts(config_data: dict[str, Any], case: dict[str, Any], trace: dict[str, Any]):
    config = parse_eval_config(config_data)
    parsed = parse_case(case)
    grade = gate.grade_deterministic(parsed, trace)
    items = gate.plan_judge_items(config, parsed, trace, grade)
    return {i["metric"]: i["prompt"] for i in items}, grade


def test_judges_see_earlier_replies_and_tool_results() -> None:
    prompts, grade = _prompts({}, MULTI_TURN_CASE, _multi_turn_trace())
    for prompt in prompts.values():
        # Turn 1 in full: the user message, the tool call and result, the reply.
        assert "UNIQUE_TURN1_MARKER" in prompt
        assert 'get_order({"id": 7}) -> TURN1_TOOL_RESULT status=shipped' in prompt
        assert "--- turn 1 of 2 ---" in prompt and "--- turn 2 of 2 ---" in prompt
        # The final reply is scored once, with its own tool calls listed.
        assert prompt.count("It ships to 99 Lake Rd.") == 1
        assert '- get_address({"id": 7}) -> 99 Lake Rd' in prompt
        # Dataset context is shown, and labelled as never sent to the agent.
        assert "system (from the dataset; not sent to the agent): The user is a" in prompt
    assert "earlier turn counts" in prompts["task_success"]
    assert "tool results of every turn" in prompts["groundedness"]
    assert grade.judge_notes == [] and grade.unrecorded_turns == 0


def test_conversation_and_transcript_pieces() -> None:
    rendered = render_case(parse_case(MULTI_TURN_CASE), _multi_turn_trace())
    lines = rendered.conversation.splitlines()
    assert lines == [
        "system (from the dataset; not sent to the agent): The user is a returning customer.",
        "--- turn 1 of 2 ---",
        "user: What is the status of order 7?",
        'agent tool call: get_order({"id": 7}) -> TURN1_TOOL_RESULT status=shipped',
        "agent: UNIQUE_TURN1_MARKER: order 7 has shipped.",
        "--- turn 2 of 2 ---",
        "user: And where does it ship?",
    ]
    assert rendered.transcript.splitlines() == [
        *lines,
        'agent tool call: get_address({"id": 7}) -> 99 Lake Rd',
        "agent: It ships to 99 Lake Rd.",
    ]


def test_single_turn_conversation_is_unchanged() -> None:
    case = {"id": "one", "messages": [{"role": "user", "content": "hi"}]}
    rendered = render_case(parse_case(case), make_trace("one", response="hello", case=case))
    assert rendered.conversation == "user: hi"
    assert rendered.transcript == "user: hi\nagent: hello"


def test_custom_template_can_use_the_whole_transcript() -> None:
    data = {
        "judges": {
            "flow": {
                "rubric": "r",
                "threshold": 1,
                "prompt_template": "{transcript}\n=> {response}",
            }
        }
    }
    case = dict(MULTI_TURN_CASE, judge={"flow": {}})
    prompts, _ = _prompts(data, case, _multi_turn_trace())
    assert prompts["flow"].endswith(
        'agent tool call: get_address({"id": 7}) -> 99 Lake Rd\n'
        "agent: It ships to 99 Lake Rd.\n=> It ships to 99 Lake Rd."
    )
    assert "UNIQUE_TURN1_MARKER" in prompts["flow"]


def test_trace_without_turns_says_the_replies_are_unrecorded() -> None:
    trace = _multi_turn_trace()
    del trace["turns"]  # an older traces file or a generate override
    prompts, grade = _prompts({}, MULTI_TURN_CASE, trace)
    assert "agent: [reply not recorded in the trace]" in prompts["task_success"]
    assert grade.unrecorded_turns == 1
    assert any("no recorded reply" in n for n in grade.judge_notes)


def test_default_limit_keeps_results_far_beyond_the_old_2000_char_cut() -> None:
    """The regression: a fact past character 2000 of a tool result must reach the judge."""
    orders = [
        {"id": f"ORD-{1000 + i}", "customer": "alice", "address": f"{i} Lake Rd"} for i in range(60)
    ]
    result = json.dumps(orders)
    assert 3000 < len(result) < DEFAULT_MAX_TOOL_RESULT_CHARS
    case = {
        "id": "list",
        "messages": [{"role": "user", "content": "list my orders"}],
        "judge": {"groundedness": {"threshold": 4}},
    }
    trace = make_trace(
        "list",
        response="ORD-1059 ships to 59 Lake Rd.",
        tool_calls=[{"name": "list_orders", "args": {}, "result": result, "is_error": False}],
        case=case,
    )
    prompts, grade = _prompts({}, case, trace)
    tool_line = next(line for line in prompts["groundedness"].splitlines() if "list_orders" in line)
    assert '"59 Lake Rd"' in tool_line and "[TRUNCATED by" not in prompts["groundedness"]
    assert grade.truncated_tool_results == 0


def test_a_cut_is_marked_and_counted() -> None:
    text, omitted = cut_tool_result("x" * 150, 100)
    assert omitted == 50
    assert text.startswith("x" * 100 + " [TRUNCATED by graph-agents-cli: the judge sees the first")
    assert "100 of 150 characters" in text and "50 more were returned to the agent" in text
    assert "Do not treat a claim as unsupported" in text
    assert cut_tool_result("x" * 150, None) == ("x" * 150, 0)
    assert cut_tool_result("x" * 100, 100) == ("x" * 100, 0)

    trace = _multi_turn_trace()
    trace["turns"][0]["tool_calls"][0]["result"] = "A" * 500
    trace["tool_calls"][0]["result"] = "B" * 500
    prompts, grade = _prompts({"judge": {"max_tool_result_chars": 100}}, MULTI_TURN_CASE, trace)
    prompt = prompts["groundedness"]
    assert "A" * 101 not in prompt and "B" * 101 not in prompt
    assert prompt.count("[TRUNCATED by graph-agents-cli") == 2
    assert grade.truncated_tool_results == 2
    assert grade.judge_notes[0].startswith("2 tool result(s) cut for the judge (800 characters")

    prompts, grade = _prompts({"judge": {"max_tool_result_chars": None}}, MULTI_TURN_CASE, trace)
    assert "A" * 500 in prompts["groundedness"] and grade.truncated_tool_results == 0


@pytest.mark.parametrize(
    ("judge", "message"),
    [
        ({"max_tool_result_chars": 0}, "max_tool_result_chars must be a whole number"),
        ({"max_tool_result_chars": -5}, "max_tool_result_chars must be a whole number"),
        ({"max_tool_result_chars": "big"}, "max_tool_result_chars must be a whole number"),
        ({"max_tool_result_chars": True}, "max_tool_result_chars must be a whole number"),
        ({"max_tool_result_chars": 1.5}, "max_tool_result_chars must be a whole number"),
        ({"max_tool_results_chars": 100}, "unknown judge key"),
    ],
)
def test_judge_block_is_validated(judge: dict[str, Any], message: str) -> None:
    with pytest.raises(EvalConfigError, match=message):
        parse_eval_config({"judge": judge})


def test_judge_block_limit_defaults() -> None:
    assert parse_eval_config({}).max_tool_result_chars == DEFAULT_MAX_TOOL_RESULT_CHARS
    assert (
        parse_eval_config({"judge": {"max_tool_result_chars": None}}).max_tool_result_chars is None
    )
    assert parse_eval_config({"judge": {"max_tool_result_chars": 7}}).max_tool_result_chars == 7


def test_grade_reports_cuts_and_records_notes(
    project: Path, runner: CliRunner, fake_judge: FakeJudge
) -> None:
    (project / _paths.DEFAULT_EVAL_CONFIG).write_text(
        "judge: {provider: fake, max_tool_result_chars: 100}\n", encoding="utf-8"
    )
    trace = _multi_turn_trace()
    trace["tool_calls"][0]["result"] = "B" * 500
    write_json_file(project / _paths.DEFAULT_INPUT_DATASET, {"cases": [MULTI_TURN_CASE]})
    write_traces(project, [trace], digest=dataset_hash([MULTI_TURN_CASE]))
    result = runner.invoke(cmd_grade, [], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    results = read_results(project)
    case = results["cases"][0]
    assert case["judge_notes"] and "1 tool result(s) cut" in case["judge_notes"][0]
    warning = next(w for w in results["warnings"] if "max_tool_result_chars" in w)
    assert "1 tool result(s) in 1 case(s)" in warning and "follow-up" in warning
    assert "were cut in the judge prompt" in result.output
    # Every judge prompt carried the whole first turn.
    assert all("UNIQUE_TURN1_MARKER" in i["prompt"] for i in fake_judge.calls[0]["items"])
