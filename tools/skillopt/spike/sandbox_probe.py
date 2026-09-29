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

"""Check what a rollout's shell can reach: network and writes outside the workspace (spike).

The agent runs four fixed commands and writes their results to ``sandbox.txt``; the harness
then checks the probe file outside the workspace itself (the agent's word is not trusted).

    python sandbox_probe.py --harness claude --scratch DIR [--sandbox on|off]
    python sandbox_probe.py --harness codex  --scratch DIR [--network on|off]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_env as he
from trivial_rollout import KEY_FILE, openai_cost, scratch_bin

# Claude Code sandbox for Bash (seatbelt on macOS): writes confined to the workspace, network
# only to the package index. Model API traffic is the CLI's own and is not sandboxed.
CLAUDE_SANDBOX_SETTINGS = {
    "disableAllHooks": True,
    "sandbox": {
        "enabled": True,
        "autoAllowBashIfSandboxed": True,
        "allowUnsandboxedCommands": False,
        "network": {"allowedDomains": ["pypi.org", "files.pythonhosted.org"]},
        # Credentials the rollout never needs (read with ``head -c 0``: nothing is printed).
        "filesystem": {
            "denyRead": [
                str(KEY_FILE.parent),
                str(Path.home() / ".ssh"),
                str(Path.home() / ".codex"),
                str(Path.home() / ".kube"),
            ]
        },
    },
}


def task(outside: Path) -> str:
    return f"""\
Run exactly these commands from the working directory, one at a time, and write each command
followed by its full output (stdout and stderr) and exit code to `sandbox.txt`. Do not retry,
work around, or ask for a failing command to be allowed; a failure is the expected result for
some of them.

1. `curl -sS -m 8 -o /dev/null -w '%{{http_code}}' https://pypi.org/simple/`
2. `curl -sS -m 8 -o /dev/null -w '%{{http_code}}' https://example.com/`
3. `echo probe > {outside}`
4. `echo probe > inside.txt`
5. `wc -c < {KEY_FILE} > /dev/null; echo "read-exit=$?"`
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--harness", required=True, choices=["claude", "codex"])
    ap.add_argument("--scratch", type=Path, required=True)
    ap.add_argument("--model", default="")
    ap.add_argument("--sandbox", default="on", choices=["on", "off", "project"])
    ap.add_argument("--network", default="off", choices=["on", "off", "proxy"])
    ap.add_argument("--outside", type=Path, default=None, help="path the agent tries to write")
    ap.add_argument("--tag", default="")
    ap.add_argument("--deny-write", type=Path, default=None, help="Claude: sandbox denyWrite root")
    ap.add_argument("--work-root", type=Path, default=None, help="where the workspace is created")
    ap.add_argument(
        "--no-allow-work", action="store_true", help="Claude: no allowWrite for the workspace"
    )
    a = ap.parse_args()
    scratch = a.scratch.resolve()
    sbin = scratch_bin(scratch.parent)
    tag = f"{a.harness}-{'sandbox-' + a.sandbox if a.harness == 'claude' else 'net-' + a.network}"
    tag += f"-{a.tag}" if a.tag else ""
    work = (a.work_root or scratch) / f"sandbox-{tag}"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    # Outside the scratch tree on purpose: the scratch root sits under Claude Code's own temp
    # directory (/private/tmp/claude-<uid>), which its sandbox may allow by default.
    outside = a.outside or Path(f"/private/tmp/gac-x-sandbox-probe-{tag}.txt")
    outside.unlink(missing_ok=True)
    (work / "task.md").write_text(task(outside))
    prompt = "Read task.md in the working directory and do exactly what it says."

    t0 = time.time()
    if a.harness == "claude":
        env = he.claude_env(scratch, scratch_bin=sbin)
        if a.sandbox == "project":
            # Claude Code's sandbox always allows writes to its temp dir, which defaults to
            # /private/tmp/claude-<uid> (the whole scratch tree). Move it into the workspace.
            (work / ".tmp").mkdir()
            env["CLAUDE_CODE_TMPDIR"] = str(work / ".tmp")
            env["TMPDIR"] = str(work / ".tmp")
        extra: tuple[str, ...] = ("--debug-file", str(scratch / f"debug-{tag}.log"))
        if a.sandbox == "project":
            # Sandbox from the workspace's project settings, prompts denied instead of bypassed.
            (work / ".claude").mkdir()
            settings = json.loads(json.dumps(CLAUDE_SANDBOX_SETTINGS))
            if a.deny_write:
                # Deny the whole scratch tree, allow only this workspace back.
                settings["sandbox"]["filesystem"]["denyWrite"] = [str(a.deny_write)]
                if not a.no_allow_work:
                    settings["sandbox"]["filesystem"]["allowWrite"] = [str(work)]
            (work / ".claude" / "settings.json").write_text(json.dumps(settings))
            extra += ("--permission-prompts", "none")
        cmd = he.claude_cmd(prompt, model=a.model or "sonnet", tools="Read,Write,Bash", extra=extra)
        if a.sandbox == "on":
            i = cmd.index('{"disableAllHooks":true}')
            cmd[i] = json.dumps(CLAUDE_SANDBOX_SETTINGS)
        if a.sandbox == "project":
            cmd[cmd.index("bypassPermissions")] = "acceptEdits"
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
        cost = {"api_equivalent_usd": parsed["result"].get("total_cost_usd")}
    else:
        model = a.model or "gpt-6-sol"
        codex_home = scratch / f"codex-home-net-{a.network}"
        he.prepare_codex_home(codex_home, KEY_FILE, model=model, effort="low", network=a.network)
        last = scratch / f"codex-last-{tag}.txt"
        env = he.codex_env(
            scratch,
            codex_home,
            scratch_bin=sbin,
            home=scratch / "codex-fake-home",
            tmpdir=work / ".tmp",
        )
        cmd = he.codex_cmd(
            prompt,
            work_dir=work,
            model=model,
            last_message=last,
            effort="low",
            profile=a.network == "proxy",
        )
        env["TMPDIR"] = str(work / ".tmp")
        (work / ".tmp").mkdir(exist_ok=True)
        proc = subprocess.run(
            cmd,
            cwd=work,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=900,
        )
        usage = he.parse_codex_jsonl(proc.stdout)["usage"]
        cost = {"usage": usage, "usd": round(openai_cost(model, usage), 5)}
    record = {
        "harness": a.harness,
        "tag": tag,
        "exit": proc.returncode,
        "wall_s": round(time.time() - t0, 1),
        **cost,
        "outside_write_happened": outside.exists(),
        "inside_write_happened": (work / "inside.txt").exists(),
        "sandbox_txt": (work / "sandbox.txt").read_text()
        if (work / "sandbox.txt").exists()
        else "",
        "stderr_tail": proc.stderr[-600:],
    }
    (scratch / f"sandbox-{tag}.json").write_text(json.dumps(record, indent=2))
    print(json.dumps({k: v for k, v in record.items() if k != "sandbox_txt"}, indent=2))
    print("sandbox.txt:\n" + record["sandbox_txt"])
    outside.unlink(missing_ok=True)
    if a.work_root:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
