#!/usr/bin/env python3
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

"""One trivial SkillOpt-shaped rollout per harness, to measure cost and time (spike).

The workspace holds a real skill (``graph-agents-cli-workflow``: SKILL.md plus references) as a
native skill of the harness, and a ``task.md``; the verifier is deterministic (file content).
The task also records the rollout's shell environment, so the run doubles as a check that the
developer's PATH (``~/.local/bin`` holds google-agents-cli and agents-cli) does not reach the
agent's shell.

    python trivial_rollout.py --harness claude --scratch DIR [--model sonnet]
    python trivial_rollout.py --harness codex  --scratch DIR [--model gpt-6-sol]

Record Codex spend in the ledger afterwards (the script prints the computed cost).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_env as he

REPO = Path(__file__).resolve().parents[3]
KEY_FILE = he.KEY_FILE
SKILL = "graph-agents-cli-workflow"

# USD per 1M tokens (input, cached input, output), OpenAI pricing page read 2026-09-27.
PRICES = {
    "gpt-6-astra": (10.00, 1.00, 50.00),
    "gpt-6-sol": (2.00, 0.20, 10.00),
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "gpt-5.6-sol": (4.00, 0.40, 20.00),
    "gpt-5.6-terra": (2.00, 0.20, 12.00),
    "gpt-5.5": (5.00, 0.50, 30.00),
    "gpt-5.4": (2.50, 0.25, 15.00),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
    "gpt-5.3-codex": (1.75, 0.175, 14.00),
}

TASK = """\
# Task

1. Record the shell environment: run `echo "$PATH"` and
   `command -v graph-agents-cli google-agents-cli agents-cli uv || true` and write their
   combined output to `env.txt`.
2. Write to `answer.txt` the single graph-agents-cli command (with its subcommand, no
   options) that runs the agent over its eval dataset and grades it. Nothing else in the file.
