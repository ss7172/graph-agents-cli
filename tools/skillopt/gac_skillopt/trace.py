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

"""Agent event streams -> one harness-independent trace -> a compact ``conversation.json``.

Claude Code (``--output-format stream-json``) and Codex (``exec --json``) report the same things
differently. ``Trace`` keeps what the verifier and SkillOpt's reflection need: the commands the
agent ran, the files it read and wrote, whether it loaded the skill under test, its final
message, and usage. ``conversation()`` renders it in the record shapes SkillOpt's
``fmt_trajectory`` understands, cut to a character budget (SkillOpt itself never truncates).
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import asdict, dataclass, field
from typing import Any

OBS_HEAD = 800
OBS_TAIL = 800
TRAJECTORY_BUDGET = 24_000


@dataclass
class Step:
    kind: str  # command | read | write | edit | skill | search | tool | message
    cmd: str  # what the agent did, as one line
    obs: str = ""  # what it got back
    exit_code: int | None = None
    is_error: bool = False


@dataclass
class Trace:
    harness: str
    prompt: str = ""
    steps: list[Step] = field(default_factory=list)
    final: str = ""
    skill_loaded: bool = False
    usage: dict[str, Any] = field(default_factory=dict)
    num_turns: int | None = None
    init: dict[str, Any] = field(default_factory=dict)
    error: str = ""  # the harness reported a failed session (rate limit, API error, crash)
    # Claude Code only: tool calls its permission gate refused ({"tool", "input"}).
    permission_denials: list[dict[str, str]] = field(default_factory=list)

    @property
    def commands(self) -> list[str]:
        return [s.cmd for s in self.steps if s.kind == "command"]

    @property
    def files_written(self) -> list[str]:
        return [s.cmd.split(" ", 1)[-1] for s in self.steps if s.kind in ("write", "edit")]

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["commands"] = self.commands
        return data


def _events(stdout: str) -> list[dict[str, Any]]:
    out = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            out.append(event)
    return out


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or ""))
            else:
                parts.append(str(block))
        return "\n".join(p for p in parts if p)
    return "" if content is None else str(content)


def _skill_path_re(skill: str) -> re.Pattern[str]:
    return re.compile(rf"skills/{re.escape(skill)}/SKILL\.md")


# ── Claude Code ─────────────────────────────────────────────────────────────


def parse_claude(stdout: str, *, skill: str, prompt: str = "") -> Trace:
    trace = Trace(harness="claude", prompt=prompt)
    events = _events(stdout)
    skill_re = _skill_path_re(skill)
    pending: dict[str, Step] = {}
    results: list[dict[str, Any]] = []
    hooks = 0
    for event in events:
        kind = event.get("type")
        if kind == "system" and "hook" in str(event.get("subtype", "")).lower():
            hooks += 1
        if kind == "system" and event.get("subtype") == "init":
            trace.init = {
                k: event.get(k)
                for k in (
                    "model",
                    "tools",
                    "skills",
                    "plugins",
                    "mcp_servers",
                    "cwd",
                    "permissionMode",
                    "claude_code_version",
                )
                if k in event
            }
        elif kind == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    trace.steps.append(Step("message", str(block["text"]).strip()))
                elif block.get("type") == "tool_use":
                    step = _claude_step(block, skill, skill_re)
                    if step.kind == "skill" or (step.kind == "read" and skill_re.search(step.cmd)):
                        trace.skill_loaded = True
                    trace.steps.append(step)
                    pending[str(block.get("id"))] = step
        elif kind == "user":
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    step = pending.pop(str(block.get("tool_use_id")), None)
                    if step is not None:
                        step.obs = _text(block.get("content"))
                        step.is_error = bool(block.get("is_error"))
        elif kind == "result":
            results.append(event)
            if event.get("is_error") or event.get("subtype") not in (None, "success"):
                trace.error = f"{event.get('subtype')}: {str(event.get('result') or '')[:300]}"
    if results:
        # A session that left a background task running gets its notification after its
        # result and answers again: a second result event, often a one-line aside. The user saw
        # every result, so the final answer is all of them; turns and time add up, and
        # total_cost_usd is already cumulative.
        trace.final = "\n\n".join(
            str(e.get("result") or "").strip() for e in results if e.get("result")
        )
        trace.num_turns = sum(int(e.get("num_turns") or 0) for e in results) or None
        last = results[-1]
        trace.usage = {
            "usage": last.get("usage") or {},
            "api_equivalent_usd": last.get("total_cost_usd"),
            "duration_ms": sum(int(e.get("duration_ms") or 0) for e in results),
            "result_events": len(results),
        }
        # Tool calls the permission gate refused (with --permission-prompts none: anything that
        # would have prompted). Noise for the benchmark, not a sandbox denial.
        for e in results:
            for d in e.get("permission_denials") or []:
                if isinstance(d, dict):
                    tool_input = d.get("tool_input") or {}
                    what = tool_input.get("command") or tool_input.get("file_path") or ""
                    trace.permission_denials.append(
                        {"tool": str(d.get("tool_name") or ""), "input": str(what)[:200]}
                    )
    if not trace.final:
        messages = [s.cmd for s in trace.steps if s.kind == "message"]
        trace.final = messages[-1] if messages else ""
    trace.init["hook_events"] = hooks
    return trace


def claude_isolation_problems(trace: Trace, skill: str) -> list[str]:
    """What the session loaded beyond the skill under test (DESIGN section 3.4); empty when
    isolated. A session without an init event (it never started) reports nothing here."""
    init = trace.init
    if not init or "skills" not in init:
        return []
    problems = []
    skills = [
        s if isinstance(s, str) else str((s or {}).get("name")) for s in init.get("skills") or []
    ]
    if skills != [skill]:
        problems.append(f"skills {skills} (expected only {skill})")
    if init.get("mcp_servers"):
        problems.append(f"MCP servers {json.dumps(init['mcp_servers'])[:200]}")
    plugins = [str(p.get("name") if isinstance(p, dict) else p) for p in init.get("plugins") or []]
    extra = [p for p in plugins if not p.startswith("agents-md")]
    if extra:
        problems.append(f"plugins {extra}")
    if init.get("hook_events"):
        problems.append(f"{init['hook_events']} hook events")
    return problems


def claude_tool_uses(stdout: str) -> list[dict[str, Any]]:
    """Every tool call of a Claude Code event stream with its full input and its result:
    ``{"name", "input", "result", "is_error"}``. The preflight judges from these, not from the
    model's narration (e.g. whether a Bash call really carried ``dangerouslyDisableSandbox``)."""
    calls: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in _events(stdout):
        message = event.get("message")
        blocks = (message.get("content") or []) if isinstance(message, dict) else []
        if event.get("type") == "system" and event.get("subtype") == "permission_denied":
            call = calls.get(str(event.get("tool_use_id")))
            if call is not None:
                call["denied_by"] = str(
                    event.get("decision_reason") or event.get("decision_reason_type") or ""
                )
        if event.get("type") == "assistant":
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    key = str(block.get("id"))
                    calls[key] = {
                        "name": str(block.get("name") or ""),
                        "input": block.get("input") or {},
                        "result": "",
                        "is_error": False,
                    }
                    order.append(key)
        elif event.get("type") == "user":
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    call = calls.get(str(block.get("tool_use_id")))
                    if call is not None:
                        call["result"] = _text(block.get("content"))
                        call["is_error"] = bool(block.get("is_error"))
    return [calls[k] for k in order]


