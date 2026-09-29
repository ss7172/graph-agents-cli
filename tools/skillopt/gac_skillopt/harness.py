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

"""Run one agent (Claude Code or Codex) or one scripted solution in a prepared workspace."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import isolation
from .paths import Bench
from .trace import Step, Trace, parse_claude, parse_codex
from .workspace import Workspace, harness_env

# Session errors that are the infrastructure's, not the agent's: rerun once, never scored.
INFRA_MARKERS = (
    "rate limit",
    "rate_limit",
    "usage limit",
    "overloaded",
    "529",
    "500 internal",
    "502",
    "503",
    "api error",
    "connection error",
    "stream disconnected",
    "not logged in",
)

# An exhausted API account (HTTP 429 insufficient_quota): no retry can succeed and every later
# rollout would score 0 at no cost, so the run stops (budget.BudgetStop) instead of scoring it.
QUOTA_MARKERS = (
    "insufficient_quota",
    "no credits remaining",
    "exceeded your current quota",
    "quota exceeded",
)


@dataclass
class AgentRun:
    harness: str
    exit_code: int
    timed_out: bool
    wall_s: float
    trace: Trace
    stderr_tail: str = ""
    raw_path: Path | None = None
    usage: dict = field(default_factory=dict)

    @property
    def quota_error(self) -> str:
        """The account's quota is exhausted (a 429 no retry fixes), or "". Only a failed
        session counts (an error event or a nonzero exit), so a successful session whose output
        mentions a quota is scored as usual."""
        if not self.trace.error and self.exit_code in (0, None):
            return ""
        text = f"{self.trace.error} {self.stderr_tail}"
        if any(marker in text.lower() for marker in QUOTA_MARKERS):
            return (self.trace.error or self.stderr_tail)[-300:]
        return ""

    @property
    def infra_error(self) -> str:
        """Why this session failed for reasons outside the agent, or ""."""
        text = f"{self.trace.error} {self.stderr_tail}".lower()
        if self.trace.error or (self.exit_code not in (0, None) and not self.trace.steps):
            for marker in INFRA_MARKERS:
                if marker in text:
                    return (self.trace.error or self.stderr_tail)[-300:]
            if not self.trace.steps and not self.timed_out:
                return (
                    f"harness exited {self.exit_code} before any action: {self.stderr_tail[-300:]}"
                )
        return ""


def _run_stream(
    cmd: list[str], *, cwd: Path, env: dict[str, str], timeout: int, out: Path, err: Path
) -> tuple[int, bool, float]:
    """Run in its own process group; on timeout the whole group is killed."""
    start = time.time()
    with out.open("w") as fout, err.open("w") as ferr:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=fout,
            stderr=ferr,
            start_new_session=True,
        )
        timed_out = False
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    proc.wait(timeout=10)
                    break
                except subprocess.TimeoutExpired:
                    continue
            code = proc.returncode if proc.returncode is not None else -9
    return code, timed_out, round(time.time() - start, 1)


def run_claude(
    bench: Bench,
    ws: Workspace,
    *,
    model: str,
    effort: str | None,
    max_turns: int | None,
    timeout: int,
    permission_mode: str | None = None,
) -> AgentRun:
    env = isolation.claude_env(bench, ws.bench_dir, port=bench.agent_port(ws.slot))
    cmd = isolation.claude_cmd(
        ws.task.full_prompt,
        model=model,
        effort=effort,
        max_turns=max_turns,
        permission_mode=permission_mode,
    )
    out, err = ws.bench_dir / "events.jsonl", ws.bench_dir / "stderr.txt"
    code, timed_out, wall = _run_stream(
        cmd, cwd=ws.root, env=env, timeout=timeout, out=out, err=err
    )
    trace = parse_claude(
        out.read_text(errors="replace"), skill=ws.task.skill, prompt=ws.task.full_prompt
    )
    return AgentRun(
        harness="claude",
        exit_code=code,
        timed_out=timed_out,
        wall_s=wall,
        trace=trace,
        stderr_tail=err.read_text(errors="replace")[-2000:],
        raw_path=out,
        usage=trace.usage,
    )


def run_codex(
    bench: Bench, ws: Workspace, codex_home: Path, *, model: str, effort: str | None, timeout: int
) -> AgentRun:
    env = isolation.codex_env(bench, ws.bench_dir, codex_home, port=bench.agent_port(ws.slot))
    last = ws.bench_dir / "last-message.txt"
    cmd = isolation.codex_cmd(
        ws.task.full_prompt, work_dir=ws.root, model=model, effort=effort, last_message=last
    )
    out, err = ws.bench_dir / "events.jsonl", ws.bench_dir / "stderr.txt"
    code, timed_out, wall = _run_stream(
        cmd, cwd=ws.root, env=env, timeout=timeout, out=out, err=err
    )
    trace = parse_codex(
        out.read_text(errors="replace"),
        skill=ws.task.skill,
        prompt=ws.task.full_prompt,
        last_message=last.read_text(errors="replace") if last.exists() else "",
    )
    return AgentRun(
        harness="codex",
        exit_code=code,
        timed_out=timed_out,
        wall_s=wall,
        trace=trace,
        stderr_tail=err.read_text(errors="replace")[-2000:],
        raw_path=out,
        usage=trace.usage,
    )


SOLUTION_WRAPPER = """set -uo pipefail -o functrace
trap 'printf "%s\\0" "$BASH_COMMAND" >> "$GAC_CMDLOG"' DEBUG
set -e
source "$GAC_SOLUTION"
"""


def run_solution(bench: Bench, ws: Workspace, kind: str, *, timeout: int = 900) -> AgentRun:
    """A scripted solution (``gold``, ``broken``) or ``noop`` (the untouched fixture).

    The script is sourced by bash in the workspace with ``set -e``; every simple command it
    runs is recorded as a command of the trace, and what it writes to ``$GAC_FINAL`` is the
    final answer. ``$TASK_DIR`` is the task directory (for prepared files), ``$PROJECT`` the
    project directory.
    """
    trace = Trace(harness=kind, prompt=ws.task.full_prompt, skill_loaded=True)
    if kind == "noop":
        return AgentRun(harness=kind, exit_code=0, timed_out=False, wall_s=0.0, trace=trace)
    script = ws.task.dir / f"{kind}.sh"
    if not script.is_file():
        raise FileNotFoundError(script)
    env = harness_env(bench, ws, port=bench.agent_port(ws.slot))
    cmdlog, final = ws.bench_dir / f"{kind}-commands.log", ws.bench_dir / f"{kind}-final.md"
    env.update(
        {
            "GAC_CMDLOG": str(cmdlog),
            "GAC_SOLUTION": str(script),
            "GAC_FINAL": str(final),
            "TASK_DIR": str(ws.task.dir),
            "PROJECT": str(ws.project),
        }
    )
    out, err = ws.bench_dir / f"{kind}-stdout.txt", ws.bench_dir / f"{kind}-stderr.txt"
    code, timed_out, wall = _run_stream(
        ["/bin/bash", "-c", SOLUTION_WRAPPER],
        cwd=ws.root,
        env=env,
        timeout=timeout,
        out=out,
        err=err,
    )
    commands = cmdlog.read_text().split("\0") if cmdlog.exists() else []
    # A here-document's body is data (a file, the final answer), not a command.
    commands = [c.split("\n", 1)[0] if "<<" in c else c for c in commands]
    trace.steps = [
        Step("command", c) for c in commands if c.strip() and not c.startswith("source ")
    ]
    trace.final = final.read_text() if final.exists() else ""
    tail = (out.read_text(errors="replace") + err.read_text(errors="replace"))[-2000:]
    return AgentRun(
        harness=kind,
        exit_code=code,
        timed_out=timed_out,
        wall_s=wall,
        trace=trace,
        stderr_tail=tail,
    )
