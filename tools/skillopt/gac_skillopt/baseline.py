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

"""Repeated evaluation of skill documents on the val and test splits, and its report.

    python -m gac_skillopt.baseline run --scratch S --harness claude --reps 3 --port-base 22100
    python -m gac_skillopt.baseline run --scratch S --harness codex --model gpt-5.6-terra \\
        --reps 1 --max-usd 15
    python -m gac_skillopt.baseline report --out S/runs/baseline --json results.json

``run`` executes every selected task once per repetition with the same rollout function the
SkillOpt adapter uses (``rollout_batch``: fixture, isolated harness, verifier, integrity,
cleanup), writing ``<out>/<harness>/rep<k>/predictions/<task>/`` per repetition. Without
``--body-dir`` the shipped skills are installed (the baseline); with it, ``<short name>.md``
files there replace the bodies of those skills (an optimised candidate, same report shape).
Codex repetitions are checked against the ledger before they start and per rollout while they
run (``RunBudget``). ``report`` aggregates the repetitions: per task, per skill and split
(``hard``, ``soft`` and SkillOpt's ``mixed`` gate metric, 0.5 hard + 0.5 soft), spend and
session counts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from . import isolation
from .budget import PRICES, PRICES_READ_ON, BudgetStop, Ledger, RunBudget
from .paths import LONG, SHORT, SKILLS, Bench
from .rollout import IsolationError, RolloutConfig, rollout_batch
from .tasks import all_tasks, load_split, skill_source, task_by_id

EVAL_SPLITS = ("val", "test")


def _selected(skills: list[str] | None, splits: list[str], ids: list[str] | None):
    wanted = [LONG.get(s, s) for s in skills] if skills else list(SKILLS)
    where: dict[str, str] = {}
    tasks = []
    for skill in wanted:
        split = load_split(skill)
        by_id = {t.id: t for t in all_tasks(skill)}
        for name in splits:
            for tid in split.get(name, []):
                where[tid] = name
                tasks.append(by_id[tid])
    if ids:
        tasks = [t for t in tasks if t.id in set(ids)]
        missing = set(ids) - {t.id for t in tasks}
        if missing:
            raise SystemExit(f"not in the selected skills/splits: {', '.join(sorted(missing))}")
    return tasks, where


def _bodies(body_dir: str | None) -> dict[str, str | None] | None:
    if not body_dir:
        return None
    out: dict[str, str | None] = {}
    for skill in SKILLS:
        path = Path(body_dir) / f"{SHORT[skill]}.md"
        if path.is_file():
            out[skill] = path.read_text()
    return out


def baseline_run_name(harness: str, rep: int) -> str:
    """The workspace directory name of one repetition. Unique per call: two baseline runs
    started in the same second (say, two candidate bodies at once) would otherwise share
    ``<workspace root>/<run>/<slot>-<task>/`` and delete each other's workspaces."""
    return f"base-{harness}-r{rep}-{time.strftime('%H%M%S')}-{uuid.uuid4().hex[:6]}"


def codex_home_name() -> str:
    """The scratch CODEX_HOME of one invocation, unique per call like ``baseline_run_name``: two
    invocations started in the same second shared one login, and the first to finish deleted it
    (``forget_codex_login``) under the other's remaining rollouts."""
    return f"baseline-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def skill_hashes() -> dict[str, str]:
    """sha256 (16 hex) of each shipped SKILL.md: which documents a baseline measured."""
    return {
        SHORT[s]: hashlib.sha256((skill_source(s) / "SKILL.md").read_bytes()).hexdigest()[:16]
        for s in SKILLS
    }