def _claude_step(block: dict[str, Any], skill: str, skill_re: re.Pattern[str]) -> Step:
    name = str(block.get("name") or "")
    args = block.get("input") or {}
    if name == "Bash":
        return Step("command", str(args.get("command", "")).strip())
    if name == "Read":
        return Step("read", f"read {args.get('file_path', '')}")
    if name == "Write":
        return Step("write", f"write {args.get('file_path', '')}")
    if name in ("Edit", "MultiEdit", "NotebookEdit"):
        return Step("edit", f"edit {args.get('file_path', args.get('notebook_path', ''))}")
    if name == "Skill":
        return Step("skill", f"skill {args.get('skill') or args.get('name') or ''}".strip())
    if name in ("Glob", "Grep"):
        return Step("search", f"{name.lower()} {json.dumps(args, sort_keys=True)}")
    return Step("tool", f"{name} {json.dumps(args, sort_keys=True)[:300]}")


# ── Codex ───────────────────────────────────────────────────────────────────


def unwrap_shell(command: str) -> str:
    """Codex runs every command as ``/bin/zsh -lc '<script>'``: keep the script."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return command
    if len(parts) == 3 and parts[0].endswith(("sh", "zsh", "bash")) and parts[1] in ("-lc", "-c"):
        return parts[2]
    return command


def parse_codex(stdout: str, *, skill: str, prompt: str = "", last_message: str = "") -> Trace:
    trace = Trace(harness="codex", prompt=prompt)
    skill_re = _skill_path_re(skill)
    # cache_write_input_tokens: the uncached input the API wrote to its prompt cache, billed above
    # the plain input price (budget.usd); Codex reports it beside the other three.
    usage = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 0,
    }
    turns = 0
    for event in _events(stdout):
        kind = event.get("type")
        if kind == "turn.completed":
            turns += 1
            for key in usage:
                usage[key] += int((event.get("usage") or {}).get(key, 0) or 0)
        elif kind in ("turn.failed", "error"):
            message = event.get("message") or (event.get("error") or {}).get("message") or ""
            if kind == "turn.failed" or "metadata" not in str(message).lower():
                trace.error = str(message)[:300]
        elif kind == "item.completed":
            item = event.get("item") or {}
            itype = item.get("type")
            if itype == "command_execution":
                cmd = unwrap_shell(str(item.get("command", "")))
                code = item.get("exit_code")
                step = Step(
                    "command",
                    cmd,
                    obs=str(item.get("aggregated_output") or ""),
                    exit_code=int(code) if str(code).lstrip("-").isdigit() else None,
                )
                step.is_error = step.exit_code not in (None, 0)
                if skill_re.search(cmd):
                    trace.skill_loaded = True
                trace.steps.append(step)
            elif itype == "file_change":
                for change in item.get("changes") or []:
                    kind_ = "write" if change.get("kind") == "add" else "edit"
                    trace.steps.append(Step(kind_, f"{kind_} {change.get('path', '')}"))
            elif itype == "agent_message" and item.get("text"):
                trace.steps.append(Step("message", str(item["text"]).strip()))
            elif itype in ("mcp_tool_call", "web_search"):
                trace.steps.append(Step("tool", f"{itype} {json.dumps(item)[:300]}"))
    trace.usage = usage
    trace.num_turns = turns
    messages = [s.cmd for s in trace.steps if s.kind == "message"]
    trace.final = last_message.strip() or (messages[-1] if messages else "")
    return trace


# ── conversation.json for SkillOpt's reflection ─────────────────────────────


def _cut(text: str, head: int = OBS_HEAD, tail: int = OBS_TAIL) -> str:
    text = text or ""
    if len(text) <= head + tail + 40:
        return text
    return f"{text[:head]}\n[... {len(text) - head - tail} characters cut ...]\n{text[-tail:]}"


def conversation(
    trace: Trace,
    *,
    skill: str,
    checks: list[dict[str, Any]] | None = None,
    budget: int = TRAJECTORY_BUDGET,
) -> list[dict[str, Any]]:
    """The trajectory in SkillOpt's record shapes: the task, one ``tool_call`` per action, the
    agent's final message, then one ``system`` (verification) record per verifier check. Reads
    of the skill itself are abbreviated (the analyst already has the skill). When the whole is
    over ``budget`` characters, tool calls are dropped from the middle, never the checks."""
    skill_re = _skill_path_re(skill)
    head: list[dict[str, Any]] = [{"role": "user", "content": trace.prompt}]
    calls: list[dict[str, Any]] = []
    for step in trace.steps:
        if step.kind == "message":
            calls.append({"role": "assistant", "content": _cut(step.cmd, 600, 200)})
            continue
        obs = _cut(step.obs)
        if step.kind == "skill" or skill_re.search(step.cmd):
            obs = "[loaded SKILL.md]"
        elif re.search(rf"skills/{re.escape(skill)}/references/", step.cmd):
            obs = "[read a reference file of the skill]"
        if step.exit_code not in (None, 0):
            obs = f"(exit {step.exit_code}) {obs}"
        calls.append({"type": "tool_call", "cmd": _cut(step.cmd, 600, 200), "obs": obs})
    final = _cut(trace.final, 2000, 500)
    if (
        calls
        and calls[-1].get("role") == "assistant"
        and trace.final.strip().startswith(str(calls[-1]["content"])[:200].strip())
    ):
        calls.pop()  # the last message is the final answer, shown once below
    tail: list[dict[str, Any]] = [{"role": "assistant", "content": final}]
    if trace.error:
        tail.append({"role": "system", "content": f"harness: session ended with {trace.error}"})
    for check in checks or []:
        state = "PASS" if check.get("passed") else "FAIL"
        kind = "mandatory" if check.get("mandatory") else "optional"
        text = f"check {check.get('id')} ({kind}) {state}: {check.get('detail', '')}"
        if not check.get("passed") and check.get("output"):
            text += f"\n{_cut(str(check['output']), 300, 900)}"
        tail.append({"role": "system", "content": text})

    def size(records: list[dict[str, Any]]) -> int:
        return sum(len(json.dumps(r)) for r in records)

    fixed = size(head) + size(tail)
    if fixed + size(calls) > budget and calls:
        keep_head, keep_tail = [], []
        room = max(budget - fixed, 2000)
        lo, hi = 0, len(calls) - 1
        # Alternate from both ends until the budget is spent: the start (how the agent
        # oriented itself) and the end (what it concluded) matter most.
        used = 0
        while lo <= hi:
            for side in ("head", "tail"):
                if lo > hi:
                    break
                record = calls[lo] if side == "head" else calls[hi]
                cost = len(json.dumps(record))
                if used + cost > room:
                    lo = hi + 1
                    break
                used += cost
                if side == "head":
                    keep_head.append(record)
                    lo += 1
                else:
                    keep_tail.insert(0, record)
                    hi -= 1
        dropped = len(calls) - len(keep_head) - len(keep_tail)
        marker = {"role": "system", "content": f"[{dropped} actions omitted from the middle]"}
        calls = [*keep_head, marker, *keep_tail] if dropped else keep_head + keep_tail
    return head + calls + tail
