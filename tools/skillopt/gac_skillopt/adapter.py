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

"""The SkillOpt environment ``gac_skills``: one graph-agents-cli skill as the trainable document.

``GacSkillsAdapter.rollout()`` runs each item (a benchmark task) on Claude Code or Codex with
the candidate installed as the real skill, scores it with the deterministic verifier and writes
``predictions/<id>/conversation.json`` for reflection (DESIGN section 4). Registration is by
``gac_skillopt.run``, which adds the class to SkillOpt's environment registries.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, ClassVar

from skillopt.datasets.base import BatchSpec, SplitDataLoader
from skillopt.envs.base import EnvAdapter

from . import isolation
from .budget import Ledger, RunBudget
from .factcheck import check_candidate
from .paths import LONG, SHORT, SPLITS_DIR, Bench
from .rollout import RolloutConfig, item_reps, rollout_repeated, tag_selection_items
from .tasks import SPLITS, all_tasks, split_skill, task_by_id

# Added to SkillOpt's generic analyst prompts (DESIGN section 4.5).
ANALYST_ADDENDUM = """

## About this skill document

The document is the body of a coding-agent skill for graph-agents-cli, a CLI for building,
evaluating and deploying LangGraph agents on Kubernetes (the frontmatter is held fixed and not
shown). The agent loads it on its own when a task matches, and may read the files under
`references/` that it names. Trajectories come from Claude Code or Codex sessions scored by a
deterministic verifier; `[verification]` lines are the verifier's checks.

Rules for edits:
- Keep the `## Not covered by this skill` and `## Migration note` sections.
- Never invent graph-agents-cli commands, flags, environment variables, file names or exit codes:
  a fact-check rejects any candidate that names a command or flag the CLI does not have.
- Prefer `replace` or `insert_after` in the section that owns the topic over `append` (an append
  lands after `## Migration note`).
- Keep edits general: never copy a task's specific values (project names, ids, URLs) into the
  document; state the rule the agent missed.
