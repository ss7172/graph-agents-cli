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

"""What a judge sees of one case: the conversation, tool calls and replies of every turn.

A multi-turn case sends each user message on one thread, and the trace keeps
every turn under ``turns`` (the top-level ``response`` and ``tool_calls`` are
the final turn's). A judge scoring the final reply needs the earlier replies
and tool results: a follow-up that relies on an answer already given, or a
claim grounded in an earlier tool result, would otherwise look wrong.

Rendered pieces:

* ``conversation``: every turn before the reply being scored in full (user
  message, each tool call with its result, the agent's reply), then the final
  user message. Dataset ``system``/``assistant`` messages, which are not sent
  to the agent, appear in place and are labelled as such.
* ``transcript``: the same plus the final turn's tool calls and reply.
* the final turn's tool calls, rendered by ``render_tool_calls``.

Tool results longer than the configured limit are cut with an explicit marker
that tells the judge how much it did not see, so a claim drawn from the omitted
part is never mistaken for an invented one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from graph_agents_cli.eval.dataset import EvalCase

# Characters of one tool result shown to a judge before it is cut. The agent
# read the whole result, so the judge needs it too; the bound only keeps one
# runaway tool from exhausting the judge's context. Configurable per project as
# ``judge.max_tool_result_chars`` in eval_config.yaml (null = never cut).
DEFAULT_MAX_TOOL_RESULT_CHARS = 50_000

NOT_SENT = "(from the dataset; not sent to the agent)"


@dataclass
class RenderedCase:
    conversation: str
    transcript: str
    final_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # Tool results (in any turn) cut for the judge, and how many characters went unseen.
    truncated_results: int = 0
    truncated_chars: int = 0
    # Earlier turns whose replies the trace does not record (an older trace or a
    # generate override): the judge is told, rather than shown a gap silently.
    unrecorded_turns: int = 0

    @property
    def notes(self) -> list[str]:
        notes: list[str] = []
        if self.truncated_results:
            notes.append(
                f"{self.truncated_results} tool result(s) cut for the judge "
                f"({self.truncated_chars} characters not shown; raise "
                "judge.max_tool_result_chars in eval_config.yaml to show more)"
            )
        if self.unrecorded_turns:
            notes.append(
                f"{self.unrecorded_turns} earlier turn(s) have no recorded reply in the trace "
                "(re-run eval generate to record every turn)"
            )
        return notes


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _args_text(args: Any) -> str:
    if args is None or args == {}:
        return ""
    return _as_text(args)


def cut_tool_result(text: str, max_chars: int | None) -> tuple[str, int]:
    """``(text shown to the judge, characters cut)``; the cut is marked in-band."""
    if max_chars is None or len(text) <= max_chars:
        return text, 0
    omitted = len(text) - max_chars
    marker = (
        f" [TRUNCATED by graph-agents-cli: the judge sees the first {max_chars} of "
        f"{len(text)} characters of this tool result; {omitted} more were returned to the "
        "agent but are not shown here. Do not treat a claim as unsupported only because "
        "it could come from the omitted part.]"
    )
    return text[:max_chars] + marker, omitted


def render_tool_call(call: dict[str, Any], max_chars: int | None) -> tuple[str, int]:
    """``name(args) -> result`` for one call, and how many result characters were cut."""
    result, omitted = cut_tool_result(_as_text(call.get("result")), max_chars)
    flag = " (error)" if call.get("is_error") else ""
    return f"{call.get('name')}({_args_text(call.get('args'))}) -> {result}{flag}", omitted


def render_tool_calls(
    calls: list[dict[str, Any]] | None, max_chars: int | None
) -> tuple[list[str], int, int]:
    """Rendered lines, the number of results cut, and the characters cut."""
    lines: list[str] = []
    cut = 0
    omitted_total = 0
    for call in calls or []:
        if not isinstance(call, dict):
            continue
        line, omitted = render_tool_call(call, max_chars)
        lines.append(line)
        if omitted:
            cut += 1
            omitted_total += omitted
    return lines, cut, omitted_total


def render_case(
    case: EvalCase,
    trace: dict[str, Any],
    *,
    max_tool_result_chars: int | None = DEFAULT_MAX_TOOL_RESULT_CHARS,
) -> RenderedCase:
    """Render the conversation and full transcript of one graded case."""
    user_count = sum(1 for m in case.messages if m.get("role") == "user")
    raw_turns = trace.get("turns")
    turns: list[dict[str, Any]] = (
        [t if isinstance(t, dict) else {} for t in raw_turns] if isinstance(raw_turns, list) else []
    )
    final_calls = [c for c in (trace.get("tool_calls") or []) if isinstance(c, dict)]
    final_response = trace.get("response")
    labelled = user_count > 1

    before: list[str] = []  # everything up to and including the final user message
    after: list[str] = []  # the final turn's tool calls and reply (transcript only)
    rendered = RenderedCase(conversation="", transcript="", final_tool_calls=final_calls)

    def _calls(lines_out: list[str], calls: list[dict[str, Any]]) -> None:
        lines, cut, omitted = render_tool_calls(calls, max_tool_result_chars)
        rendered.truncated_results += cut
        rendered.truncated_chars += omitted
        lines_out.extend(f"agent tool call: {line}" for line in lines)

    turn_index = -1
    for message in case.messages:
        role = message.get("role", "?")
        content = str(message.get("content", ""))
        if role != "user":
            # Dataset context for the judge; the agent never received it.
            before.append(f"{role} {NOT_SENT}: {content}")
            continue
        turn_index += 1
        if labelled:
            before.append(f"--- turn {turn_index + 1} of {user_count} ---")
        before.append(f"user: {content}")
        if turn_index == user_count - 1:
            continue  # the final turn's calls and reply are the ones being scored
        record = turns[turn_index] if turn_index < len(turns) else None
        if record is None:
            rendered.unrecorded_turns += 1
            before.append("agent: [reply not recorded in the trace]")
            continue
        _calls(before, [c for c in (record.get("tool_calls") or []) if isinstance(c, dict)])
        before.append(f"agent: {_as_text(record.get('response'))}")

    _calls(after, final_calls)
    after.append(f"agent: {_as_text(final_response)}")

    rendered.conversation = "\n".join(before)
    rendered.transcript = "\n".join(before + after)
    return rendered
