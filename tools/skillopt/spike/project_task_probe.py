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

"""Can an agent run the project's own checks inside the rollout sandbox? (spike)

The harness (outside the sandbox) creates a project with this checkout's CLI and installs it
from the warm scratch uv cache; the agent then runs ``lint`` and ``eval run`` (fake provider,
server on a port from the experiment's range) and reports exit codes. The harness re-reads
the results file the eval wrote, so the agent's report is not trusted.

    python project_task_probe.py --harness claude|codex --scratch DIR --port 22002
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_env as he
from trivial_rollout import KEY_FILE, openai_cost, scratch_bin

TASK = """\
# Task

The project in `proj/` was created with graph-agents-cli and its dependencies are installed.
From `proj/`, run these and write each command with its exit code (and the last 5 lines of
its output) to `../result.txt`:

1. `graph-agents-cli lint`
2. `graph-agents-cli eval run`

Do not change any project file. Do not start `playground`.
"""


def setup_project(work: Path, sbin: Path, research: Path) -> None:
    env = {
        **os.environ,
        **he.base_env(research, scratch_bin=sbin),
        "UV_CACHE_DIR": str(research / "uv-cache"),
    }
    subprocess.run(
        [
            "graph-agents-cli",
            "create",
            "proj",
            "-o",
            str(work),
            "-y",
            "--skip-checks",
            "--registry",
            "ghcr.io/example",
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["graph-agents-cli", "install"],
        cwd=work / "proj",
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    envfile = re.sub(
        r"(?m)^API_KEY=.*$", "API_KEY=dev", (work / "proj" / ".env.example").read_text()
    )
    (work / "proj" / ".env").write_text(envfile)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--harness", required=True, choices=["claude", "codex"])
    ap.add_argument("--scratch", type=Path, required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--model", default="")
    ap.add_argument("--scratchpad", type=Path, required=True, help="write-denied for Claude")
    ap.add_argument("--debug", action="store_true", help="GRAPH_AGENTS_CLI_DEBUG=1, full eval log")
    a = ap.parse_args()
    scratch = a.scratch.resolve()
    research = scratch.parent
    sbin = scratch_bin(research)
    work = he.WORKSPACE_ROOT / f"probe-project-{a.harness}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    t_setup = time.time()
    setup_project(work, sbin, research)
    setup_s = round(time.time() - t_setup, 1)
    task = TASK
    if a.debug:
        task += (
            "\nAlso write the complete output of `graph-agents-cli eval run` to `../eval.log`.\n"
        )
    (work / "task.md").write_text(task)
    prompt = "Read task.md in the working directory and do exactly what it says."

    t0 = time.time()
    if a.harness == "claude":
        env = he.claude_env(scratch, scratch_bin=sbin)
        (work / ".tmp").mkdir()
        env.update({"TMPDIR": str(work / ".tmp"), "UV_CACHE_DIR": str(work / ".uv-cache")})
        (work / ".claude").mkdir()
        (work / ".claude" / "settings.json").write_text(
            json.dumps(he.claude_project_settings(deny_write=(a.scratchpad.resolve(),)))
        )
        cmd = he.claude_cmd(
            prompt,
            model=a.model or "sonnet",
            tools="Read,Write,Bash",
            extra=("--permission-prompts", "none"),
        )
        cmd[cmd.index("bypassPermissions")] = "acceptEdits"
        env["GRAPH_AGENTS_CLI_RUN_PORT"] = str(a.port)
        proc = subprocess.run(
            cmd,
            cwd=work,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=1200,
        )
        parsed = he.parse_stream_json(proc.stdout)
        cost = {
            "api_equivalent_usd": parsed["result"].get("total_cost_usd"),
            "num_turns": parsed["result"].get("num_turns"),
        }
    else:
        model = a.model or "gpt-6-sol"
        codex_home = scratch / "codex-home-net-proxy"
        he.prepare_codex_home(codex_home, KEY_FILE, model=model, effort="low", network="proxy")
        last = scratch / f"codex-last-project-{a.harness}.txt"
        env = he.codex_env(
            scratch,
            codex_home,
            scratch_bin=sbin,
            home=scratch / "codex-fake-home",
            tmpdir=work / ".tmp",
        )
        env.update(
            {"UV_CACHE_DIR": str(work / ".uv-cache"), "GRAPH_AGENTS_CLI_RUN_PORT": str(a.port)}
        )
        if a.debug:
            env["GRAPH_AGENTS_CLI_DEBUG"] = "1"
        cmd = he.codex_cmd(
            prompt, work_dir=work, model=model, last_message=last, effort="low", profile=True
        )
        proc = subprocess.run(
            cmd,
            cwd=work,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=1200,
        )
        usage = he.parse_codex_jsonl(proc.stdout)["usage"]
        cost = {"usage": usage, "usd": round(openai_cost(model, usage), 5)}
    wall = round(time.time() - t0, 1)
    results = (
        sorted((work / "proj" / "artifacts" / "grade_results").glob("results_*.json"))
        if (work / "proj" / "artifacts").exists()
        else []
    )
    record = {
        "harness": a.harness,
        "exit": proc.returncode,
        "setup_s": setup_s,
        "wall_s": wall,
        **cost,
        "eval_results_files": [p.name for p in results],
        "result_txt": (work / "result.txt").read_text() if (work / "result.txt").exists() else "",
        "eval_log": (work / "eval.log").read_text()[-6000:] if (work / "eval.log").exists() else "",
        "stderr_tail": proc.stderr[-600:],
    }
    (scratch / f"project-probe-{a.harness}.json").write_text(json.dumps(record, indent=2))
    print(json.dumps({k: v for k, v in record.items() if k != "result_txt"}, indent=2))
    print("result.txt:\n" + record["result_txt"])
    if record["eval_log"]:
        print("eval.log (tail):\n" + record["eval_log"][-3500:])
    # Anything left listening on the port is a leak of this rollout.
    held = subprocess.run(
        ["lsof", "-t", "-nP", f"-iTCP:{a.port}", "-sTCP:LISTEN"], capture_output=True, text=True
    ).stdout
    print("port still held:", bool(held.strip()))
    # Stop what the rollout left behind (only processes running from this workspace).
    for pid in held.split():
        cmdline = subprocess.run(
            ["ps", "-o", "command=", "-p", pid], capture_output=True, text=True
        ).stdout
        if str(work) in cmdline:
            subprocess.run(["kill", pid])
            print("killed leftover", pid)
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
