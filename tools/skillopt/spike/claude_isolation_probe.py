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

"""Prove (or disprove) Claude Code isolation for SkillOpt rollouts (spike).

Each variant starts one ``claude -p`` session in a scratch workspace that holds exactly one
project skill (``gac-probe-skill``), asks the model to list its skills, tools, MCP servers and
any CLAUDE.md/AGENTS.md instructions, and records the session's own ``init`` event (the
authoritative list of what was loaded), hook events, the answer, usage, cost and wall time.

    python claude_isolation_probe.py --scratch DIR --variant isolated [--model sonnet]

Variants: ``default`` (no isolation flags; shows what leaks), ``isolated`` (the flags in
harness_env.CLAUDE_ISOLATION_FLAGS), ``safe_mode`` (``--safe-mode``), ``config_dir``
(``CLAUDE_CONFIG_DIR`` in scratch).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_env as he

PROBE_SKILL = """\
---
name: gac-probe-skill
description: Probe skill for the SkillOpt isolation check. Use when asked for the probe word.
---

# Probe skill

When asked for the probe word, answer exactly: PROBE-WORD-7G3K.
"""

PROMPT = (
    "Do not use any tools except the Skill tool if you need it. Answer in plain text with these "
    "sections: SKILLS: the exact names of every skill available to you (from your system "
    "prompt / Skill tool listing), or NONE. TOOLS: the exact names of every tool you can call. "
    "MCP: the names of any MCP servers or connectors, or NONE. INSTRUCTIONS: quote the first "
    "line of every CLAUDE.md, AGENTS.md or memory file whose contents you were given, or NONE. "
    "PROBE: if a skill tells you a probe word, give it."
)

# Names that exist only in the developer's configuration: any of them in the init event or the
# answer is a leak.
OWNER_CANARIES = (
    "google-agents-cli",
    "gstack",
    "vercel",
    "frontend-design",
    "claude_ai_",
    "claude-in-chrome",
    "iFabric",
    "Project continuity",
    "gh-axi",
    "herdr",
    "engineering:",
    "finance:",
    "anthropic-skills",
)


def run_variant(scratch: Path, variant: str, model: str) -> dict:
    work = scratch / f"work-{variant}"
    skill_dir = work / ".claude" / "skills" / "gac-probe-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(PROBE_SKILL)

    isolated = variant != "default"
    extra: tuple[str, ...] = ()
    env = he.claude_env(scratch, isolated=isolated)
    if variant == "safe_mode":
        extra = ("--safe-mode",)
    elif variant == "config_dir":
        cfg = scratch / "claude-config"
        cfg.mkdir(exist_ok=True)
        env["CLAUDE_CONFIG_DIR"] = str(cfg)
    if variant == "default":
        cmd = he.claude_cmd(
            PROMPT, model=model, isolated=False, extra=("--no-session-persistence",)
        )
    else:
        cmd = he.claude_cmd(PROMPT, model=model, isolated=True, extra=extra)

    t0 = time.time()
    proc = subprocess.run(cmd, cwd=work, env=env, capture_output=True, text=True, timeout=600)
    wall = round(time.time() - t0, 1)
    parsed = he.parse_stream_json(proc.stdout)
    init = parsed["init"]
    result = parsed["result"]
    answer = str(result.get("result") or "")
    init_text = json.dumps(init)
    leaks = sorted({c for c in OWNER_CANARIES if c.lower() in (init_text + answer).lower()})
    record = {
        "variant": variant,
        "cmd": [c if len(c) < 300 else c[:300] + "..." for c in cmd],
        "env_keys": sorted(env),
        "cwd": str(work),
        "exit": proc.returncode,
        "wall_s": wall,
        "init": {
            k: init.get(k)
            for k in (
                "cwd",
                "model",
                "tools",
                "mcp_servers",
                "slash_commands",
                "skills",
                "plugins",
                "agents",
                "apiKeySource",
                "output_style",
                "permissionMode",
                "claude_code_version",
                "memory_paths",
            )
            if k in init
        },
        "init_keys": sorted(init),
        "hook_events": parsed["hooks"],
        "tool_calls": parsed["tool_calls"],
        "answer": answer,
        "usage": result.get("usage"),
        "total_cost_usd": result.get("total_cost_usd"),
        "duration_ms": result.get("duration_ms"),
        "num_turns": result.get("num_turns"),
        "is_error": result.get("is_error"),
        "stderr_tail": proc.stderr[-1500:],
        "owner_canaries_found": leaks,
    }
    (scratch / f"probe-{variant}.json").write_text(json.dumps(record, indent=2))
    return record


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scratch", type=Path, required=True)
    ap.add_argument(
        "--variant", required=True, choices=["default", "isolated", "safe_mode", "config_dir"]
    )
    ap.add_argument("--model", default="sonnet")
    a = ap.parse_args()
    a.scratch.mkdir(parents=True, exist_ok=True)
    rec = run_variant(a.scratch.resolve(), a.variant, a.model)
    summary = {
        k: rec[k]
        for k in (
            "variant",
            "exit",
            "wall_s",
            "total_cost_usd",
            "num_turns",
            "owner_canaries_found",
        )
    }
    summary["skills"] = rec["init"].get("skills")
    summary["plugins"] = rec["init"].get("plugins")
    summary["mcp_servers"] = rec["init"].get("mcp_servers")
    summary["hooks_fired"] = len(rec["hook_events"])
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
