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

"""``system deploy``: ``graph-agents-cli deploy --env ENV`` in every project, callees first.

The projects deploy in waves: a wave holds the agents whose callees are all
deployed, so a caller's first card check finds its peers up (a cycle is broken
at its first agent in file order, with a warning). At most ``--parallel``
deploys run at once (default ``deploy.parallel`` of the file, else 3): round 1
of the experiments deployed seven agents at once and the hub took 158 s where
27-38 s was usual. A failed wave stops the run unless ``--keep-going``. Each
deploy is a child process in its project's directory with no terminal input,
its output kept in a log file; the per-agent build, image load and rollout
times are read from the commands it prints.

Outside ``dev`` every project must record its kube context
(``environments.<env>.context``): a deploy would otherwise ask to confirm the
kubeconfig's current context, and ``system deploy`` never answers that for it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from graph_agents_cli.system._system import Node, System

BUILD = "build"
LOAD = "load"
ROLL = "roll"
# The commands `deploy` echoes (`▸ cmd` or `[dry-run] cmd`) that start each phase.
PHASE_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (BUILD, ("docker build",)),
    (
        LOAD,
        ("kind load", "k3d image import", "minikube image load", "k3s ctr", "docker push"),
    ),
    (ROLL, ("helm upgrade", "helm template")),
)
TAIL_LINES = 15


@dataclass
class Outcome:
    """One project's deploy."""

    agent: str
    exit_code: int
    seconds: float
    phases: dict[str, float] = field(default_factory=dict)
    log: Path | None = None
    tail: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def describe(self) -> str:
        phases = ", ".join(f"{name} {secs:.1f} s" for name, secs in self.phases.items())
        timing = f"{self.seconds:.1f} s" + (f" ({phases})" if phases else "")
        if self.ok:
            return f"{self.agent}: deployed in {timing}"
        return f"{self.agent}: FAILED (exit {self.exit_code}) after {timing}"


# (node, argv after `graph-agents-cli`, log path) -> (exit code, [(seconds since start, line)])
Runner = Callable[[Node, list[str], Path], tuple[int, list[tuple[float, str]]]]


def run_cli(node: Node, args: list[str], log: Path) -> tuple[int, list[tuple[float, str]]]:
    """Run ``graph-agents-cli <args>`` in ``node``'s project; every line timed and logged."""
    command = [sys.executable, "-m", "graph_agents_cli.main", *args]
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    start = time.monotonic()
    lines: list[tuple[float, str]] = []
    with log.open("w", encoding="utf-8") as sink:
        process = subprocess.Popen(
            command,
            cwd=node.root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stdout is not None
        for line in process.stdout:
            lines.append((time.monotonic() - start, line.rstrip("\n")))
            sink.write(line)
            sink.flush()
        code = process.wait()
    return code, lines


def phases(lines: list[tuple[float, str]], total: float) -> dict[str, float]:
    """How long each phase took, from the first line that starts it to the next phase."""
    starts: dict[str, float] = {}
    for seconds, line in lines:
        for phase, markers in PHASE_MARKERS:
            if phase not in starts and any(marker in line for marker in markers):
                starts[phase] = seconds
    ordered = sorted(starts.items(), key=lambda item: item[1])
    result = {}
    for index, (phase, begin) in enumerate(ordered):
        end = ordered[index + 1][1] if index + 1 < len(ordered) else total
        result[phase] = max(0.0, end - begin)
    return {phase: result[phase] for phase, _ in PHASE_MARKERS if phase in result}


def waves(system: System, names: list[str]) -> tuple[list[list[str]], list[str]]:
    """The deploy waves of ``names`` (file order within a wave), and the cycle warnings."""
    selected = [n for n in system.nodes if n in names]
    callees = {
        n: {lk.callee.name for lk in system.calls(system.nodes[n])} & set(selected)
        for n in selected
    }
    done: set[str] = set()
    remaining = list(selected)
    result: list[list[str]] = []
    warnings: list[str] = []
    while remaining:
        wave = [n for n in remaining if callees[n] <= done]
        if not wave:
            first = remaining[0]
            waiting = sorted(callees[first] - done)
            warnings.append(
                f"a cycle: {first} is deployed before {', '.join(waiting)}, which it calls "
                "(its first card checks may fail until they are up)"
            )
            wave = [first]
        result.append(wave)
        done.update(wave)
        remaining = [n for n in remaining if n not in done]
    return result, warnings


def deploy(
    system: System,
    env: str,
    names: list[str],
    *,
    parallel: int,
    keep_going: bool,
    extra: list[str],
    runner: Runner | None = None,
    report: Callable[[str], None] = print,
    log_dir: Path | None = None,
) -> list[Outcome]:
    """Deploy ``names`` wave by wave; the outcome of each deploy that ran (``runner``: how a
    deploy runs, ``run_cli`` by default)."""
    run = runner or run_cli
    plan, warnings = waves(system, names)
    for warning in warnings:
        report(f"Warning: {warning}")
    logs = log_dir or Path(tempfile.mkdtemp(prefix=f"gac-system-deploy-{env}-"))
    report(f"Logs: {logs}")
    outcomes: list[Outcome] = []
    lock = threading.Lock()

    def one(name: str) -> Outcome:
        node = system.nodes[name]
        log = logs / f"{name}.log"
        start = time.monotonic()
        try:
            code, lines = run(node, ["deploy", "--env", env, *extra], log)
        except OSError as exc:
            code, lines = 2, [(0.0, f"could not run graph-agents-cli deploy: {exc}")]
        total = time.monotonic() - start
        outcome = Outcome(
            agent=name,
            exit_code=code,
            seconds=total,
            phases=phases(lines, total),
            log=log,
            tail=[line for _, line in lines[-TAIL_LINES:]],
        )
        with lock:
            report(outcome.describe())
            if not outcome.ok:
                for line in outcome.tail:
                    report(f"    {line}")
                report(f"    log: {log}")
        return outcome

    for number, wave in enumerate(plan, start=1):
        report(f"Wave {number}/{len(plan)}: {', '.join(wave)}")
        with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
            results = list(pool.map(one, wave))
        outcomes.extend(results)
        if any(not r.ok for r in results) and not keep_going and number < len(plan):
            skipped = [n for later in plan[number:] for n in later]
            report(f"Stopped after wave {number}; not deployed: {', '.join(skipped)}")
            break
    return outcomes
