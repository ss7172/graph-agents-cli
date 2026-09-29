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

"""What Codex rollouts cost, and the spend limits a run must stay under (DESIGN section 8.3).

Claude Code rollouts run on the developer's plan and cost nothing here (their API-equivalent
figure is recorded for information). Codex rollouts are billed to an OpenAI key: each one's
usage is priced, added to the run's total, and appended to an external ledger when one is
configured (``GAC_SKILLOPT_LEDGER``: a ``spend.py`` with ``add`` and ``check`` subcommands).
Before each Codex rollout the run refuses to start another when its own cap (``max_usd``) or
the ledger's stop-at would be crossed.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

# USD per 1M tokens (input, cached input, output), read from
# https://developers.openai.com/api/docs/pricing on 2026-09-27. Update with the date when prices
# change; a model missing here cannot run (its cost could not be accounted).
# Cache writes (the uncached input the API stores in its prompt cache, which Codex reports as
# `cache_write_input_tokens`) have their own, higher price: $2.50 for gpt-5.6-terra (short
# context), read on 2026-09-28. They used to be priced as plain input, which undercounted a Codex
# rollout by about 13 %. Models without a recorded cache-write price are charged 1.25 x input.
PRICES_READ_ON = "2026-09-28"
CACHE_WRITE_PRICES: dict[str, float] = {"gpt-5.6-terra": 2.50}
PRICES: dict[str, tuple[float, float, float]] = {
    "gpt-6-astra": (10.00, 1.00, 50.00),
    "gpt-6-sol": (2.00, 0.20, 10.00),
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "gpt-5.6-sol": (4.00, 0.40, 20.00),
    "gpt-5.6-terra": (2.00, 0.20, 12.00),
    "gpt-5.5": (5.00, 0.50, 30.00),
    "gpt-5.4": (2.50, 0.25, 15.00),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
}


class BudgetStop(RuntimeError):
    """A rollout would cross a spend limit: the run stops (SkillOpt can resume it later)."""


def usd(model: str, usage: dict[str, int]) -> float:
    if model not in PRICES:
        raise BudgetStop(f"no price recorded for {model}; add it to budget.PRICES first")
    pin, pcached, pout = PRICES[model]
    pwrite = CACHE_WRITE_PRICES.get(model, 1.25 * pin)
    cached = int(usage.get("cached_input_tokens", 0))
    uncached = max(int(usage.get("input_tokens", 0)) - cached, 0)
    written = min(int(usage.get("cache_write_input_tokens", 0)), uncached)
    return (
        (uncached - written) * pin
        + written * pwrite
        + cached * pcached
        + int(usage.get("output_tokens", 0)) * pout
    ) / 1e6


@dataclass
class Ledger:
    """The programme's shared ledger (``spend.py``), or none."""

    script: Path | None
    agent: str = "gac-bench"
    track: str = "skillopt"

    @classmethod
    def from_env(cls, agent: str = "gac-bench") -> Ledger:
        value = os.environ.get("GAC_SKILLOPT_LEDGER", "")
        return cls(Path(value) if value else None, agent=agent)

    def check(self, need: float) -> tuple[bool, str]:
        """The ledger's verdict on spending ``need`` more: the programme's stop-at and, with a
        track, that track's own cap (without ``--track`` only the stop-at was checked)."""
        if self.script is None:
            return True, "no ledger configured"
        cmd = [sys.executable, str(self.script), "check", "--need", f"{need:.4f}"]
        if self.track:
            cmd += ["--track", self.track]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        return proc.returncode == 0, proc.stdout.strip()

    def add(self, *, model: str, usage: dict[str, int], cost: float, note: str) -> str:
        if self.script is None:
            return ""
        # Rounded up to the next tenth of a cent: estimates stay conservative.
        rounded = int(cost * 1000 + 0.999999) / 1000
        proc = subprocess.run(
            [
                sys.executable,
                str(self.script),
                "add",
                "--agent",
                self.agent,
                "--track",
                self.track,
                "--model",
                model,
                "--input",
                str(int(usage.get("input_tokens", 0))),
                "--cached",
                str(int(usage.get("cached_input_tokens", 0))),
                "--output",
                str(int(usage.get("output_tokens", 0))),
                "--usd",
                f"{rounded:.4f}",
                "--note",
                note
                + (
                    f" (cache writes {int(usage['cache_write_input_tokens'])})"
                    if usage.get("cache_write_input_tokens")
                    else ""
                ),
            ],
            capture_output=True,
            text=True,
        )
        return proc.stdout.strip()


@dataclass
class RunBudget:
    """One run's Codex spend against its cap and the ledger."""

    model: str
    max_usd: float
    ledger: Ledger
    estimate_per_rollout: float = 0.60
    spent: float = 0.0
    rollouts: int = 0
    inflight: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def before(self) -> None:
        """Reserve the next rollout; raises ``BudgetStop`` when it could cross a limit (the
        rollouts already running are counted at the same estimate)."""
        with self._lock:
            mean = self.spent / self.rollouts if self.rollouts else 0.0
            need = max(self.estimate_per_rollout, mean)
            committed = self.spent + (self.inflight + 1) * need
            if committed > self.max_usd:
                raise BudgetStop(
                    f"run cap: spent ${self.spent:.2f}, {self.inflight} running, next ~${need:.2f}: "
                    f"over max_usd ${self.max_usd:.2f}"
                )
            ok, message = self.ledger.check((self.inflight + 1) * need)
            if not ok:
                raise BudgetStop(f"ledger refuses: {message}")
            self.inflight += 1

    def record(self, usage: dict[str, int], note: str) -> float:
        """Price a finished rollout, add it to the run and the ledger, release its reservation."""
        cost = usd(self.model, usage)
        with self._lock:
            self.spent += cost
            self.rollouts += 1
            self.inflight = max(self.inflight - 1, 0)
        self.ledger.add(model=self.model, usage=usage, cost=cost, note=note)
        return cost

    def release(self) -> None:
        """A reserved rollout that never reached the model."""
        with self._lock:
            self.inflight = max(self.inflight - 1, 0)
