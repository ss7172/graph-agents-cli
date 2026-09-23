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

"""graph-agents-cli eval analyze command: cluster failures by reason."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import click
from rich.table import Table

from graph_agents_cli._output import Console
from graph_agents_cli._project import find_project_root
from graph_agents_cli.eval import _paths
from graph_agents_cli.eval._common import (
    EvalConfigError,
    load_json_file,
    resolve_judge_identity,
    utc_now_iso,
    write_json_file,
)
from graph_agents_cli.eval._judge import results_by_id, run_judge_runner
from graph_agents_cli.eval.config import load_eval_config
from graph_agents_cli.eval.gate import STATUS_PASSED, STATUS_RANK

_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_SPACES = re.compile(r"\s+")
_PATTERN_LIMIT = 120


def normalize_reason(reason: str) -> tuple[str, str]:
    """``(kind, pattern)`` for a reason line: the prefix before ':' and a masked message.

    Quoted literals become ``<x>`` and numbers ``#`` so ``contains: response does
    not contain 'hello'`` and ``... 'goodbye'`` fall into one cluster.
    """
    kind, sep, rest = reason.partition(":")
    if not sep:
        kind, rest = "other", reason
    kind = kind.strip().lower() or "other"
    if kind == "error":
        # "error: judge <metric>: ..." -> keep the judge name out of the pattern.
        sub_kind, sub_sep, sub_rest = rest.strip().partition(":")
        if sub_sep and sub_kind.lower().startswith("judge "):
            kind = "error/judge"
            rest = sub_rest
    pattern = rest.strip().lower()
    pattern = _QUOTED.sub("<x>", pattern)
    pattern = _NUMBER.sub("#", pattern)
    pattern = _SPACES.sub(" ", pattern).strip()
    if len(pattern) > _PATTERN_LIMIT:
        pattern = pattern[: _PATTERN_LIMIT - 3] + "..."
    return kind, pattern


def cluster_results(results: dict[str, Any]) -> list[dict[str, Any]]:
    """Deterministic clusters of every non-passed case's reasons."""
    clusters: dict[tuple[str, str, str], dict[str, Any]] = {}
    for case in results.get("cases", []):
        status = case.get("status")
        if status == STATUS_PASSED:
            continue
        reasons = case.get("reasons") or [f"{status}: no reason recorded"]
        for reason in reasons:
            kind, pattern = normalize_reason(str(reason))
            key = (str(status), kind, pattern)
            cluster = clusters.setdefault(
                key,
                {
                    "status": status,
                    "kind": kind,
                    "pattern": pattern,
                    "count": 0,
                    "case_ids": [],
                    "examples": [],
                },
            )
            cluster["count"] += 1
            if case["id"] not in cluster["case_ids"]:
                cluster["case_ids"].append(case["id"])
            if len(cluster["examples"]) < 3:
                cluster["examples"].append(str(reason))
    ordered = sorted(
        clusters.values(),
        key=lambda c: (-c["count"], -STATUS_RANK.get(c["status"], 0), c["kind"], c["pattern"]),
    )
    for index, cluster in enumerate(ordered, start=1):
        cluster["rank"] = index
    return ordered


def _summary_prompt(results: dict[str, Any], clusters: list[dict[str, Any]]) -> str:
    lines = [
        "You are reviewing the failures of an automated evaluation of an AI agent.",
        f"Summary: {results.get('summary')}",
        "Failure clusters (status / check-or-metric / pattern / count / examples):",
    ]
    for cluster in clusters[:20]:
        lines.append(
            f"- [{cluster['rank']}] {cluster['status']} / {cluster['kind']} / {cluster['pattern']} "
            f"/ {cluster['count']} case(s): {cluster['case_ids'][:5]}"
        )
        for example in cluster["examples"]:
            lines.append(f"    e.g. {example}")
    lines.append(
        "For each cluster give the most likely root cause (prompt, tool, data, or expectation) "
        "and one concrete fix. Finish with the three highest-priority actions. Be brief."
    )
    return "\n".join(lines)


def _resolve_results_path(project_root: Path | None, results: str | None) -> Path:
    if results:
        path = Path(results)
        if not path.is_absolute() and project_root is not None and not path.exists():
            path = project_root / path
        if not path.is_file():
            raise EvalConfigError(f"results file not found: {path}")
        return path
    if project_root is None:
        raise EvalConfigError("not inside a graph-agents-cli project; pass --results PATH")
    latest = _paths.latest_file(
        _paths.default_grade_results_dir(project_root), _paths.RESULTS_FILE_PREFIX
    )
    if latest is None:
        raise EvalConfigError(
            f"no results found under {_paths.default_grade_results_dir(project_root)}; "
            "run `graph-agents-cli eval grade` first or pass --results PATH"
        )
    return latest