def cmd_run(args: argparse.Namespace) -> int:
    bench = Bench.from_env(args.scratch, port_base=args.port_base)
    isolation.ensure_bin(bench)
    splits = [s.strip() for s in args.splits.split(",") if s.strip()]
    tasks, where = _selected(args.skill, splits, args.task)
    if args.harness == "codex":
        if not args.model:
            raise SystemExit("--model is required for codex")
    bodies = _bodies(args.body_dir)
    mode = args.permission_mode or isolation.CLAUDE_PERMISSION_MODE
    if args.harness == "claude" and mode not in isolation.PERMISSION_MODES:
        raise SystemExit(f"--permission-mode must be one of {isolation.PERMISSION_MODES}")
    out_root = Path(args.out or bench.runs / "baseline") / args.harness
    out_root.mkdir(parents=True, exist_ok=True)
    meta = {
        "harness": args.harness,
        "model": args.model or ("sonnet" if args.harness == "claude" else ""),
        "effort": args.effort,
        # Claude Code only, always with the Bash sandbox from the workspace settings. Rounds 1
        # and 2 used acceptEdits; the mode changes what the permission gate refuses.
        "permission_mode": mode if args.harness == "claude" else "",
        "max_turns": args.max_turns,
        "cli": isolation.cli_version(bench),
        "skills": skill_hashes() if bodies is None else {"candidates": sorted(bodies)},
        "body_dir": args.body_dir or "",
        "splits": splits,
        "ports": [bench.port_base, bench.port_base + 25 + args.slots - 1],
    }
    codex_home = None
    budget = None
    ledger = Ledger.from_env()
    if args.harness == "codex":
        codex_home = bench.scratch / "codex" / codex_home_name()
        isolation.prepare_codex_home(bench, codex_home, model=args.model, effort=args.effort)
        budget = RunBudget(
            model=args.model,
            max_usd=args.max_usd,
            ledger=ledger,
            estimate_per_rollout=args.estimate,
        )
    status = 0
    try:
        for rep in range(args.rep_start, args.rep_start + args.reps):
            rep_dir = out_root / f"rep{rep}"
            if (rep_dir / "summary.json").exists() and not args.force:
                print(f"== rep {rep}: already done ({rep_dir}); --force reruns it")
                continue
            if args.harness == "codex":
                need = len(tasks) * args.estimate
                ok, message = ledger.check(need)
                print(f"== rep {rep}: ledger {message}")
                if not ok:
                    print("== stopping: the ledger has no room for this repetition")
                    status = 2
                    break
            run_name = baseline_run_name(args.harness, rep)
            cfg = RolloutConfig(
                harness=args.harness,
                model=meta["model"],
                effort=args.effort,
                max_turns=args.max_turns,
                slots=args.slots,
                keep_workspaces=args.keep,
                run_name=run_name,
                codex_home=codex_home,
                budget=budget,
                permission_mode=mode if args.harness == "claude" else None,
            )
            print(f"== rep {rep}: {len(tasks)} task(s), {args.slots} slot(s), run {run_name}")
            started = time.time()
            try:
                results = rollout_batch(bench, tasks, bodies, cfg, rep_dir)
            except (BudgetStop, IsolationError) as exc:
                print(f"== stopped: {exc}")
                status = 3
                break
            skill_of = {t.id: SHORT[t.skill] for t in tasks}
            for r in results:
                r["split"] = where.get(r["id"], "")
                r["skill"] = skill_of.get(r["id"], "")
            summary = {
                **meta,
                "rep": rep,
                "run_name": run_name,
                "wall_s": round(time.time() - started, 1),
                "codex_usd": round(budget.spent, 4) if budget else 0.0,
                "results": [
                    {k: v for k, v in r.items() if k not in ("task_description", "reference_text")}
                    for r in results
                ],
            }
            (rep_dir / "summary.json").write_text(json.dumps(summary, indent=1))
            hard = sum(r["hard"] for r in results) / max(len(results), 1)
            soft = sum(r["soft"] for r in results) / max(len(results), 1)
            print(f"== rep {rep}: hard {hard:.3f} soft {soft:.3f} ({summary['wall_s']} s)")
    finally:
        if codex_home is not None:
            isolation.forget_codex_login(codex_home)
    if budget is not None:
        print(f"codex spend this invocation: ${budget.spent:.4f} over {budget.rollouts} rollout(s)")
    return status


# ── rescore ─────────────────────────────────────────────────────────────────


