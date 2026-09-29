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

"""SkillOpt runs on gac-bench: registration, the initial skill document, the spend preflight.

    python -m gac_skillopt.run eval  --config configs/claude.yaml --skill eval --split valid_seen --n 2
    python -m gac_skillopt.run train --config configs/claude.yaml --skill eval [--cfg-options k=v ...]

``eval`` is SkillOpt's evaluation path (``scripts/eval_only.py``: the adapter's ``rollout`` on
one split and ``compute_score``); ``train`` is ``scripts/train.py`` (``ReflACTTrainer``). Both
get the ``gac_skills`` environment registered, the shipped ``SKILL.md`` body as the initial
document, and ``env.skill``/``env.scratch`` set. Codex runs are refused unless the ledger has
room for ``env.max_usd``.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .adapter import GacSkillsAdapter
from .budget import Ledger
from .paths import LONG, TOOL_DIR, Bench
from .tasks import initial_body


def _register() -> None:
    import scripts.eval_only as eval_only
    import scripts.train as train

    train._ENV_REGISTRY["gac_skills"] = GacSkillsAdapter
    eval_only._ENV_REGISTRY["gac_skills"] = GacSkillsAdapter


def _read_cfg(path: str, overrides: list[str]) -> dict:
    from skillopt.config import flatten_config, is_structured, load_config

    cfg = load_config(path, overrides=overrides)
    return flatten_config(cfg) if is_structured(cfg) else cfg


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m gac_skillopt.run")
    ap.add_argument("mode", choices=["eval", "train"])
    ap.add_argument("--config", required=True)
    ap.add_argument("--skill", required=True, help="eval, scaffold, ... or the full skill name")
    ap.add_argument("--scratch", help="default: GAC_SKILLOPT_SCRATCH")
    ap.add_argument(
        "--split", default="valid_seen", help="eval: train | valid_seen | valid_unseen | all"
    )
    ap.add_argument("--n", type=int, default=0, help="eval: first N items of the split (0 = all)")
    ap.add_argument("--body", help="eval: a candidate body instead of the shipped SKILL.md")
    ap.add_argument(
        "--out", help="output directory (default: <scratch>/runs/<mode>-<skill>-<time>)"
    )
    ap.add_argument("--cfg-options", nargs="*", default=[])
    args = ap.parse_args(argv)

    skill = LONG.get(args.skill, args.skill)
    bench = Bench.from_env(args.scratch)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = Path(
        args.out or bench.runs / f"{args.mode}-{skill.removeprefix('graph-agents-cli-')}-{stamp}"
    )
    out.mkdir(parents=True, exist_ok=True)
    body_file = out / "skill_init.md"
    body_file.write_text(Path(args.body).read_text() if args.body else initial_body(skill))
    config = str(
        Path(args.config)
        if Path(args.config).is_absolute()
        else (Path.cwd() / args.config).resolve()
    )
    overrides = [f"env.skill={skill}", f"env.scratch={bench.scratch}", *args.cfg_options]

    cfg = _read_cfg(config, overrides)
    if cfg.get("harness") == "codex":
        cap = float(cfg.get("max_usd") or 0)
        ok, message = Ledger.from_env().check(cap)
        print(f"  [gac_skills] ledger: {message}")
        if not ok:
            print("  [gac_skills] refusing: the ledger has no room for this run's max_usd")
            return 1

    _register()
    try:
        _dispatch(args, config, body_file, out, overrides)
    finally:
        for adapter in GacSkillsAdapter.instances:
            adapter.close()
    print(f"  [gac_skills] outputs: {out}")
    return 0


def _dispatch(
    args: argparse.Namespace, config: str, body_file: Path, out: Path, overrides: list[str]
) -> None:
    if args.mode == "eval":
        import scripts.eval_only as eval_only

        sys.argv = [
            "eval_only",
            "--config",
            config,
            "--skill",
            str(body_file),
            "--split",
            args.split,
            "--out_root",
            str(out),
            "--cfg-options",
            *overrides,
            f"evaluation.test_env_num={args.n or 10000}",
        ]
        eval_only.main()
    else:
        import scripts.train as train

        from .optlog import instrument

        print(f"  [gac_skills] optimizer calls: {instrument(out)}")
        sys.argv = [
            "train",
            "--config",
            config,
            "--cfg-options",
            *overrides,
            f"env.skill_init={body_file}",
            f"env.out_root={out}",
            f"model.claude_code_exec_path={TOOL_DIR / 'bin' / 'claude-isolated'}",
        ]
        train.main()


if __name__ == "__main__":
    sys.exit(main())
