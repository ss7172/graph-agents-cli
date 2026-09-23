# Copyright 2026 Google LLC
# Modifications Copyright 2026 graph-agents-cli contributors
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

"""graph-agents-cli eval compare command: diff two results files."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click
from rich.table import Table

from graph_agents_cli._output import Console, emit
from graph_agents_cli.eval._common import EXIT_GATE_FAILED, load_json_file
from graph_agents_cli.eval.gate import STATUS_RANK


def _diff(base: dict, cand: dict, prefix: str = "") -> dict:
    """Compute a recursive diff between two dicts.

    Nested dicts are diffed recursively with dotted key paths.
    Numeric changes include a delta (e.g., "+0.07" or "-0.03").
    """
    differences = {}

    all_keys = set(base.keys()) | set(cand.keys())
    for key in sorted(all_keys):
        full_key = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
        base_val = base.get(key)
        cand_val = cand.get(key)

        if base_val == cand_val:
            continue

        # Recurse into nested dicts
        if isinstance(base_val, dict) and isinstance(cand_val, dict):
            nested = _diff(base_val, cand_val, prefix=full_key)
            differences.update(nested["differences"])
            continue

        entry = {"baseline": base_val, "candidate": cand_val}
        if isinstance(base_val, int | float) and isinstance(cand_val, int | float):
            delta = cand_val - base_val
            entry["delta"] = f"+{delta}" if delta >= 0 else str(delta)
        differences[full_key] = entry

    if prefix:
        return {"differences": differences}

    return {
        "baseline_keys": sorted(base.keys()),
        "candidate_keys": sorted(cand.keys()),
        "differences": differences,
        "changed_keys": sorted(differences.keys()),
        "unchanged_keys": sorted(k for k in all_keys if k not in differences),
    }


def _rank(status: str | None) -> int:
    return STATUS_RANK.get(status or "", 3)


def _score_map(case: dict[str, Any]) -> dict[str, float | None]:
    return {
        name: entry.get("score")
        for name, entry in (case.get("judge_scores") or {}).items()
        if isinstance(entry, dict)
    }


def compare_results(base: dict[str, Any], cand: dict[str, Any]) -> dict[str, Any]:
    """Per-case status changes, quality-rate deltas, summary deltas, regression flag."""
    base_cases = {c["id"]: c for c in base.get("cases", []) if isinstance(c, dict)}
    cand_cases = {c["id"]: c for c in cand.get("cases", []) if isinstance(c, dict)}
    cases: dict[str, dict[str, Any]] = {}
    regressions: list[str] = []
    improvements: list[str] = []
    for case_id in sorted(set(base_cases) | set(cand_cases)):
        b = base_cases.get(case_id)
        c = cand_cases.get(case_id)
        b_status = b.get("status") if b else None
        c_status = c.get("status") if c else None
        if b is None:
            change = "added"
        elif c is None:
            change = "removed"
            regressions.append(f"{case_id}: removed from the candidate run")
        elif _rank(c_status) > _rank(b_status):
            change = "regressed"
            regressions.append(f"{case_id}: {b_status} -> {c_status}")
        elif _rank(c_status) < _rank(b_status):
            change = "improved"
            improvements.append(f"{case_id}: {b_status} -> {c_status}")
        else:
            change = "unchanged"
        scores: dict[str, dict[str, Any]] = {}
        b_scores = _score_map(b) if b else {}
        c_scores = _score_map(c) if c else {}
        for metric in sorted(set(b_scores) | set(c_scores)):
            entry: dict[str, Any] = {
                "baseline": b_scores.get(metric),
                "candidate": c_scores.get(metric),
            }
            if isinstance(entry["baseline"], int | float) and isinstance(
                entry["candidate"], int | float
            ):
                entry["delta"] = round(entry["candidate"] - entry["baseline"], 4)
            scores[metric] = entry
        cases[case_id] = {
            "baseline": b_status,
            "candidate": c_status,
            "change": change,
            "candidate_reasons": (c or {}).get("reasons", []),
            "scores": scores,
        }

    quality: dict[str, dict[str, Any]] = {}
    b_quality = base.get("quality") or {}
    c_quality = cand.get("quality") or {}
    for metric in sorted(set(b_quality) | set(c_quality)):
        b_entry = b_quality.get(metric) or {}
        c_entry = c_quality.get(metric) or {}
        b_rate = b_entry.get("pass_rate")
        c_rate = c_entry.get("pass_rate")
        entry = {
            "baseline_pass_rate": b_rate,
            "candidate_pass_rate": c_rate,
            "baseline_met": b_entry.get("met"),
            "candidate_met": c_entry.get("met"),
            "min_pass_rate": c_entry.get("min_pass_rate", b_entry.get("min_pass_rate")),
        }
        if isinstance(b_rate, int | float) and isinstance(c_rate, int | float):
            entry["delta"] = round(c_rate - b_rate, 4)
            if c_rate < b_rate:
                regressions.append(f"quality {metric}: pass rate {b_rate:.0%} -> {c_rate:.0%}")
            elif c_rate > b_rate:
                improvements.append(f"quality {metric}: pass rate {b_rate:.0%} -> {c_rate:.0%}")
        if b_entry.get("met") is True and c_entry.get("met") is False:
            regressions.append(f"quality {metric}: min_pass_rate no longer met")
        quality[metric] = entry

    b_summary = base.get("summary") or {}
    c_summary = cand.get("summary") or {}
    b_exit = b_summary.get("exit_code")
    c_exit = c_summary.get("exit_code")
    if isinstance(b_exit, int) and isinstance(c_exit, int) and c_exit > b_exit:
        regressions.append(f"exit code {b_exit} -> {c_exit}")

    return {
        "dataset_hash": {
            "baseline": base.get("dataset_hash"),
            "candidate": cand.get("dataset_hash"),
        },
        "same_dataset": base.get("dataset_hash") == cand.get("dataset_hash"),
        "traces_dataset_hash": {
            label: {
                "value": doc.get("traces_dataset_hash"),
                # Only a recorded, differing value is a mismatch (older results lack the key).
                "mismatch": bool(
                    doc.get("traces_dataset_hash")
                    and doc.get("dataset_hash")
                    and doc.get("traces_dataset_hash") != doc.get("dataset_hash")
                ),
            }
            for label, doc in (("baseline", base), ("candidate", cand))
        },
        "summary": _diff(b_summary, c_summary),
        "quality": quality,
        "cases": cases,
        "regressions": regressions,
        "improvements": improvements,
        "regressed": bool(regressions),
    }


def _print_report(console: Console, report: dict[str, Any], baseline: str, candidate: str) -> None:
    console.print(f"Baseline:  [cyan]{baseline}[/cyan]")
    console.print(f"Candidate: [cyan]{candidate}[/cyan]")
    if not report["same_dataset"]:
        console.print("[yellow]Warning:[/yellow] the two results come from different datasets.")
    for label, entry in (
        ("baseline", report["traces_dataset_hash"]["baseline"]),
        ("candidate", report["traces_dataset_hash"]["candidate"]),
    ):
        if entry["mismatch"]:
            console.print(
                f"[yellow]Warning:[/yellow] the {label} results were graded against a dataset "
                "the traces were not generated from (traces_dataset_hash != dataset_hash)."
            )

    changed = {k: v for k, v in report["cases"].items() if v["change"] != "unchanged"}
    table = Table(title="Case status changes", show_header=True, header_style="bold")
    table.add_column("Case")
    table.add_column("Baseline")
    table.add_column("Candidate")
    table.add_column("Change")
    style = {"regressed": "red", "improved": "green", "added": "cyan", "removed": "magenta"}
    for case_id, entry in changed.items():
        table.add_row(
            case_id,
            str(entry["baseline"]),
            str(entry["candidate"]),
            f"[{style[entry['change']]}]{entry['change']}[/{style[entry['change']]}]",
        )
    if changed:
        console.print(table)
    else:
        console.print(f"No case status changes across {len(report['cases'])} case(s).")

    if report["quality"]:
        qtable = Table(title="Quality metrics", show_header=True, header_style="bold")
        qtable.add_column("Metric")
        qtable.add_column("Baseline", justify="right")
        qtable.add_column("Candidate", justify="right")
        qtable.add_column("Delta", justify="right")
        for metric, entry in report["quality"].items():
            b, c = entry["baseline_pass_rate"], entry["candidate_pass_rate"]
            qtable.add_row(
                metric,
                "n/a" if b is None else f"{b:.0%}",
                "n/a" if c is None else f"{c:.0%}",
                "n/a" if "delta" not in entry else f"{entry['delta']:+.0%}",
            )
        console.print(qtable)

    diffs = report["summary"]["differences"]
    if diffs:
        console.print("Summary deltas:")
        for key, entry in diffs.items():
            delta = f" ({entry['delta']})" if "delta" in entry else ""
            console.print(f"  {key}: {entry['baseline']} -> {entry['candidate']}{delta}")
    if report["regressions"]:
        console.print("[red]Regressions:[/red]")
        for line in report["regressions"]:
            console.print(f"  - {line}")
    if report["improvements"]:
        console.print("[green]Improvements:[/green]")
        for line in report["improvements"]:
            console.print(f"  - {line}")
    if not report["regressions"]:
        console.print("[green]No regressions.[/green]")


@click.command("compare")
@click.argument("baseline", type=click.Path(exists=True, dir_okay=False))
@click.argument("candidate", type=click.Path(exists=True, dir_okay=False))
@click.option("--fail-on-regression", is_flag=True, help="Exit 1 when the candidate regresses.")
@click.option(
    "--json", "as_json", is_flag=True, help="Print the full comparison as JSON instead of tables."
)
def cmd_compare(baseline: str, candidate: str, *, fail_on_regression: bool, as_json: bool) -> None:
    """Compare two eval results files (baseline, candidate).

    Reports per-case status changes, quality pass-rate deltas and summary
    deltas. A regression is a case whose status got worse (or disappeared), a
    quality metric whose pass rate dropped or stopped meeting its
    min_pass_rate, or a worse exit code. Purely in-process.
    """
    base = load_json_file(Path(baseline), "baseline results")
    cand = load_json_file(Path(candidate), "candidate results")
    report = compare_results(base, cand)
    report["baseline"] = baseline
    report["candidate"] = candidate
    if as_json:
        emit(json.loads(json.dumps(report)))
    else:
        _print_report(Console(), report, baseline, candidate)
    if fail_on_regression and report["regressed"]:
        sys.exit(EXIT_GATE_FAILED)
