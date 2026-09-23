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

"""The evaluation gate: case statuses, quality rates, exit codes.

One rule: complete case accounting, every deterministic check and every
mandatory judge metric must pass. Only judge metrics listed under
``quality_metrics:`` may miss their per-case threshold at a rate bounded by
``min_pass_rate``. Statuses: ``passed``, ``failed``, ``quality_below_threshold``,
``error``, ``missing``. Exit codes: 0 gate met; 1 any failed or a quality
metric under its rate; 2 any error or missing; 3 configuration error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rich.table import Table

from graph_agents_cli._output import Console
from graph_agents_cli.eval._common import (
    EXIT_GATE_FAILED,
    EXIT_INCOMPLETE,
    EXIT_OK,
    EvalConfigError,
)
from graph_agents_cli.eval.checks import run_checks
from graph_agents_cli.eval.config import EvalConfig, render_judge_prompt, resolve_threshold
from graph_agents_cli.eval.dataset import EvalCase

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_QUALITY = "quality_below_threshold"
STATUS_ERROR = "error"
STATUS_MISSING = "missing"
STATUSES: tuple[str, ...] = (
    STATUS_PASSED,
    STATUS_FAILED,
    STATUS_QUALITY,
    STATUS_ERROR,
    STATUS_MISSING,
)
# Higher is worse; used by ``eval compare`` to detect regressions.
STATUS_RANK: dict[str, int] = {
    STATUS_PASSED: 0,
    STATUS_QUALITY: 1,
    STATUS_FAILED: 2,
    STATUS_ERROR: 3,
    STATUS_MISSING: 3,
}

_STATUS_STYLE = {
    STATUS_PASSED: "green",
    STATUS_QUALITY: "yellow",
    STATUS_FAILED: "red",
    STATUS_ERROR: "red",
    STATUS_MISSING: "magenta",
}


@dataclass
class CaseGrade:
    id: str
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)
    judge_scores: dict[str, dict[str, Any]] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    quality_misses: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.missing:
            return STATUS_MISSING
        if self.errors:
            return STATUS_ERROR
        if self.failures:
            return STATUS_FAILED
        if self.quality_misses:
            return STATUS_QUALITY
        return STATUS_PASSED

    @property
    def reasons(self) -> list[str]:
        return [*self.missing, *self.errors, *self.failures, *self.quality_misses]

    @property
    def judgeable(self) -> bool:
        """Judges run only for cases whose mandatory items have not already failed."""
        return not (self.missing or self.errors or self.failures)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "reasons": self.reasons,
            "checks": self.checks,
            "judge_scores": self.judge_scores,
        }


# --- deterministic stage ------------------------------------------------------


def grade_deterministic(case: EvalCase, trace: dict[str, Any] | None) -> CaseGrade:
    """Case accounting plus the deterministic checks the case declares."""
    grade = CaseGrade(id=case.id)
    if trace is None:
        grade.missing.append("missing: no trace for case")
        return grade
    if trace.get("status") == "missing":
        grade.missing.append(f"missing: {trace.get('error') or 'trace has no response'}")
        return grade
    if trace.get("status") == "error":
        grade.errors.append(f"error: {trace.get('error') or 'generation failed'}")
        return grade
    if trace.get("response") is None:
        grade.missing.append("missing: trace has no response")
        return grade
    grade.checks = run_checks(case.expect, trace)
    for name, result in grade.checks.items():
        if not result["passed"]:
            grade.failures.append(f"{name}: {result['reason']}")
    return grade


# --- judge stage --------------------------------------------------------------


def case_metrics(config: EvalConfig, case: EvalCase) -> dict[str, dict[str, Any]]:
    """Metric name -> case-level spec for every judge/custom metric that applies."""
    metrics: dict[str, dict[str, Any]] = {name: dict(spec) for name, spec in case.judge.items()}
    for name in config.custom_metrics:
        metrics.setdefault(name, {})
    return metrics


def validate_case_metrics(config: EvalConfig, case: EvalCase) -> None:
    """Unknown metric or missing threshold is a configuration error (exit 3)."""
    known = config.metric_names()
    for name, spec in case_metrics(config, case).items():
        if name not in known:
            raise EvalConfigError(
                f"case {case.id!r} declares unknown judge metric {name!r} "
                f"(known: {', '.join(sorted(known))})"
            )
        resolve_threshold(config, name, spec)


def item_id(case_id: str, metric: str) -> str:
    return f"{case_id}/{metric}"


def plan_judge_items(
    config: EvalConfig, case: EvalCase, trace: dict[str, Any], grade: CaseGrade
) -> list[dict[str, Any]]:
    """Runner items for the judge and custom metrics of one judgeable case."""
    if not grade.judgeable:
        return []
    items: list[dict[str, Any]] = []
    response = trace.get("response")
    response = "" if response is None else str(response)
    for name, spec in case_metrics(config, case).items():
        threshold = resolve_threshold(config, name, spec)
        quality = config.is_quality(name)
        custom = config.custom_metrics.get(name)
        grade.judge_scores[name] = {
            "score": None,
            "threshold": threshold,
            "passed": None,
            "quality": quality,
            "reasoning": "",
            "error": None,
            # "judge" (an LLM judge) or "custom" (a project callable): errors say which.
            "kind": "custom" if custom is not None else "judge",
        }
        if custom is not None:
            items.append(
                {
                    "id": item_id(case.id, name),
                    "kind": "custom",
                    "case_id": case.id,
                    "metric": name,
                    "callable": custom.callable,
                    "case": case.raw,
                    "trace": trace,
                }
            )
            continue
        judge = config.judges[name]
        items.append(
            {
                "id": item_id(case.id, name),
                "kind": "judge",
                "case_id": case.id,
                "metric": name,
                "scale": judge.scale,
                "prompt": render_judge_prompt(
                    judge,
                    conversation=case.conversation_text(),
                    response=response,
                    reference=case.reference,
                    context=case.context,
                    tool_calls=trace.get("tool_calls") or None,
                ),
            }
        )
    return items


def apply_judge_results(grade: CaseGrade, results: dict[str, dict[str, Any]]) -> None:
    """Fold runner results into the grade: error, mandatory fail, or quality miss."""
    for name, entry in grade.judge_scores.items():
        label = "custom metric" if entry.get("kind") == "custom" else "judge"
        result = results.get(item_id(grade.id, name))
        if result is None:
            entry["error"] = "no result from judge runner"
            grade.errors.append(f"error: {label} {name}: no result from judge runner")
            continue
        if result.get("error"):
            entry["error"] = str(result["error"])
            grade.errors.append(f"error: {label} {name}: {result['error']}")
            continue
        score = result.get("score")
        if not isinstance(score, int | float) or isinstance(score, bool):
            entry["error"] = f"{label} returned no numeric score ({score!r})"
            grade.errors.append(f"error: {label} {name}: no numeric score")
            continue
        entry["score"] = float(score)
        entry["reasoning"] = str(result.get("reasoning") or "")
        passed = float(score) >= float(entry["threshold"])
        entry["passed"] = passed
        if passed:
            continue
        text = f"{name}: score {_fmt(score)} below threshold {_fmt(entry['threshold'])}"
        if entry["quality"]:
            grade.quality_misses.append(f"{text} (quality)")
        else:
            grade.failures.append(text)


def _fmt(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


# --- aggregate ----------------------------------------------------------------


def summarize(grades: list[CaseGrade]) -> dict[str, int]:
    summary = dict.fromkeys(STATUSES, 0)
    for grade in grades:
        summary[grade.status] += 1
    return summary


def compute_quality(config: EvalConfig, grades: list[CaseGrade]) -> dict[str, dict[str, Any]]:
    """Per quality metric: pass rate over planned cases, ``met``; null when incomplete."""
    planned = len(grades)
    complete = all(g.status not in (STATUS_ERROR, STATUS_MISSING) for g in grades)
    quality: dict[str, dict[str, Any]] = {}
    for name, metric in config.quality_metrics.items():
        below = sum(
            1
            for g in grades
            if (entry := g.judge_scores.get(name)) is not None
            and entry.get("quality")
            and entry.get("passed") is False
        )
        if not complete or planned == 0:
            quality[name] = {
                "pass_rate": None,
                "min_pass_rate": metric.min_pass_rate,
                "met": None,
                "below_threshold": below,
            }
            continue
        rate = (planned - below) / planned
        quality[name] = {
            "pass_rate": round(rate, 4),
            "min_pass_rate": metric.min_pass_rate,
            "met": rate >= metric.min_pass_rate,
            "below_threshold": below,
        }
    return quality


def exit_code_for(summary: dict[str, int], quality: dict[str, dict[str, Any]]) -> int:
    if summary.get(STATUS_ERROR) or summary.get(STATUS_MISSING):
        return EXIT_INCOMPLETE
    if summary.get(STATUS_FAILED) or any(q.get("met") is False for q in quality.values()):
        return EXIT_GATE_FAILED
    return EXIT_OK


def print_summary(console: Console, results: dict[str, Any]) -> None:
    summary = results["summary"]
    table = Table(title="Evaluation gate", show_header=True, header_style="bold")
    table.add_column("Status")
    table.add_column("Cases", justify="right")
    for status in STATUSES:
        count = summary.get(status, 0)
        style = _STATUS_STYLE[status] if count else "dim"
        table.add_row(f"[{style}]{status}[/{style}]", str(count))
    table.add_row(
        "[bold]planned[/bold]",
        str(results.get("planned", sum(summary.get(s, 0) for s in STATUSES))),
    )
    console.print(table)

    quality = results.get("quality") or {}
    if quality:
        qtable = Table(title="Quality metrics", show_header=True, header_style="bold")
        qtable.add_column("Metric")
        qtable.add_column("Pass rate", justify="right")
        qtable.add_column("Min", justify="right")
        qtable.add_column("Met")
        for name, entry in quality.items():
            rate = entry.get("pass_rate")
            met = entry.get("met")
            met_text = (
                "n/a (incomplete)"
                if met is None
                else ("[green]yes[/green]" if met else "[red]no[/red]")
            )
            qtable.add_row(
                name,
                "n/a" if rate is None else f"{rate:.0%}",
                f"{entry.get('min_pass_rate', 1.0):.0%}",
                met_text,
            )
        console.print(qtable)

    flagged = [c for c in results.get("cases", []) if c["status"] != STATUS_PASSED]
    if flagged:
        ctable = Table(title="Cases needing attention", show_header=True, header_style="bold")
        ctable.add_column("Case")
        ctable.add_column("Status")
        ctable.add_column("Reasons")
        for case in flagged:
            style = _STATUS_STYLE[case["status"]]
            ctable.add_row(
                case["id"],
                f"[{style}]{case['status']}[/{style}]",
                "\n".join(case["reasons"][:4]) + ("\n..." if len(case["reasons"]) > 4 else ""),
            )
        console.print(ctable)
    code = summary.get("exit_code", 0)
    verdict = {
        0: "[green]gate met[/green]",
        1: "[red]gate failed[/red]",
        2: "[magenta]incomplete run[/magenta]",
    }
    console.print(f"Result: {verdict.get(code, code)} (exit code {code})")