"""


def openai_cost(model: str, usage: dict) -> float:
    pin, pcached, pout = PRICES[model]
    cached = usage.get("cached_input_tokens", 0)
    uncached = usage.get("input_tokens", 0) - cached
    return (uncached * pin + cached * pcached + usage.get("output_tokens", 0) * pout) / 1e6


def scratch_bin(research: Path) -> Path:
    """graph-agents-cli (this checkout, installed with uv tool into scratch), uv and uvx."""
    b = research / "bin"
    if not (b / "graph-agents-cli").exists():
        subprocess.run(
            ["uv", "tool", "install", "--quiet", "--from", str(REPO), "graph-agents-cli"],
            env={
                **os.environ,
                "UV_TOOL_DIR": str(research / "uv-tools"),
                "UV_TOOL_BIN_DIR": str(b),
            },
            check=True,
        )
    # uv < 0.9.29 panics inside the macOS agent sandboxes (SCDynamicStore is blocked:
    # "Tokio executor failed"); the developer's uv is 0.9.2, so rollouts get a current uv.
    uv_venv = research / "uv-new"
    if not (uv_venv / "bin" / "uv").exists():
        subprocess.run(["uv", "venv", "-q", str(uv_venv)], check=True)
        subprocess.run(
            ["uv", "pip", "install", "-q", "uv"],
            env={**os.environ, "VIRTUAL_ENV": str(uv_venv)},
            check=True,
        )
    for tool in ("uv", "uvx"):
        if not (b / tool).exists():
            (b / tool).symlink_to(uv_venv / "bin" / tool)
    return b


def verify(work: Path) -> dict:
    answer = (work / "answer.txt").read_text().strip() if (work / "answer.txt").exists() else ""
    env_txt = (work / "env.txt").read_text() if (work / "env.txt").exists() else ""
    leaked = [n for n in ("google-agents-cli", "agents-cli") if f"/{n}\n" in env_txt + "\n"]
    return {
        "answer": answer,
        "answer_ok": answer.strip("`").strip() == "graph-agents-cli eval run",
        "env_txt": env_txt,
        "confound_bins_on_path": leaked,
        "dot_local_bin_on_path": "/.local/bin" in env_txt,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--harness", required=True, choices=["claude", "codex"])
    ap.add_argument("--scratch", type=Path, required=True)
    ap.add_argument("--model", default="")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--shell", default="", help="Claude only: CLAUDE_CODE_SHELL value")
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    scratch = a.scratch.resolve()
    research = scratch.parent
    sbin = scratch_bin(research)
    tag = f"{a.harness}{'-' + a.tag if a.tag else ''}"
    work = scratch / f"trivial-{tag}"
    if work.exists():
        shutil.rmtree(work)
    skills_root = work / (".claude/skills" if a.harness == "claude" else ".agents/skills")
    shutil.copytree(REPO / "skills" / SKILL, skills_root / SKILL)
    (work / "task.md").write_text(TASK)
    prompt = (
        "Read task.md in the working directory and complete it. The graph-agents-cli skills "
        "available to you describe the CLI. Work only inside the working directory."
    )

    t0 = time.time()
    if a.harness == "claude":
        model = a.model or "sonnet"
        env = he.claude_env(scratch, scratch_bin=sbin)
        if a.shell:
            env["CLAUDE_CODE_SHELL"] = a.shell
        cmd = he.claude_cmd(prompt, model=model, effort=a.effort)
        proc = subprocess.run(
            cmd,
            cwd=work,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=900,
        )
        parsed = he.parse_stream_json(proc.stdout)
        res = parsed["result"]
        run = {
            "model": parsed["init"].get("model"),
            "skills_listed": parsed["init"].get("skills"),
            "tool_calls": [
                {"name": t["name"], "input": json.dumps(t["input"])[:200]}
                for t in parsed["tool_calls"]
            ],
            "usage": res.get("usage"),
            "api_equivalent_usd": res.get("total_cost_usd"),
            "num_turns": res.get("num_turns"),
            "duration_ms": res.get("duration_ms"),
            "hooks_fired": len(parsed["hooks"]),
        }
    else:
        model = a.model or "gpt-6-sol"
        codex_home = scratch / "codex-home"
        he.prepare_codex_home(codex_home, KEY_FILE, model=model, effort=a.effort)
        last = scratch / f"codex-last-{tag}.txt"
        env = he.codex_env(scratch, codex_home, scratch_bin=sbin, home=scratch / "codex-fake-home")
        cmd = he.codex_cmd(prompt, work_dir=work, model=model, last_message=last, effort=a.effort)
        proc = subprocess.run(
            cmd,
            cwd=work,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=900,
        )
        parsed = he.parse_codex_jsonl(proc.stdout)
        run = {
            "model": model,
            "items": [
                {
                    k: str(v)[:300]
                    for k, v in (i or {}).items()
                    if k in ("type", "command", "text", "message", "exit_code")
                }
                for i in parsed["items"]
            ],
            "usage": parsed["usage"],
            "usd": round(openai_cost(model, parsed["usage"]), 5),
        }
    wall = round(time.time() - t0, 1)
    record = {
        "harness": a.harness,
        "tag": tag,
        "exit": proc.returncode,
        "wall_s": wall,
        "effort": a.effort,
        "shell": a.shell,
        **run,
        "verify": verify(work),
        "stderr_tail": proc.stderr[-800:],
    }
    (scratch / f"trivial-{tag}.json").write_text(json.dumps(record, indent=2))
    show = {k: record[k] for k in ("harness", "exit", "wall_s", "usage")}
    show.update(
        {k: record.get(k) for k in ("api_equivalent_usd", "usd", "num_turns", "skills_listed")}
    )
    show["verify"] = {k: v for k, v in record["verify"].items() if k != "env_txt"}
    print(json.dumps(show, indent=2))
    print("env.txt:\n" + record["verify"]["env_txt"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
