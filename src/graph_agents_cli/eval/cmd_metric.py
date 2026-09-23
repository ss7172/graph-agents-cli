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

"""graph-agents-cli eval metric commands."""

from __future__ import annotations

import click
from rich.table import Table

from graph_agents_cli._click import LazyGroup
from graph_agents_cli._output import Console, emit
from graph_agents_cli._project import find_project_root
from graph_agents_cli.eval import _paths
from graph_agents_cli.eval.checks import CHECK_DESCRIPTIONS
from graph_agents_cli.eval.config import BUILTIN_JUDGES, EvalConfig, load_eval_config


@click.group("metric", cls=LazyGroup)
def metric_group() -> None:
    """Discover evaluation metrics: deterministic checks and judges."""


def _project_config() -> EvalConfig | None:
    project_root = find_project_root()
    if project_root is None:
        return None
    path = _paths.default_eval_config(project_root)
    if not path.is_file():
        return None
    try:
        return load_eval_config(path)
    except click.ClickException:
        return None


@click.command("list")
@click.option("--json", "as_json", is_flag=True, help="Print the catalogue as JSON.")
def list_metrics(*, as_json: bool) -> None:
    """List deterministic checks and built-in judges (plus this project's config)."""
    config = _project_config()
    judges = []
    for name, spec in BUILTIN_JUDGES.items():
        judges.append(
            {
                "name": name,
                "scale": spec["scale"],
                "description": spec["description"],
                "source": "built-in",
            }
        )
    custom = []
    if config is not None:
        for name, spec in config.judges.items():
            if not spec.builtin:
                judges.append(
                    {
                        "name": name,
                        "scale": spec.scale,
                        "description": spec.description or "custom rubric",
                        "source": str(config.source),
                    }
                )
        for name, metric in config.custom_metrics.items():
            custom.append(
                {
                    "name": name,
                    "callable": metric.callable,
                    "threshold": metric.threshold,
                    "description": metric.description,
                    "source": str(config.source),
                }
            )
        quality = sorted(config.quality_metrics)
    else:
        quality = []

    catalogue = {
        "checks": [{"name": n, "description": d} for n, d in CHECK_DESCRIPTIONS.items()],
        "judges": judges,
        "custom_metrics": custom,
        "quality_metrics": quality,
    }
    if as_json:
        emit(catalogue)
        return

    console = Console()
    checks = Table(
        title="Deterministic checks (expect.<name>)", show_header=True, header_style="bold"
    )
    checks.add_column("Check", style="cyan", no_wrap=True)
    checks.add_column("Description")
    for entry in catalogue["checks"]:
        checks.add_row(entry["name"], entry["description"])
    console.print(checks)

    table = Table(title="Judge metrics (judge.<name>)", show_header=True, header_style="bold")
    table.add_column("Judge", style="cyan", no_wrap=True)
    table.add_column("Scale", justify="right")
    table.add_column("Description")
    table.add_column("Source")
    for entry in judges:
        mark = " [yellow](quality)[/yellow]" if entry["name"] in quality else ""
        table.add_row(
            entry["name"] + mark, str(entry["scale"]), entry["description"], entry["source"]
        )
    console.print(table)

    if custom:
        ctable = Table(
            title="Custom metrics (custom_metrics in eval_config.yaml)",
            show_header=True,
            header_style="bold",
        )
        ctable.add_column("Metric", style="cyan", no_wrap=True)
        ctable.add_column("Callable")
        ctable.add_column("Threshold", justify="right")
        for entry in custom:
            mark = " [yellow](quality)[/yellow]" if entry["name"] in quality else ""
            ctable.add_row(entry["name"] + mark, entry["callable"], str(entry["threshold"]))
        console.print(ctable)

    console.print(
        "Judge metrics are mandatory unless listed under quality_metrics in "
        f"{_paths.DEFAULT_EVAL_CONFIG}; custom metrics are module:function callables "
        "run inside the project's environment."
    )


metric_group.add_lazy_command(
    "list",
    "graph_agents_cli.eval.cmd_metric:list_metrics",
    "List deterministic checks and built-in judges (plus this project's config).",
)
