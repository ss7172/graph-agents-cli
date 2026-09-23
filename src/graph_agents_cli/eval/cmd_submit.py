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

"""graph-agents-cli eval submit command: upload a dataset and results to LangSmith.

Optional and never required (D16): needs ``LANGSMITH_API_KEY`` and the
``langsmith`` extra (``uv tool install 'graph-agents-cli[langsmith]'``). The
SDK is imported lazily so the CLI works without it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import click

from graph_agents_cli._output import Console
from graph_agents_cli._project import find_project_root
from graph_agents_cli.eval import _paths
from graph_agents_cli.eval._common import (
    EvalConfigError,
    load_json_file,
    project_env,
    project_meta,
)
from graph_agents_cli.eval.cmd_grade import load_traces
from graph_agents_cli.eval.dataset import Dataset, cases_from_raw, load_dataset
from graph_agents_cli.eval.gate import STATUS_PASSED

LANGSMITH_API_KEY = "LANGSMITH_API_KEY"
LANGSMITH_ENDPOINT = "LANGSMITH_ENDPOINT"


def _langsmith_client(api_key: str, endpoint: str | None) -> Any:
    try:
        import langsmith
    except ImportError as exc:
        raise EvalConfigError(
            "the langsmith package is not installed; install the extra: "
            "uv tool install 'graph-agents-cli[langsmith]'"
        ) from exc
    kwargs: dict[str, Any] = {"api_key": api_key}
    if endpoint:
        kwargs["api_url"] = endpoint
    return langsmith.Client(**kwargs)


def _resolve_results(project_root: Path | None, results: str | None) -> Path:
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
            "no results found; run `graph-agents-cli eval grade` first or pass --results PATH"
        )
    return latest


def _resolve_traces(
    project_root: Path | None, traces: str | None, results: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    candidates: list[Path] = []
    if traces:
        path = Path(traces)
        if not path.is_absolute() and project_root is not None and not path.exists():
            path = project_root / path
        candidates = sorted(path.glob("*.json")) if path.is_dir() else [path]
    else:
        for recorded in results.get("traces_files") or []:
            path = Path(recorded)
            if not path.is_absolute() and project_root is not None:
                path = project_root / path
            if path.is_file():
                candidates.append(path)
    if not candidates:
        return {}, {}
    return load_traces(candidates)


def _resolve_dataset(
    project_root: Path | None,
    dataset: str | None,
    results: dict[str, Any],
    trace_meta: dict[str, Any],
) -> Dataset:
    root = project_root or Path.cwd()
    if dataset:
        files = _paths.resolve_input_datasets(root, dataset)
        if not files:
            raise EvalConfigError(f"dataset not found: {dataset}")
        return load_dataset(files)
    recorded = results.get("dataset_paths") or trace_meta.get("dataset_paths") or []
    files = [root / p if not Path(p).is_absolute() else Path(p) for p in recorded]
    if files and all(f.is_file() for f in files):
        return load_dataset(files)
    embedded = trace_meta.get("embedded_cases") or []
    if embedded:
        return cases_from_raw(embedded)
    raise EvalConfigError("cannot locate the dataset for these results; pass --dataset PATH")


def upload(
    client: Any,
    *,
    dataset: Dataset,
    results: dict[str, Any],
    traces: dict[str, dict[str, Any]],
    dataset_name: str,
    experiment: str,
    console: Console,
) -> dict[str, Any]:
    """Create/update the dataset and examples, then an experiment with runs and feedback."""
    try:
        ls_dataset = client.read_dataset(dataset_name=dataset_name)
        console.print(f"Updating LangSmith dataset [cyan]{dataset_name}[/cyan]")
    except Exception:
        ls_dataset = client.create_dataset(
            dataset_name=dataset_name,
            description="graph-agents-cli eval dataset (tests/eval/datasets)",
        )
        console.print(f"Created LangSmith dataset [cyan]{dataset_name}[/cyan]")

    existing: dict[str, Any] = {}
    for example in client.list_examples(dataset_id=ls_dataset.id):
        metadata = getattr(example, "metadata", None) or {}
        case_id = metadata.get("case_id")
        if case_id:
            existing[str(case_id)] = example

    example_ids: dict[str, Any] = {}
    for case in dataset.cases:
        inputs = {"messages": case.messages}
        outputs: dict[str, Any] = {}
        if case.reference is not None:
            outputs["reference"] = case.reference
        if case.context is not None:
            outputs["context"] = case.context
        metadata = {
            "case_id": case.id,
            "dataset_hash": dataset.hash,
            "expect": case.expect,
            "judge": case.judge,
            **case.metadata,
        }
        if case.id in existing:
            example = existing[case.id]
            client.update_example(
                example_id=example.id, inputs=inputs, outputs=outputs or None, metadata=metadata
            )
            example_ids[case.id] = example.id
        else:
            example = client.create_example(
                inputs=inputs, outputs=outputs or None, dataset_id=ls_dataset.id, metadata=metadata
            )
            example_ids[case.id] = getattr(example, "id", None)

    project = client.create_project(
        project_name=experiment,
        reference_dataset_id=ls_dataset.id,
        metadata={
            "dataset_hash": results.get("dataset_hash"),
            "traces_dataset_hash": results.get("traces_dataset_hash"),
            "graded_at": results.get("graded_at"),
            "agent_version": results.get("agent_version"),
            "model": results.get("model"),
            "judge": results.get("judge"),
            "summary": results.get("summary"),
            "quality": results.get("quality"),
        },
    )
    console.print(f"Created LangSmith experiment [cyan]{experiment}[/cyan]")

    run_count = 0
    feedback_count = 0
    for case_result in results.get("cases", []):
        case_id = case_result["id"]
        trace = traces.get(case_id) or {}
        case = dataset.by_id().get(case_id)
        run_id = uuid.uuid4()
        end = datetime.now(UTC)
        latency = trace.get("latency_ms")
        start = (
            end - timedelta(milliseconds=float(latency))
            if isinstance(latency, int | float)
            else end
        )
        client.create_run(
            id=run_id,
            name=case_id,
            run_type="chain",
            inputs={"messages": case.messages} if case else {},
            outputs={
                "response": trace.get("response"),
                "tool_calls": trace.get("tool_calls"),
                "status": case_result.get("status"),
            },
            error=trace.get("error"),
            start_time=start,
            end_time=end,
            project_name=experiment,
            reference_example_id=example_ids.get(case_id),
            extra={
                "metadata": {
                    "case_id": case_id,
                    "thread_id": trace.get("thread_id"),
                    "run_id": trace.get("run_id"),
                    "usage": trace.get("usage"),
                    "latency_ms": latency,
                    "reasons": case_result.get("reasons"),
                }
            },
        )
        run_count += 1
        client.create_feedback(
            run_id,
            key="gate",
            score=1 if case_result.get("status") == STATUS_PASSED else 0,
            value=case_result.get("status"),
            comment="; ".join(case_result.get("reasons") or []) or None,
        )
        feedback_count += 1
        for name, check in (case_result.get("checks") or {}).items():
            client.create_feedback(
                run_id,
                key=f"check:{name}",
                score=1 if check.get("passed") else 0,
                comment=check.get("reason") or None,
            )
            feedback_count += 1
        for name, entry in (case_result.get("judge_scores") or {}).items():
            score = entry.get("score")
            if isinstance(score, int | float):
                client.create_feedback(
                    run_id, key=name, score=score, comment=entry.get("reasoning") or None
                )
                feedback_count += 1

    return {
        "dataset_id": str(getattr(ls_dataset, "id", "")),
        "dataset_url": getattr(ls_dataset, "url", None),
        "project_id": str(getattr(project, "id", "")),
        "project_url": getattr(project, "url", None),
        "examples": len(example_ids),
        "runs": run_count,
        "feedback": feedback_count,
    }


@click.command("submit")
@click.option(
    "--results",
    default=None,
    help="Results file. Defaults to the newest artifacts/grade_results/results_<ts>.json.",
)
@click.option(
    "--traces",
    default=None,
    help="Traces file or directory. Defaults to the traces recorded in the results.",
)
@click.option(
    "--dataset",
    default=None,
    help="Dataset file or directory. Defaults to the dataset recorded in the results.",
)
@click.option(
    "--dataset-name", default=None, help="LangSmith dataset name. Defaults to '<project>-eval'."
)
@click.option(
    "--experiment",
    default=None,
    help="LangSmith experiment (project) name. Defaults to '<dataset-name>-<graded_at>'.",
)
@click.option("--endpoint", default=None, help="LangSmith API URL. Defaults to LANGSMITH_ENDPOINT.")
def cmd_submit(
    *,
    results: str | None,
    traces: str | None,
    dataset: str | None,
    dataset_name: str | None,
    experiment: str | None,
    endpoint: str | None,
) -> None:
    """Upload the dataset and a results file to LangSmith as an experiment.

    Creates or updates the dataset and its examples (one per case, keyed by
    case id), then creates an experiment whose runs carry each case's
    response, tool calls and status, with feedback scores for the gate, every
    deterministic check and every judge metric. Requires LANGSMITH_API_KEY in
    the environment or the project's .env, and the `langsmith` extra.
    """
    console = Console()
    project_root = find_project_root()
    env = project_env(project_root)
    api_key = env.get(LANGSMITH_API_KEY)
    if not api_key:
        raise EvalConfigError(
            "LangSmith is not configured: set LANGSMITH_API_KEY in the environment or the "
            "project's .env (optional; the local eval gate does not need it)"
        )
    results_path = _resolve_results(project_root, results)
    results_doc = load_json_file(results_path, "results file")
    if not isinstance(results_doc, dict) or not isinstance(results_doc.get("cases"), list):
        raise EvalConfigError(f"results file {results_path} has no 'cases' list")
    trace_meta, trace_map = _resolve_traces(project_root, traces, results_doc)
    if not trace_map:
        console.print("[yellow]Warning:[/yellow] no traces found; runs will carry statuses only.")
    ds = _resolve_dataset(project_root, dataset, results_doc, trace_meta)

    meta = project_meta(project_root)
    name = dataset_name or f"{meta['name'] or 'graph-agents-cli'}-eval"
    stamp = (
        str(results_doc.get("graded_at") or _paths.timestamp())
        .replace(":", "")
        .replace("+0000", "Z")
    )
    experiment_name = experiment or f"{name}-{stamp}"

    client = _langsmith_client(api_key, endpoint or env.get(LANGSMITH_ENDPOINT))
    console.print(
        f"Submitting [cyan]{results_path}[/cyan] ({len(results_doc['cases'])} case(s)) to LangSmith..."
    )
    report = upload(
        client,
        dataset=ds,
        results=results_doc,
        traces=trace_map,
        dataset_name=name,
        experiment=experiment_name,
        console=console,
    )
    console.print(
        f"[green]Uploaded[/green] {report['examples']} example(s), {report['runs']} run(s), "
        f"{report['feedback']} feedback score(s)."
    )
    for label, key in (("Dataset", "dataset_url"), ("Experiment", "project_url")):
        if report.get(key):
            console.print(f"{label}: [cyan]{report[key]}[/cyan]")
