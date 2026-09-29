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

"""One rollout end to end (DESIGN section 4.3), and a batch of them on parallel slots.

fixture -> skill -> agent (or scripted solution) -> verifier -> integrity -> cleanup, writing
``<out_dir>/predictions/<task id>/`` (``conversation.json`` for SkillOpt's reflection,
``result.json``, ``trace.json``, the raw event stream) and returning SkillOpt's result dict.
"""

from __future__ import annotations

import json
import queue
import shutil
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import harness as h
from . import isolation
from . import workspace as wsmod
from .budget import BudgetStop, RunBudget
from .paths import Bench
from .tasks import Task
from .trace import claude_isolation_problems, conversation
from .verify import Verifier, fail_reason, score

HARNESSES = ("claude", "codex", "gold", "broken", "noop")


class IsolationError(RuntimeError):
    """A rollout saw more than the skill under test: every result of the run is suspect."""


@dataclass
class RolloutConfig:
    harness: str
    model: str = ""
    effort: str | None = "medium"
    max_turns: int | None = 80
    timeout_s: int | None = None  # default: the task's own
    slots: int = 4
    keep_workspaces: bool = False
    run_name: str = ""
    codex_home: Path | None = None
    budget: RunBudget | None = None
    infra_retries: int = 1
    # Claude Code only (isolation.PERMISSION_MODES); None: isolation.CLAUDE_PERMISSION_MODE.
    permission_mode: str | None = None
    log: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.harness not in HARNESSES:
            raise ValueError(f"harness must be one of {HARNESSES}")
        if self.harness == "claude" and self.permission_mode is None:
            self.permission_mode = isolation.CLAUDE_PERMISSION_MODE
        if not self.run_name:
            self.run_name = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


_print_lock = threading.Lock()