def rescore_transcripts(out: Path) -> list[dict[str, Any]]:
    """Re-run every rollout's ``transcript`` checks under ``out`` with the current verifier and
    task definitions, and update ``result.json`` and the repetition's ``summary.json``.

    Transcript checks depend only on the recorded trace (the commands the agent ran and its
    final answer), so this is exact after a verifier fix; every other check keeps its recorded
    result. A rollout zeroed for integrity, isolation or infrastructure stays zero. Returns the
    rollouts whose score changed."""
    from .trace import Step, Trace
    from .verify import CheckResult, Verifier, fail_reason, score

    changed = []
    for summary_path in sorted(out.glob("*/rep*/summary.json")):
        summary = json.loads(summary_path.read_text())
        by_id = {r["id"]: r for r in summary["results"]}
        for pred in sorted((summary_path.parent / "predictions").iterdir()):
            result_path, trace_path = pred / "result.json", pred / "trace.json"
            if not (result_path.exists() and trace_path.exists()):
                continue
            result = json.loads(result_path.read_text())
            data = json.loads(trace_path.read_text())
            trace = Trace(
                harness=data.get("harness", ""),
                steps=[Step(**step) for step in data.get("steps", [])],
                final=data.get("final", ""),
            )
            verifier = Verifier.__new__(Verifier)
            verifier.trace = trace
            defs = {c["id"]: c for c in task_by_id(result["id"]).checks}
            checks = []
            for c in result.get("checks", []):
                if c["type"] == "transcript" and c["id"] in defs:
                    passed, detail, output = verifier._transcript(defs[c["id"]])
                    why = defs[c["id"]].get("why")
                    if not passed and why:
                        detail = f"{why} -- {detail}"
                    c = {**c, "passed": bool(passed), "detail": detail, "output": output[-1500:]}
                checks.append(c)
            objs = [
                CheckResult(
                    id=c["id"],
                    type=c["type"],
                    passed=c["passed"],
                    mandatory=c["mandatory"],
                    weight=c["weight"],
                    detail=c["detail"],
                    output=c.get("output", ""),
                )
                for c in checks
            ]
            hard, soft = score(objs)
            reason = fail_reason(objs)
            old_reason = result.get("fail_reason") or ""
            if old_reason.startswith(("integrity:", "isolation:", "infrastructure:")):
                hard, soft, reason = 0, 0.0, old_reason
            elif result.get("timed_out") and old_reason.startswith("timed out"):
                reason = old_reason.split("; ", 1)[0] + "; " + reason
            if (hard, soft) == (result["hard"], result["soft"]):
                continue
            note = {"hard": result["hard"], "soft": result["soft"], "fail_reason": old_reason}
            result.update({"hard": hard, "soft": soft, "fail_reason": reason, "checks": checks})
            result["failed_mandatory"] = [
                c["id"] for c in checks if c["mandatory"] and not c["passed"]
            ]
            result["checks_passed"] = sum(c["passed"] for c in checks)
            result.setdefault("rescored_from", note)
            result_path.write_text(json.dumps(result, indent=1))
            row = by_id.get(result["id"])
            if row is not None:
                row.update(
                    {k: result[k] for k in ("hard", "soft", "fail_reason", "failed_mandatory")}
                )
                row.setdefault("rescored_from", note)
            changed.append(
                {
                    "rollout": f"{summary_path.parent.parent.name}/{summary_path.parent.name}/{pred.name}",
                    "from": note,
                    "to": {"hard": hard, "soft": soft},
                }
            )
        summary_path.write_text(json.dumps(summary, indent=1))
    return changed


def cmd_rescore(args: argparse.Namespace) -> int:
    changed = rescore_transcripts(Path(args.out))
    print(json.dumps(changed, indent=1))
    print(f"{len(changed)} rollout(s) changed")
    return 0


# ── report ──────────────────────────────────────────────────────────────────


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 4) if xs else None


def _agg(rows: list[dict[str, Any]]) -> dict[str, Any]:
    hard = [float(r["hard"]) for r in rows]
    soft = [float(r["soft"]) for r in rows]
    h, s = _mean(hard), _mean(soft)
    return {
        "n": len(rows),
        "hard": h,
        "soft": s,
        "mixed": None if h is None or s is None else round(0.5 * h + 0.5 * s, 4),
    }


