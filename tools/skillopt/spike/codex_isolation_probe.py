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

"""Prove Codex isolation for SkillOpt rollouts (spike).

One ``codex exec --json`` session in a scratch workspace holding exactly one repo skill
(``.agents/skills/gac-probe-skill``), with a scratch CODEX_HOME logged in by API key. The model
lists the skills and instructions it was given; the JSONL stream gives token usage.

    python codex_isolation_probe.py --scratch DIR --variant scratch_home --model gpt-6-sol

Variants: ``real_home`` (HOME unchanged: shows ``~/.agents/skills`` discovery) and
``scratch_home`` (HOME in scratch). Record the spend in the ledger afterwards.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_env as he

KEY_FILE = he.KEY_FILE

PROBE_SKILL = """\
---
name: gac-probe-skill
description: Probe skill for the SkillOpt isolation check. Use when asked for the probe word.
---

# Probe skill

When asked for the probe word, answer exactly: PROBE-WORD-7G3K.
"""

PROMPT = (
    "Do not run any commands except to read a SKILL.md if you need it. Answer in plain text with "
    "these sections: SKILLS: the exact names of every skill listed as available to you, or NONE. "
    "TOOLS: the names of the tools you can call. MCP: any MCP servers or connectors, or NONE. "
    "INSTRUCTIONS: quote the first line of every AGENTS.md or project instructions file you were "
    "given, or NONE. PROBE: if a skill tells you a probe word, give it."
)

# Names that exist only in the developer's own configuration: any of them in the init event or
# the answer is a leak. They are machine-specific, so they are read from
# GAC_SKILLOPT_CANARIES (comma-separated) instead of being committed; list your global skills,
# plugins, MCP servers and the first line of your global instructions files there.
OWNER_CANARIES = tuple(
    c.strip() for c in os.environ.get("GAC_SKILLOPT_CANARIES", "").split(",") if c.strip()
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scratch", type=Path, required=True)
    ap.add_argument("--variant", required=True, choices=["real_home", "scratch_home"])
    ap.add_argument("--model", default="gpt-6-sol")
    ap.add_argument("--effort", default="low")
    a = ap.parse_args()
    scratch = a.scratch.resolve()
    codex_home = scratch / "codex-home"
    he.prepare_codex_home(codex_home, KEY_FILE, model=a.model, effort=a.effort)

    work = scratch / f"codex-work-{a.variant}"
    skill_dir = work / ".agents" / "skills" / "gac-probe-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(PROBE_SKILL)
    last = scratch / f"codex-last-{a.variant}.txt"
    home = scratch / "codex-fake-home" if a.variant == "scratch_home" else None
    env = he.codex_env(scratch, codex_home, home=home)
    cmd = he.codex_cmd(PROMPT, work_dir=work, model=a.model, last_message=last, effort=a.effort)

    t0 = time.time()
    proc = subprocess.run(
        cmd,
        cwd=work,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=600,
    )
    wall = round(time.time() - t0, 1)
    parsed = he.parse_codex_jsonl(proc.stdout)
    answer = last.read_text() if last.exists() else ""
    leaks = sorted({c for c in OWNER_CANARIES if c.lower() in answer.lower()})
    record = {
        "variant": a.variant,
        "model": a.model,
        "effort": a.effort,
        "cmd": [*cmd[:-1], "<prompt>"],
        "env_keys": sorted(env),
        "exit": proc.returncode,
        "wall_s": wall,
        "event_types": parsed["types"],
        "usage": parsed["usage"],
        "items": [
            {k: (v if len(str(v)) < 400 else str(v)[:400] + "...") for k, v in (i or {}).items()}
            for i in parsed["items"]
        ],
        "answer": answer,
        "stderr_tail": proc.stderr[-1500:],
        "owner_canaries_found": leaks,
        "codex_home_files": sorted(
            str(p.relative_to(codex_home)) for p in codex_home.rglob("*") if p.is_file()
        )[:60],
    }
    (scratch / f"codex-probe-{a.variant}.json").write_text(json.dumps(record, indent=2))
    print(
        json.dumps(
            {k: record[k] for k in ("variant", "exit", "wall_s", "usage", "owner_canaries_found")},
            indent=2,
        )
    )
    print("ANSWER:\n" + answer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