def _say(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def _run_agent(bench: Bench, ws: wsmod.Workspace, cfg: RolloutConfig) -> h.AgentRun:
    timeout = int(cfg.timeout_s or ws.task.timeout_s)
    if cfg.harness == "claude":
        return h.run_claude(
            bench,
            ws,
            model=cfg.model or "sonnet",
            effort=cfg.effort,
            max_turns=cfg.max_turns,
            timeout=timeout,
            permission_mode=cfg.permission_mode,
        )
    if cfg.harness == "codex":
        if cfg.codex_home is None:
            raise ValueError("codex rollouts need a prepared codex_home")
        return h.run_codex(
            bench, ws, cfg.codex_home, model=cfg.model, effort=cfg.effort, timeout=timeout
        )
    return h.run_solution(bench, ws, cfg.harness, timeout=timeout)


def rollout_one(
    bench: Bench,
    task: Task,
    body: str | None,
    cfg: RolloutConfig,
    slot: int,
    out_dir: Path,
    tag: str = "",
) -> dict[str, Any]:
    """Run ``task`` with the skill body ``body`` (``None``: the shipped skill). ``tag`` tells
    apart workspaces of the same task in one run (repetitions)."""
    pred = out_dir / "predictions" / task.id
    pred.mkdir(parents=True, exist_ok=True)
    started = time.time()
    record: dict[str, Any] = {
        "id": task.id,
        "task_type": task.task_type,
        "family": task.family,
        "task_description": task.prompt,
        "reference_text": task.reference,
        "harness": cfg.harness,
        "model": cfg.model,
        "hard": 0,
        "soft": 0.0,
    }
    ws = None
    attempt = 0
    try:
        while True:
            attempt += 1
            ws = wsmod.build(bench, task, cfg.run_name, slot, tag=tag)
            wsmod.install_skill(
                bench, ws, harness="codex" if cfg.harness == "codex" else "claude", body=body
            )
            reserved = False
            if cfg.harness == "codex" and cfg.budget is not None:
                cfg.budget.before()
                reserved = True
            try:
                run = _run_agent(bench, ws, cfg)
            except BaseException:
                if reserved and cfg.budget is not None:
                    cfg.budget.release()
                raise
            if cfg.harness == "codex" and cfg.budget is not None:
                record["usd"] = round(
                    cfg.budget.record(run.usage, note=f"{cfg.run_name} {task.id}"), 5
                )
            quota = run.quota_error if cfg.harness in ("claude", "codex") else ""
            if quota:
                record.update({"fail_reason": f"infrastructure: quota: {quota}"})
                (pred / "result.json").write_text(json.dumps(record, indent=1))
                raise BudgetStop(f"{task.id}: the API account has no quota left: {quota[:200]}")
            infra = run.infra_error if cfg.harness in ("claude", "codex") else ""
            if infra and attempt <= cfg.infra_retries:
                _say(f"  [{task.id}] infrastructure failure, retrying once: {infra[:160]}")
                time.sleep(30 if "limit" in infra.lower() else 5)
                wsmod.cleanup_processes(bench, ws)
                wsmod.remove(ws)
                continue
            break
        leftovers_agent = wsmod.cleanup_processes(bench, ws)
        leaks = claude_isolation_problems(run.trace, task.skill) if cfg.harness == "claude" else []
        integrity_now = wsmod.integrity_hashes(ws, "codex" if cfg.harness == "codex" else "claude")
        tampered = sorted(
            k
            for k in set(ws.skill_files) | set(integrity_now)
            if ws.skill_files.get(k) != integrity_now.get(k)
        )
        env = wsmod.harness_env(bench, ws, port=bench.verifier_port(slot))
        checks = Verifier(ws, env, run.trace).run(task.checks)
        leftovers_verifier = wsmod.cleanup_processes(bench, ws)
        hard, soft = score(checks)
        reason = fail_reason(checks)
        if tampered:
            hard, soft = 0, 0.0
            reason = f"integrity: the rollout changed {', '.join(tampered[:3])}"
        if infra:
            hard, soft = 0, 0.0
            reason = f"infrastructure: {infra[:300]}"
        if leaks:
            hard, soft = 0, 0.0
            reason = "isolation: the session loaded " + "; ".join(leaks)
        if run.timed_out:
            reason = f"timed out after {cfg.timeout_s or task.timeout_s}s; " + reason
        record.update(
            {
                "hard": hard,
                "soft": soft,
                "fail_reason": reason,
                "n_turns": run.trace.num_turns or len(run.trace.steps),
                "skill_loaded": run.trace.skill_loaded,
                "wall_s": round(time.time() - started, 1),
                "agent_wall_s": run.wall_s,
                "agent_exit": run.exit_code,
                "timed_out": run.timed_out,
                "infra_error": infra,
                "attempts": attempt,
                "usage": run.usage,
                "failed_mandatory": [c.id for c in checks if c.mandatory and not c.passed],
                "checks_passed": sum(c.passed for c in checks),
                "checks_total": len(checks),
                "leftovers": leftovers_agent + leftovers_verifier,
                "isolation": leaks,
                "permission_mode": cfg.permission_mode if cfg.harness == "claude" else "",
                "permission_denials": len(run.trace.permission_denials),
                "permission_denied": run.trace.permission_denials[:5],
                "init": {
                    k: run.trace.init.get(k)
                    for k in (
                        "model",
                        "skills",
                        "plugins",
                        "mcp_servers",
                        "hook_events",
                        "permissionMode",
                    )
                },
            }
        )
        conv = conversation(run.trace, skill=task.skill, checks=[c.to_json() for c in checks])
        (pred / "conversation.json").write_text(json.dumps(conv, indent=1))
        (pred / "trace.json").write_text(json.dumps(run.trace.to_json(), indent=1))
        (pred / "result.json").write_text(
            json.dumps(
                {**record, "checks": [c.to_json() for c in checks], "stderr_tail": run.stderr_tail},
                indent=1,
            )
        )
        if run.raw_path is not None and run.raw_path.exists():
            shutil.copy2(run.raw_path, pred / "events.jsonl")
        if leaks:
            raise IsolationError(f"{task.id}: {reason}")
    except (BudgetStop, IsolationError):
        raise
    except wsmod.FixtureError as exc:
        record.update({"fail_reason": f"infrastructure: fixture: {exc}", "infra_error": str(exc)})
        (pred / "result.json").write_text(json.dumps(record, indent=1))
    except Exception as exc:
        record.update(
            {"fail_reason": f"infrastructure: harness error: {exc}", "infra_error": repr(exc)}
        )
        (pred / "result.json").write_text(
            json.dumps({**record, "traceback": traceback.format_exc()}, indent=1)
        )
    finally:
        if ws is not None:
            wsmod.cleanup_processes(bench, ws)
            if not cfg.keep_workspaces:
                wsmod.remove(ws)
    return record


def _run_jobs(
    bench: Bench,
    jobs: list[tuple[Task, Path, str]],
    body: str | dict[str, str | None] | None,
    cfg: RolloutConfig,
) -> list[dict[str, Any]]:
    """Run ``(task, out_dir, tag)`` jobs ``cfg.slots`` at a time; results in job order."""
    slots: queue.Queue[int] = queue.Queue()
    for i in range(max(1, min(cfg.slots, 25))):
        slots.put(i)
    stop = threading.Event()

    def work(task: Task, out_dir: Path, tag: str) -> dict[str, Any]:
        if stop.is_set():
            return {
                "id": task.id,
                "hard": 0,
                "soft": 0.0,
                "fail_reason": "not run: the run stopped",
                "skipped": True,
            }
        slot = slots.get()
        try:
            candidate = body.get(task.skill) if isinstance(body, dict) else body
            result = rollout_one(bench, task, candidate, cfg, slot, out_dir, tag=tag)
            label = f"{task.id}{' ' + tag if tag else ''}"
            _say(
                f"  [{cfg.harness}] {label}: hard={result['hard']} soft={result['soft']:.2f}"
                + (f" ({result.get('fail_reason', '')[:140]})" if not result["hard"] else "")
            )
            return result
        except (BudgetStop, IsolationError):
            stop.set()
            raise
        finally:
            slots.put(slot)

    with ThreadPoolExecutor(max_workers=max(1, cfg.slots)) as pool:
        futures = [pool.submit(work, *job) for job in jobs]
        results = []
        first_stop: Exception | None = None
        for f in futures:
            try:
                results.append(f.result())
            except (BudgetStop, IsolationError) as exc:
                first_stop = first_stop or exc
    if first_stop is not None:
        raise first_stop
    return results


def rollout_batch(
    bench: Bench,
    tasks: list[Task],
    body: str | dict[str, str | None] | None,
    cfg: RolloutConfig,
    out_dir: Path,
) -> list[dict[str, Any]]:
    """Every task once, ``cfg.slots`` at a time; results in task order. ``body`` is one
    candidate for every task, or a mapping from skill name to candidate (absent or ``None``:
    the shipped skill) for a batch that mixes skills."""
    out_dir.mkdir(parents=True, exist_ok=True)
    return _run_jobs(bench, [(t, out_dir, "") for t in tasks], body, cfg)


def rollout_repeated(
    bench: Bench,
    tasks: list[Task],
    body: str | dict[str, str | None] | None,
    cfg: RolloutConfig,
    out_dir: Path,
    reps: int,
) -> list[dict[str, Any]]:
    """Every task ``reps`` times in one pool (the same wall time as one pass when there are
    enough slots), repetition ``k`` under ``<out_dir>/rep<k>/predictions/``; returns one result
    per task with the mean over its scored repetitions (``mean_over_reps``) and writes
    ``<out_dir>/val_reps.json`` with every repetition's score."""
    if reps <= 1:
        return rollout_batch(bench, tasks, body, cfg, out_dir)
    jobs = []
    for k in range(1, reps + 1):
        (out_dir / f"rep{k}").mkdir(parents=True, exist_ok=True)
        jobs += [(t, out_dir / f"rep{k}", f"rep{k}") for t in tasks]
    flat = _run_jobs(bench, jobs, body, cfg)
    per_rep = [flat[i * len(tasks) : (i + 1) * len(tasks)] for i in range(reps)]
    merged = mean_over_reps(per_rep)
    (out_dir / "val_reps.json").write_text(
        json.dumps(
            {
                "reps": reps,
                "items": [
                    {"id": m["id"], "hard": m["hard"], "soft": m["soft"], "reps": m["reps"]}
                    for m in merged
                ],
            },
            indent=1,
        )
    )
    return merged


# SkillOpt's names for the selection split (skillopt.datasets.base._SPLIT_ALIAS).
SELECTION_SPLITS = ("valid_seen", "selection", "val")
# The item key that carries the repetition count from the adapter's batch to its rollout.
REPS_KEY = "gac_reps"


def tag_selection_items(items: list[dict], *, phase: str, split: str, reps: int) -> list[dict]:
    """The items of an evaluation batch on the selection split, marked to run ``reps`` times
    (``env.val_reps``). Train batches and the test split run once."""
    if reps > 1 and phase == "eval" and split in SELECTION_SPLITS:
        return [{**i, REPS_KEY: reps} for i in items]
    return items


def item_reps(items: list[dict]) -> int:
    """How many times each of these items runs: the mark ``tag_selection_items`` left, else 1."""
    return max([int(i.get(REPS_KEY) or 1) for i in items] or [1])


def _infra_zeroed(result: dict[str, Any]) -> bool:
    """A repetition scored 0 for the infrastructure (rate limit, API error, fixture), not for
    the agent."""
    return bool(result.get("infra_error")) or str(result.get("fail_reason") or "").startswith(
        "infrastructure:"
    )


def mean_over_reps(per_rep: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """One result per task from its repetitions (``per_rep[k][i]`` is task ``i`` in repetition
    ``k``): ``hard`` and ``soft`` are the means over the repetitions the agent was scored on.

    A repetition zeroed by the infrastructure is left out of the mean, as a single-rollout gate
    would have retried it; only when every repetition was, the task scores 0 with that reason.
    The other fields come from the first scored repetition; ``reps`` lists every repetition's
    score, ``fail_reason`` names each repetition that failed."""
    if not per_rep:
        return []
    merged = []
    for i in range(len(per_rep[0])):
        runs = [rep[i] for rep in per_rep]
        scored = [r for r in runs if not _infra_zeroed(r)]
        use = scored or runs
        base = dict(use[0])
        if scored:
            base["hard"] = round(sum(float(r["hard"]) for r in scored) / len(scored), 4)
            base["soft"] = round(sum(float(r["soft"]) for r in scored) / len(scored), 4)
        else:
            base["hard"], base["soft"] = 0, 0.0
        reasons = [
            f"rep{k + 1}: {r.get('fail_reason', '')}"
            for k, r in enumerate(runs)
            if float(r["hard"]) < 1 and r.get("fail_reason")
        ]
        base["fail_reason"] = " | ".join(reasons)
        base["reps"] = [
            {
                "rep": k + 1,
                "hard": r["hard"],
                "soft": r["soft"],
                "infra": _infra_zeroed(r),
                "fail_reason": str(r.get("fail_reason") or "")[:300],
            }
            for k, r in enumerate(runs)
        ]
        base["reps_scored"] = len(scored)
        merged.append(base)
    return merged
