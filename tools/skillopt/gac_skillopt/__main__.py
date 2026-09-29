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

"""gac-bench command line (no SkillOpt needed; SkillOpt runs are ``gac_skillopt.run``).

python -m gac_skillopt setup      --scratch DIR            build the CLI, warm the caches
python -m gac_skillopt validate                            task schemas and frozen splits
python -m gac_skillopt list       [--skill S]              tasks with their split
python -m gac_skillopt selfcheck  --scratch DIR [--skill S] [--task ID]...
                                  gold must score hard=1, broken and noop hard=0
python -m gac_skillopt rollout    --scratch DIR --harness claude|codex --task ID...
                                  direct rollouts of the shipped skill (no SkillOpt)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import isolation
from .paths import SHORT, SKILLS, Bench
from .rollout import RolloutConfig, rollout_batch
from .tasks import SPLITS, all_tasks, load_split, split_problems


def _tasks(args: argparse.Namespace):
    tasks = all_tasks(args.skill) if getattr(args, "skill", None) else all_tasks()
    if getattr(args, "task", None):
        wanted = set(args.task)
        tasks = [t for t in tasks if t.id in wanted]
        missing = wanted - {t.id for t in tasks}
        if missing:
            raise SystemExit(f"unknown task(s): {', '.join(sorted(missing))}")
    if getattr(args, "split", None):
        ids = set()
        for skill in {t.skill for t in tasks}:
            ids |= set(load_split(skill)[args.split])
        tasks = [t for t in tasks if t.id in ids]
    return tasks


def cmd_setup(args: argparse.Namespace) -> int:
    bench = Bench.from_env(args.scratch, port_base=getattr(args, "port_base", None))
    isolation.ensure_bin(bench, rebuild=args.rebuild_cli, on_stale="rebuild")
    print("graph-agents-cli:", isolation.cli_version(bench))
    print("project python:", isolation.project_python(bench))
    # Warm the shared uv cache with one project of each runtime.
    from .tasks import Task
    from .workspace import build, remove

    for runtime in ("fastapi", "langgraph-server"):
        task = Task(
            id=f"warm-{runtime}",
            skill=SKILLS[0],
            family="warm",
            task_type="warm",
            prompt="",
            fixture={
                "kind": "project",
                "name": "warm",
                "create_args": ["--runtime", runtime, "--registry", "ghcr.io/warm"],
            },
            checks=[],
            reference="",
            dir=Path("/nonexistent"),
        )
        t0 = time.time()
        ws = build(bench, task, "setup", 0)
        print(f"warmed {runtime}: {time.time() - t0:.1f}s")
        remove(ws)
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    tasks = all_tasks()
    problems = split_problems()
    for t in tasks:
        for kind in ("gold", "broken"):
            if not (t.dir / f"{kind}.sh").is_file():
                problems.append(f"{t.id}: missing {kind}.sh")
    by_skill = {s: sum(1 for t in tasks if t.skill == s) for s in SKILLS}
    print(json.dumps({"tasks": len(tasks), "by_skill": by_skill}, indent=1))
    for p in problems:
        print("PROBLEM:", p)
    return 1 if problems else 0


def cmd_list(args: argparse.Namespace) -> int:
    for skill in [args.skill] if args.skill else SKILLS:
        split = load_split(skill)
        where = {tid: name for name in SPLITS for tid in split.get(name, [])}
        for t in all_tasks(skill):
            print(f"{SHORT.get(t.skill, t.skill):15} {where.get(t.id, '-'):5} {t.family:28} {t.id}")
    return 0


def cmd_selfcheck(args: argparse.Namespace) -> int:
    bench = Bench.from_env(args.scratch, port_base=getattr(args, "port_base", None))
    isolation.ensure_bin(bench)
    tasks = _tasks(args)
    kinds = args.kinds.split(",")
    out_root = bench.runs / f"selfcheck-{time.strftime('%Y%m%d-%H%M%S')}"
    table: dict[str, dict[str, dict]] = {t.id: {} for t in tasks}
    for kind in kinds:
        cfg = RolloutConfig(
            harness=kind,
            slots=args.slots,
            keep_workspaces=args.keep,
            run_name=f"{out_root.name}-{kind}",
        )
        print(f"== {kind}: {len(tasks)} task(s)")
        for result in rollout_batch(bench, tasks, None, cfg, out_root / kind):
            table[result["id"]][kind] = result
    bad = []
    by_id = {t.id: t for t in tasks}
    for tid, row in table.items():
        for kind, result in row.items():
            want = 1 if kind == "gold" else 0
            if result.get("infra_error") or result["hard"] != want:
                bad.append((tid, kind, result["hard"], result.get("fail_reason", "")))
            elif kind == "gold" and (result.get("agent_exit") != 0 or result["soft"] != 1.0):
                bad.append(
                    (
                        tid,
                        kind,
                        result["hard"],
                        f"gold exited {result.get('agent_exit')}, soft {result['soft']}",
                    )
                )
            elif kind == "broken":
                failed = set(result.get("failed_mandatory", []))
                if failed != set(by_id[tid].broken_fails):
                    bad.append(
                        (
                            tid,
                            kind,
                            0,
                            f"broken failed {sorted(failed)}, expected {sorted(by_id[tid].broken_fails)}",
                        )
                    )
    summary = {
        "out": str(out_root),
        "tasks": len(tasks),
        "rows": {
            tid: {
                k: {"hard": r["hard"], "soft": r["soft"], "wall_s": r.get("wall_s")}
                for k, r in row.items()
            }
            for tid, row in table.items()
        },
        "bad": bad,
    }
    (out_root / "selfcheck.json").write_text(json.dumps(summary, indent=1))
    print(f"\n{'task':34} " + " ".join(f"{k:>12}" for k in kinds))
    for tid, row in table.items():
        cells = []
        for k in kinds:
            r = row.get(k)
            cells.append(
                f"{'-' if r is None else str(r['hard']) + '/' + format(r['soft'], '.2f'):>12}"
            )
        print(f"{tid:34} " + " ".join(cells))
    for tid, kind, hard, reason in bad:
        print(f"WRONG: {tid} {kind} hard={hard}: {reason[:300]}")
    print(f"results: {out_root}")
    return 1 if bad else 0


def cmd_preflight(args: argparse.Namespace) -> int:
    """Re-prove the Claude Code isolation before a run (DESIGN section 3.4) in the rollouts'
    permission mode: the session lists only the skill under test and runs in the requested mode;
    writes outside the workspace are denied (shell, Write and Edit); the key directory, the
    checkout and other runs cannot be read (shell, Read, Grep and Glob); the network reaches
    PyPI only; ``dangerouslyDisableSandbox`` does not leave the sandbox; the session cannot
    change its own sandbox settings; no other agent CLI is on PATH."""
    from . import preflight

    bench = Bench.from_env(args.scratch, port_base=getattr(args, "port_base", None))
    isolation.ensure_bin(bench)
    mode = args.permission_mode or isolation.CLAUDE_PERMISSION_MODE
    if isolation.key_file() is None:
        print(
            "note: GAC_SKILLOPT_OPENAI_KEY_FILE is not set, so the key directory is neither "
            "denied nor probed"
        )
    out = (
        Path(args.out)
        if args.out
        else bench.runs / f"preflight-{time.strftime('%Y%m%d-%H%M%S')}-{mode}"
    )
    report = preflight.run(
        bench, skill=args.skill_name, model=args.model, permission_mode=mode, out=out
    )
    summary = {
        "permission_mode": mode,
        "init": report.get("init"),
        "checks": {
            name: ("ok" if c["ok"] else ("FAILED" if c["attempted"] else "NOT ATTEMPTED"))
            + ("" if c["gating"] else " (not gating)")
            for name, c in report.get("checks", {}).items()
        },
        "outside_files_that_existed": report.get("outside_files_that_existed"),
        "report": str(out / "report.json"),
    }
    print(json.dumps(summary, indent=1))
    print("preflight:", "ok" if report["ok"] else "FAILED")
    return 0 if report["ok"] else 1


def cmd_rollout(args: argparse.Namespace) -> int:
    from .budget import Ledger, RunBudget

    bench = Bench.from_env(args.scratch, port_base=getattr(args, "port_base", None))
    isolation.ensure_bin(bench)
    tasks = _tasks(args)
    body = Path(args.body).read_text() if args.body else None
    cfg = RolloutConfig(
        harness=args.harness,
        model=args.model,
        effort=args.effort,
        slots=args.slots,
        keep_workspaces=args.keep,
        timeout_s=args.timeout,
        permission_mode=args.permission_mode if args.harness == "claude" else None,
    )
    if args.harness == "codex":
        if not args.model:
            raise SystemExit("--model is required for codex")
        codex_home = bench.scratch / "codex" / cfg.run_name
        isolation.prepare_codex_home(
            bench, codex_home, model=args.model, effort=args.effort or "medium"
        )
        cfg.codex_home = codex_home
        cfg.budget = RunBudget(model=args.model, max_usd=args.max_usd, ledger=Ledger.from_env())
    out = bench.runs / f"rollout-{cfg.run_name}"
    try:
        results = rollout_batch(bench, tasks, body, cfg, out)
    finally:
        if cfg.codex_home is not None:
            isolation.forget_codex_login(cfg.codex_home)
    hard = sum(r["hard"] for r in results) / max(len(results), 1)
    soft = sum(r["soft"] for r in results) / max(len(results), 1)
    spent = cfg.budget.spent if cfg.budget else 0.0
    print(
        json.dumps(
            {"out": str(out), "hard": hard, "soft": soft, "codex_usd": round(spent, 4)}, indent=1
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m gac_skillopt")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("setup")
    p.add_argument("--scratch")
    p.add_argument("--rebuild-cli", action="store_true")
    p = sub.add_parser("preflight")
    p.add_argument("--scratch")
    p.add_argument("--model", default="sonnet")
    p.add_argument("--skill-name", default="graph-agents-cli-workflow")
    p.add_argument("--permission-mode", choices=isolation.PERMISSION_MODES)
    p.add_argument("--out", help="events.jsonl and report.json (default: <scratch>/runs/...)")
    sub.add_parser("validate")
    p = sub.add_parser("list")
    p.add_argument("--skill")
    for name in ("selfcheck", "rollout"):
        p = sub.add_parser(name)
        p.add_argument("--scratch")
        p.add_argument("--skill")
        p.add_argument("--task", action="append")
        p.add_argument("--split", choices=SPLITS)
        p.add_argument("--slots", type=int, default=4)
        p.add_argument("--keep", action="store_true", help="keep workspaces for inspection")
    sub.choices["selfcheck"].add_argument("--kinds", default="gold,broken,noop")
    p = sub.choices["rollout"]
    p.add_argument("--harness", required=True, choices=["claude", "codex"])
    p.add_argument("--model", default="")
    p.add_argument("--effort", default="medium")
    p.add_argument("--timeout", type=int)
    p.add_argument("--body", help="a candidate SKILL.md body (default: the shipped skill)")
    p.add_argument("--max-usd", type=float, default=5.0, help="Codex spend cap for this command")
    p.add_argument("--permission-mode", choices=isolation.PERMISSION_MODES)
    for name in ("setup", "preflight", "selfcheck", "rollout"):
        sub.choices[name].add_argument(
            "--port-base",
            type=int,
            help="slot i uses base+i and base+25+i (default: GAC_SKILLOPT_PORT_BASE or 22050)",
        )
    args = ap.parse_args(argv)
    return {
        "setup": cmd_setup,
        "preflight": cmd_preflight,
        "validate": cmd_validate,
        "list": cmd_list,
        "selfcheck": cmd_selfcheck,
        "rollout": cmd_rollout,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