- Do not grow the document more than the edit needs; agents read all of it.
"""


class GacTaskLoader(SplitDataLoader):
    """Items from ``tasks/<skill>/`` assigned by ``splits/<skill>.json`` (not split directories)."""

    def __init__(self, skill: str, split_file: str = "", seed: int = 42, limit: int = 0) -> None:
        super().__init__(split_dir="", split_mode="split_dir", seed=seed, limit=limit)
        self.skill = LONG.get(skill, skill)
        self.split_file = split_file

    def setup(self, cfg: dict) -> None:
        import json

        path = (
            Path(self.split_file) if self.split_file else SPLITS_DIR / f"{SHORT[self.skill]}.json"
        )
        split = json.loads(path.read_text())
        tasks = {t.id: t for t in all_tasks(self.skill)}
        for name in SPLITS:
            items = [tasks[tid].item() for tid in split.get(name, [])]
            self._splits[name] = items[: self.limit] if self.limit else items
        counts = " ".join(f"{k}={len(v)}" for k, v in self._splits.items())
        print(f"  [GacTaskLoader] {self.skill}: {counts} (from {path})")

    def load_split_items(self, split_path: str) -> list[dict]:  # pragma: no cover - unused
        raise NotImplementedError("GacTaskLoader reads splits/<skill>.json")


class GacSkillsAdapter(EnvAdapter):
    # Every adapter the process created, so the runner can close them (delete the Codex login).
    instances: ClassVar[list[GacSkillsAdapter]] = []

    def __init__(
        self,
        skill: str = "graph-agents-cli-eval",
        harness: str = "claude",
        harness_model: str = "",
        harness_effort: str = "medium",
        harness_max_turns: int = 80,
        harness_permission_mode: str = "",
        val_reps: int = 1,
        scratch: str = "",
        slots: int = 4,
        port_base: int = 22050,
        task_timeout_s: int = 0,
        max_usd: float = 0.0,
        split_file: str = "",
        factcheck: bool = True,
        keep_workspaces: bool = False,
        seed: int = 42,
        limit: int = 0,
        analyst_workers: int = 4,
        failure_only: bool = False,
        minibatch_size: int = 3,
        edit_budget: int = 3,
    ) -> None:
        self.skill = LONG.get(skill, skill)
        self.harness = harness
        self.harness_model = harness_model or ("sonnet" if harness == "claude" else "")
        self.harness_effort = harness_effort
        self.harness_max_turns = harness_max_turns
        self.harness_permission_mode = harness_permission_mode or (
            isolation.CLAUDE_PERMISSION_MODE if harness == "claude" else ""
        )
        if harness == "claude" and self.harness_permission_mode not in isolation.PERMISSION_MODES:
            raise SystemExit(
                f"env.harness_permission_mode must be one of {isolation.PERMISSION_MODES}"
            )
        # Each selection (val) item runs this many times and the gate sees its mean: one
        # rollout per item moved the gate on a single flake in round 2.
        self.val_reps = max(1, int(val_reps or 1))
        self.scratch = scratch
        self.slots = slots
        self.port_base = port_base
        self.task_timeout_s = task_timeout_s
        self.max_usd = float(max_usd)
        self.factcheck = factcheck
        self.keep_workspaces = keep_workspaces
        self.analyst_workers = analyst_workers
        self.failure_only = failure_only
        self.minibatch_size = minibatch_size
        self.edit_budget = edit_budget
        self.dataloader = GacTaskLoader(self.skill, split_file=split_file, seed=seed, limit=limit)
        self.bench: Bench | None = None
        self.codex_home: Path | None = None
        self.budget: RunBudget | None = None
        self.run_id = time.strftime("%Y%m%d-%H%M%S")
        GacSkillsAdapter.instances.append(self)

    # ── lifecycle ──

    def setup(self, cfg: dict) -> None:
        super().setup(cfg)
        self.dataloader.setup(cfg)
        self.bench = Bench.from_env(self.scratch or None, port_base=int(self.port_base))
        isolation.ensure_bin(self.bench)
        if self.harness == "codex":
            if not self.harness_model:
                raise SystemExit("env.harness_model is required for codex")
            if self.max_usd <= 0:
                raise SystemExit("env.max_usd must be set for codex runs (the run's spend cap)")
            self.codex_home = self.bench.scratch / "codex" / f"{self.run_id}-{self.skill}"
            isolation.prepare_codex_home(
                self.bench, self.codex_home, model=self.harness_model, effort=self.harness_effort
            )
            self.budget = RunBudget(
                model=self.harness_model, max_usd=self.max_usd, ledger=Ledger.from_env()
            )

    def close(self) -> None:
        if self.codex_home is not None:
            isolation.forget_codex_login(self.codex_home)

    def get_dataloader(self):
        return self.dataloader

    def build_env_from_batch(self, batch: BatchSpec, **kwargs: Any) -> list[dict]:
        return tag_selection_items(
            list(batch.payload or []), phase=batch.phase, split=batch.split, reps=self.val_reps
        )

    def build_train_env(self, batch_size: int, seed: int, **kwargs: Any) -> list[dict]:
        batch = self.dataloader.build_train_batch(batch_size=batch_size, seed=seed, **kwargs)
        return self.build_env_from_batch(batch)

    def build_eval_env(self, env_num: int, split: str, seed: int, **kwargs: Any) -> list[dict]:
        batch = self.dataloader.build_eval_batch(env_num=env_num, split=split, seed=seed, **kwargs)
        return self.build_env_from_batch(batch)

    def get_task_types(self) -> list[str]:
        loader = self.dataloader
        types = {
            str(i.get("task_type"))
            for i in loader.train_items + loader.val_items + loader.test_items
        }
        return sorted(types) or ["gac"]

    # ── rollout ──

    def rollout(
        self, env_manager: Any, skill_content: str, out_dir: str, **kwargs: Any
    ) -> list[dict]:
        if self.bench is None:
            raise RuntimeError("setup() was not called")
        items: list[dict] = list(env_manager or [])
        tasks = [task_by_id(str(i["id"])) for i in items]
        body = (
            split_skill(skill_content)[1]
            if skill_content.lstrip().startswith("---")
            else skill_content
        )
        if self.factcheck:
            problems = check_candidate(self.skill, body, bench=self.bench)
            if problems:
                reason = "fact-check rejected the document: " + "; ".join(problems[:5])
                print(f"  [gac_skills] {reason}")
                return [
                    {
                        "id": t.id,
                        "hard": 0,
                        "soft": 0.0,
                        "task_type": t.task_type,
                        "task_description": t.prompt,
                        "reference_text": t.reference,
                        "fail_reason": reason,
                        "factcheck": problems,
                    }
                    for t in tasks
                ]
        cfg = RolloutConfig(
            harness=self.harness,
            model=self.harness_model,
            effort=self.harness_effort,
            max_turns=self.harness_max_turns,
            timeout_s=self.task_timeout_s or None,
            slots=self.slots,
            keep_workspaces=self.keep_workspaces,
            run_name=f"{self.run_id}-{Path(out_dir).name}",
            codex_home=self.codex_home,
            budget=self.budget,
            permission_mode=self.harness_permission_mode or None,
        )
        reps = item_reps(items)
        if reps > 1:
            print(f"  [gac_skills] {len(tasks)} selection item(s) x {reps} repetitions")
        return rollout_repeated(self.bench, tasks, body, cfg, Path(out_dir), reps)

    # ── analyst prompts ──

    def get_error_minibatch_prompt(self) -> str | None:
        base = super().get_error_minibatch_prompt() or ""
        return base + ANALYST_ADDENDUM

    def get_success_minibatch_prompt(self) -> str | None:
        base = super().get_success_minibatch_prompt() or ""
        return base + ANALYST_ADDENDUM