@click.command("analyze")
@click.option(
    "--results",
    default=None,
    help="Results file. Defaults to the newest artifacts/grade_results/results_<ts>.json.",
)
@click.option(
    "--output",
    "output_path",
    default=None,
    help="Analysis file. Defaults to artifacts/analysis_<ts>.json.",
)
@click.option(
    "--top-k", type=click.IntRange(min=1), default=None, help="Print only the K largest clusters."
)
@click.option(
    "--judge", is_flag=True, help="Ask the judge model for root causes and fixes per cluster."
)
@click.option("--judge-provider", default=None, help="Override the judge provider (with --judge).")
@click.option("--judge-model", default=None, help="Override the judge model (with --judge).")
def cmd_analyze(
    *,
    results: str | None,
    output_path: str | None,
    top_k: int | None,
    judge: bool,
    judge_provider: str | None,
    judge_model: str | None,
) -> None:
    """Cluster failed, quality-below-threshold, error and missing cases by reason.

    Grouping is deterministic (status, check or metric, masked message), so two
    runs of the same results file produce the same clusters. With --judge the
    clusters are also summarised by the judge model through the project's
    judge runner. The analysis is written to artifacts/analysis_<ts>.json.
    """
    console = Console()
    project_root = find_project_root()
    results_path = _resolve_results_path(project_root, results)
    data = load_json_file(results_path, "results file")
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise EvalConfigError(f"results file {results_path} has no 'cases' list")

    clusters = cluster_results(data)
    flagged = sorted({cid for c in clusters for cid in c["case_ids"]})
    analysis: dict[str, Any] = {
        "results_file": str(results_path),
        "dataset_hash": data.get("dataset_hash"),
        "generated_at": utc_now_iso(),
        "total_cases": len(data["cases"]),
        "flagged_cases": len(flagged),
        "summary": data.get("summary"),
        "clusters": clusters,
        "judge_summary": None,
    }

    if judge and clusters:
        if project_root is None:
            raise EvalConfigError("--judge needs the project's environment; run inside the project")
        config = load_eval_config(_paths.default_eval_config(project_root))
        identity = resolve_judge_identity(
            project_root,
            config_provider=config.judge_provider,
            config_model=config.judge_model,
            flag_provider=judge_provider,
            flag_model=judge_model,
        )
        console.print("Asking the judge model to summarise the clusters...")
        output = run_judge_runner(
            project_root,
            {
                "judge": identity,
                "items": [
                    {
                        "id": "summary",
                        "kind": "summarize",
                        "prompt": _summary_prompt(data, clusters),
                    }
                ],
            },
        )
        entry = results_by_id(output).get("summary") or {}
        if entry.get("error"):
            raise EvalConfigError(f"judge summary failed: {entry['error']}")
        analysis["judge_summary"] = entry.get("reasoning") or ""
        analysis["judge"] = {"provider": output.get("provider"), "model": output.get("model")}

    root = project_root or Path.cwd()
    out = Path(output_path) if output_path else _paths.default_analysis_path(root)
    if not out.is_absolute():
        out = root / out
    write_json_file(out, analysis)

    shown = clusters[:top_k] if top_k else clusters
    if not clusters:
        console.print(
            f"[green]No failures to analyze[/green] in {results_path} ({len(data['cases'])} case(s) passed)."
        )
    else:
        table = Table(
            title=f"Failure clusters ({len(flagged)} of {len(data['cases'])} cases)",
            show_header=True,
            header_style="bold",
        )
        table.add_column("#", justify="right")
        table.add_column("Status")
        table.add_column("Check / metric")
        table.add_column("Pattern")
        table.add_column("Cases", justify="right")
        table.add_column("Case ids")
        for cluster in shown:
            ids = ", ".join(cluster["case_ids"][:5]) + (
                ", ..." if len(cluster["case_ids"]) > 5 else ""
            )
            table.add_row(
                str(cluster["rank"]),
                cluster["status"],
                cluster["kind"],
                cluster["pattern"],
                str(cluster["count"]),
                ids,
            )
        console.print(table)
    if analysis["judge_summary"]:
        console.rule("[bold]Judge summary[/bold]")
        console.print(analysis["judge_summary"])
    console.print(f"Analysis saved to [green]{out}[/green]")