def _compact(r: dict[str, Any], rep: int) -> dict[str, Any]:
    usage = r.get("usage") or {}
    row = {
        "rep": rep,
        "hard": r["hard"],
        "soft": r["soft"],
        "failed": r.get("failed_mandatory", []),
        "reason": (r.get("fail_reason") or "")[:240],
        "skill_loaded": r.get("skill_loaded"),
        "turns": r.get("n_turns"),
        "agent_s": r.get("agent_wall_s"),
        "timed_out": r.get("timed_out", False),
        "attempts": r.get("attempts", 1),
        "infra": bool(r.get("infra_error")),
        "leftovers": len(r.get("leftovers") or []),
    }
    if r.get("leftovers"):
        row["leftover_notes"] = [str(n)[:240] for n in r["leftovers"]]
    if "permission_denials" in r:
        row["permission_denials"] = r["permission_denials"]
        if r.get("init", {}).get("permissionMode"):
            row["session_mode"] = r["init"]["permissionMode"]
    if "usd" in r:
        row["usd"] = r["usd"]
        row["tokens"] = {
            "in": usage.get("input_tokens", 0),
            "cached": usage.get("cached_input_tokens", 0),
            "out": usage.get("output_tokens", 0),
        }
    elif usage.get("api_equivalent_usd") is not None:
        row["api_equivalent_usd"] = round(float(usage["api_equivalent_usd"]), 4)
    return row


def build_report(out: Path) -> dict[str, Any]:
    report: dict[str, Any] = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "harnesses": {}}
    for hdir in sorted(p for p in out.iterdir() if p.is_dir()):
        reps = sorted(
            (int(p.name[3:]), json.loads((p / "summary.json").read_text()))
            for p in hdir.glob("rep*")
            if (p / "summary.json").exists()
        )
        if not reps:
            continue
        meta = {k: v for k, v in reps[0][1].items() if k not in ("results", "rep", "run_name")}
        meta.pop("wall_s", None)
        meta.pop("codex_usd", None)
        tasks: dict[str, dict[str, Any]] = {}
        flat: list[dict[str, Any]] = []
        for rep, summary in reps:
            for r in summary["results"]:
                t = tasks.setdefault(
                    r["id"],
                    {
                        "skill": r.get("skill", ""),
                        "split": r.get("split", ""),
                        "family": r.get("family", ""),
                        "reps": [],
                    },
                )
                t["reps"].append(_compact(r, rep))
                flat.append({**r, "rep": rep})
        skills: dict[str, dict[str, Any]] = {}
        for t in tasks.values():
            t.update(_agg(t["reps"]))
            entry = skills.setdefault(t["skill"], {})
            for key in (t["split"], "all"):
                entry.setdefault(key, []).extend(t["reps"])
        skills_agg = {
            s: {k: _agg(v) for k, v in sorted(e.items())} for s, e in sorted(skills.items())
        }
        overall = {
            key: _agg(
                [row for t in tasks.values() if key in (t["split"], "all") for row in t["reps"]]
            )
            for key in ("train", "val", "test", "all")
        }
        usd = sum(float(r.get("usd", 0.0)) for r in flat)
        api_eq = sum(
            float((r.get("usage") or {}).get("api_equivalent_usd") or 0.0)
            for r in flat
            if "usd" not in r
        )
        report["harnesses"][hdir.name] = {
            "meta": meta,
            "reps": [rep for rep, _ in reps],
            "wall_s": {str(rep): s.get("wall_s") for rep, s in reps},
            "overall": overall,
            "skills": skills_agg,
            "tasks": dict(sorted(tasks.items())),
            "totals": {
                "rollouts": len(flat),
                "sessions": sum(int(r.get("attempts", 1) or 1) for r in flat),
                "infra_failures": sum(1 for r in flat if r.get("infra_error")),
                "timeouts": sum(1 for r in flat if r.get("timed_out")),
                "skill_not_loaded": sum(1 for r in flat if r.get("skill_loaded") is False),
                "leftover_processes": sum(len(r.get("leftovers") or []) for r in flat),
                "permission_denials": sum(int(r.get("permission_denials") or 0) for r in flat),
                "rollouts_with_permission_denials": sum(
                    1 for r in flat if int(r.get("permission_denials") or 0) > 0
                ),
                "codex_usd_last_attempts": round(usd, 4),
                "claude_api_equivalent_usd": round(api_eq, 2),
            },
        }
    report["prices_usd_per_1m"] = {
        "read_on": PRICES_READ_ON,
        **{k: list(v) for k, v in PRICES.items()},
    }
    return report


