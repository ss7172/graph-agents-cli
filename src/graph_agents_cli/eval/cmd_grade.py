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

"""graph-agents-cli eval grade command: score traces and apply the gate."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import click

from graph_agents_cli._output import Console
from graph_agents_cli._project import find_project_root
from graph_agents_cli.eval import _paths, gate
from graph_agents_cli.eval._common import (
    EvalConfigError,
    load_json_file,
    project_meta,
    resolve_judge_identity,
    utc_now_iso,
    write_json_file,
)
from graph_agents_cli.eval._judge import results_by_id, run_judge_runner
from graph_agents_cli.eval.config import EvalConfig, load_eval_config
from graph_agents_cli.eval.dataset import Dataset, EvalCase, cases_from_raw, load_dataset

DEFAULT_JUDGE_TIMEOUT = 600


def _resolve_trace_files(project_root: Path | None, traces_path: str | None) -> list[Path]:
    if traces_path:
        path = Path(traces_path)
        if not path.is_absolute() and project_root is not None:
            candidate = project_root / path
            path = candidate if candidate.exists() or not path.exists() else path
        if path.is_dir():
            files = sorted(path.glob("*.json"))
            if not files:
                raise EvalConfigError(f"no JSON trace files found in {path}")
            return files
        if not path.is_file():
            raise EvalConfigError(f"traces file not found: {path}")
        return [path]
    if project_root is None:
        raise EvalConfigError(
            "not inside a graph-agents-cli project (no manifest found); pass --traces PATH"
        )
    latest = _paths.latest_file(_paths.default_traces_dir(project_root), _paths.TRACES_FILE_PREFIX)
    if latest is None:
        raise EvalConfigError(
            f"no traces found under {_paths.default_traces_dir(project_root)}; "
            "run `graph-agents-cli eval generate` first or pass --traces PATH"
        )
    return [latest]


def load_traces(files: list[Path]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Merge trace files into ``(wrapper metadata, traces by case id)``.

    Every file must come from the same dataset (``dataset_hash``); a case id
    seen twice keeps the entry from the most recently generated file.
    """
    meta: dict[str, Any] = {"dataset_hash": None, "files": []}
    traces: dict[str, dict[str, Any]] = {}
    generated: dict[str, str] = {}
    embedded: dict[str, dict[str, Any]] = {}
    for path in files:
        data = load_json_file(path, "traces file")
        if not isinstance(data, dict) or not isinstance(data.get("traces"), list):
            raise EvalConfigError(f"traces file {path} must be an object with a 'traces' list")
        digest = data.get("dataset_hash")
        if meta["dataset_hash"] is None:
            meta["dataset_hash"] = digest
        elif digest != meta["dataset_hash"]:
            raise EvalConfigError(
                f"traces file {path} was generated from a different dataset "
                f"({digest} != {meta['dataset_hash']}); grade one dataset at a time"
            )
        for key in ("generated_at", "agent_version", "model", "dataset_paths", "base_url"):
            if data.get(key) is not None and meta.get(key) is None:
                meta[key] = data[key]
        meta["files"].append(str(path))
        stamp = str(data.get("generated_at") or "")
        for trace in data["traces"]:
            if not isinstance(trace, dict) or not trace.get("case_id"):
                raise EvalConfigError(f"traces file {path} has an entry without case_id")
            case_id = str(trace["case_id"])
            if case_id in traces and generated.get(case_id, "") > stamp:
                continue
            traces[case_id] = trace
            generated[case_id] = stamp
            if isinstance(trace.get("case"), dict):
                embedded[case_id] = trace["case"]
    meta["embedded_cases"] = [embedded[cid] for cid in traces if cid in embedded]
    return meta, traces


def _planned_dataset(
    project_root: Path | None,
    dataset_flag: str | None,
    meta: dict[str, Any],
    traces: dict[str, dict[str, Any]],
    console: Console,
) -> Dataset:
    """The planned cases: ``--dataset``, else the recorded dataset, else embedded cases."""
    root = project_root or Path.cwd()
    if dataset_flag:
        files = _paths.resolve_input_datasets(root, dataset_flag)
        if not files:
            raise EvalConfigError(f"dataset not found: {dataset_flag}")
        dataset = load_dataset(files)
        if meta.get("dataset_hash") and dataset.hash != meta["dataset_hash"]:
            console.print(
                "[yellow]Warning:[/yellow] --dataset differs from the dataset the traces were "
                "generated from (hash mismatch): its expectations are applied to responses "
                "produced for another dataset version, and cases absent from the traces count "
                "as missing. Run `eval generate` to refresh the traces. The results record both "
                "hashes (dataset_hash, traces_dataset_hash)."
            )
        return dataset

    recorded = meta.get("dataset_paths") or []
    if recorded and project_root is not None:
        files = [project_root / p for p in recorded]
        if all(f.is_file() for f in files):
            try:
                dataset = load_dataset(files)
            except EvalConfigError as exc:
                console.print(f"[yellow]Warning:[/yellow] recorded dataset unusable ({exc}).")
            else:
                if dataset.hash == meta.get("dataset_hash"):
                    return dataset
                console.print(
                    "[yellow]Warning:[/yellow] the dataset on disk changed since the traces were "
                    "generated; grading against the cases embedded in the traces."
                )

    embedded = meta.get("embedded_cases") or []
    if embedded:
        return cases_from_raw(embedded)

    console.print(
        "[yellow]Warning:[/yellow] traces carry no case definitions; pass --dataset to apply "
        "expectations. Grading case accounting only."
    )
    cases = [EvalCase(id=cid, messages=[{"role": "user", "content": ""}]) for cid in traces]
    return Dataset(cases=cases, hash=str(meta.get("dataset_hash") or ""))


def grade_traces(
    *,
    traces_path: str | None = None,
    dataset: str | None = None,
    config_path: str | None = None,
    output_path: str | None = None,
    judge_provider: str | None = None,
    judge_model: str | None = None,
    judge_timeout: int = DEFAULT_JUDGE_TIMEOUT,
    console: Console | None = None,
) -> int:
    """Grade traces and return the gate exit code (0/1/2). Raises EvalConfigError for 3."""
    console = console or Console()
    project_root = find_project_root()

    files = _resolve_trace_files(project_root, traces_path)
    console.print(f"Loading traces from [cyan]{', '.join(str(f) for f in files)}[/cyan]")
    meta, traces = load_traces(files)

    if config_path:
        cfg_file = Path(config_path)
        if not cfg_file.is_absolute() and project_root is not None and not cfg_file.exists():
            cfg_file = project_root / cfg_file
        config: EvalConfig = load_eval_config(cfg_file, required=True)
    else:
        config = load_eval_config(
            _paths.default_eval_config(project_root) if project_root else None
        )

    planned = _planned_dataset(project_root, dataset, meta, traces, console)
    for case in planned.cases:
        gate.validate_case_metrics(config, case)

    extra = sorted(set(traces) - set(planned.case_ids))
    if extra:
        console.print(
            f"[yellow]Warning:[/yellow] {len(extra)} trace(s) not in the dataset are ignored: "
            f"{', '.join(extra[:5])}{'...' if len(extra) > 5 else ''}"
        )

    grades: list[gate.CaseGrade] = []
    items: list[dict[str, Any]] = []
    for case in planned.cases:
        trace = traces.get(case.id)
        grade = gate.grade_deterministic(case, trace)
        if trace is not None and grade.judgeable:
            items.extend(gate.plan_judge_items(config, case, trace, grade))
        grades.append(grade)

    judge = resolve_judge_identity(
        project_root,
        config_provider=config.judge_provider,
        config_model=config.judge_model,
        flag_provider=judge_provider,
        flag_model=judge_model,
    )
    if items:
        if project_root is None:
            raise EvalConfigError(
                "judge metrics need the project's environment; run `eval grade` inside the "
                "project (no graph-agents-cli-manifest.yaml found)"
            )
        console.print(
            f"Running {len(items)} judge/custom metric call(s) in the project environment "
            f"(judge: {judge['provider'] or 'default'}/{judge['model'] or 'default'})..."
        )
        output = run_judge_runner(
            project_root, {"judge": judge, "items": items}, timeout=judge_timeout
        )
        results = results_by_id(output)
        for grade in grades:
            if grade.judge_scores:
                gate.apply_judge_results(grade, results)
        judge = {
            "provider": output.get("provider") or judge["provider"],
            "model": output.get("model") or judge["model"],
        }

    summary = gate.summarize(grades)
    quality = gate.compute_quality(config, grades)
    exit_code = gate.exit_code_for(summary, quality)
    summary["exit_code"] = exit_code
    identity = project_meta(project_root)

    results_doc = {
        "dataset_hash": planned.hash or meta.get("dataset_hash"),
        # Provenance (additive fields): the dataset the traces were
        # generated from, which differs from dataset_hash after `--dataset`.
        "traces_dataset_hash": meta.get("dataset_hash"),
        "graded_at": utc_now_iso(),
        "generated_at": meta.get("generated_at"),
        "judge": judge,
        "capture": identity["capture"],
        "agent_version": meta.get("agent_version") or identity["agent_version"],
        "model": meta.get("model") or identity["model"],
        "traces_files": meta["files"],
        "dataset_paths": [str(p) for p in planned.sources] or meta.get("dataset_paths") or [],
        "config": str(config.source) if config.source else None,
        "planned": len(grades),
        "summary": summary,
        "quality": quality,
        "cases": [g.to_dict() for g in grades],
    }

    root_for_output = project_root or Path.cwd()
    out = _paths.resolve_output_path(
        root_for_output,
        output_path,
        default_dir=_paths.default_grade_results_dir(root_for_output),
        prefix=_paths.RESULTS_FILE_PREFIX,
    )
    write_json_file(out, results_doc)
    gate.print_summary(console, results_doc)
    console.print(f"Results saved to [green]{out}[/green]")
    return exit_code