def markdown_tables(report: dict[str, Any]) -> str:
    """Per-skill and per-task tables for the results page."""
    lines = []
    for harness, h in report["harnesses"].items():
        lines += [f"### {harness} ({h['meta'].get('model')}, reps {h['reps']})", ""]
        splits = [s for s in ("train", "val", "test") if h["overall"][s].get("n")]
        head = " | ".join(f"{s} hard | {s} soft" for s in splits)
        lines += [
            f"| Skill | {head} | all mixed |",
            "|---|" + "---|---|" * len(splits) + "---|",
        ]
        for skill, agg in h["skills"].items():
            cells = " | ".join(
                f"{_fmt(agg.get(s, {}).get('hard'))} | {_fmt(agg.get(s, {}).get('soft'))}"
                for s in splits
            )
            lines.append(f"| {skill} | {cells} | {_fmt(agg.get('all', {}).get('mixed'))} |")
        o = h["overall"]
        cells = " | ".join(f"{_fmt(o[s].get('hard'))} | {_fmt(o[s].get('soft'))}" for s in splits)
        lines.append(f"| **all** | {cells} | {_fmt(o['all'].get('mixed'))} |")
        lines += [
            "",
            "| Task | Skill | Split | hard per rep | soft per rep |",
            "|---|---|---|---|---|",
        ]
        for tid, t in h["tasks"].items():
            hs = " ".join(str(r["hard"]) for r in t["reps"])
            ss = " ".join(f"{r['soft']:.2f}" for r in t["reps"])
            lines.append(f"| `{tid}` | {t['skill']} | {t['split']} | {hs} | {ss} |")
        lines.append("")
    return "\n".join(lines)


def _fmt(x: float | None) -> str:
    return "-" if x is None else f"{x:.2f}"


def cmd_report(args: argparse.Namespace) -> int:
    out = Path(args.out)
    report = build_report(out)
    text = json.dumps(report, indent=1)
    if args.json:
        Path(args.json).write_text(text + "\n")
    if args.md:
        Path(args.md).write_text(markdown_tables(report) + "\n")
    if not args.json and not args.md:
        print(markdown_tables(report))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m gac_skillopt.baseline")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--scratch")
    p.add_argument("--harness", required=True, choices=["claude", "codex"])
    p.add_argument("--model", default="")
    p.add_argument("--effort", default="medium")
    p.add_argument("--max-turns", type=int, default=80)
    p.add_argument(
        "--permission-mode",
        choices=isolation.PERMISSION_MODES,
        help=f"Claude Code rollouts (default {isolation.CLAUDE_PERMISSION_MODE}); the Bash "
        "sandbox applies in both",
    )
    p.add_argument("--reps", type=int, default=1)
    p.add_argument("--rep-start", type=int, default=1)
    p.add_argument("--splits", default=",".join(EVAL_SPLITS))
    p.add_argument("--skill", action="append", help="short or full name; default: all six")
    p.add_argument("--task", action="append", help="only these tasks (of the selected splits)")
    p.add_argument("--slots", type=int, default=4)
    p.add_argument("--port-base", type=int)
    p.add_argument("--out", help="default: <scratch>/runs/baseline")
    p.add_argument("--body-dir", help="<short name>.md candidate bodies (default: shipped skills)")
    p.add_argument("--max-usd", type=float, default=15.0, help="Codex spend cap per invocation")
    p.add_argument("--estimate", type=float, default=0.60, help="Codex USD per rollout, reserved")
    p.add_argument("--keep", action="store_true", help="keep workspaces")
    p.add_argument("--force", action="store_true", help="rerun repetitions that already exist")
    p = sub.add_parser("rescore", help="re-run transcript checks on recorded traces")
    p.add_argument("--out", required=True)
    p = sub.add_parser("report")
    p.add_argument("--out", required=True, help="the run root (holds <harness>/rep<k>/)")
    p.add_argument("--json")
    p.add_argument("--md")
    args = ap.parse_args(argv)
    return {"run": cmd_run, "rescore": cmd_rescore, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