@click.command("grade")
@click.option(
    "--traces",
    "traces_path",
    type=click.Path(),
    default=None,
    help=(
        "Traces file, or a directory whose *.json files are merged (all from one dataset). "
        "Defaults to the newest artifacts/traces/traces_<ts>.json."
    ),
)
@click.option(
    "--dataset",
    default=None,
    help=(
        "Dataset file or directory to re-read for planned-case accounting and expectations. "
        "Defaults to the dataset recorded in the traces (falling back to the cases embedded in them)."
    ),
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(),
    default=None,
    help=f"Eval config (judge, quality_metrics, judges, custom_metrics). Defaults to {_paths.DEFAULT_EVAL_CONFIG}.",
)
@click.option(
    "--output",
    "-o",
    "output_path",
    default=None,
    help=(
        "Results file, or a directory to write results_<ts>.json into. Defaults to "
        f"{_paths.ARTIFACTS_DIR}/{_paths.GRADE_RESULTS_SUBDIR}/results_<ts>.json."
    ),
)
@click.option(
    "--judge-provider", default=None, help="Override the judge model provider for this run."
)
@click.option("--judge-model", default=None, help="Override the judge model name for this run.")
@click.option(
    "--judge-timeout",
    type=click.IntRange(min=1),
    default=DEFAULT_JUDGE_TIMEOUT,
    show_default=True,
    help="Seconds allowed for the judge runner (all judge calls of the run).",
)
def cmd_grade(
    *,
    traces_path: str | None,
    dataset: str | None,
    config_path: str | None,
    output_path: str | None,
    judge_provider: str | None,
    judge_model: str | None,
    judge_timeout: int,
) -> None:
    """Grade traces against the dataset's checks and judges and apply the gate.

    Deterministic checks run in-process first; judge metrics and custom metrics
    run inside the project's environment through the staged judge runner.
    Every planned case ends as passed, failed, quality_below_threshold, error
    or missing, and the results file records the per-status counts.

    \b
    Exit codes:
      0  gate met
      1  a case failed, or a quality metric is under its min_pass_rate
      2  a case is error or missing (incomplete run)
      3  configuration error (unknown metric, unreachable judge, no threshold)
    """
    code = grade_traces(
        traces_path=traces_path,
        dataset=dataset,
        config_path=config_path,
        output_path=output_path,
        judge_provider=judge_provider,
        judge_model=judge_model,
        judge_timeout=judge_timeout,
    )
    if code:
        sys.exit(code)
