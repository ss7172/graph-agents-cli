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

"""graph-agents-cli eval command group."""

import click

from graph_agents_cli._click import LazyGroup


@click.group("eval", cls=LazyGroup)
def eval_group():
    """Evaluate agents and compare results.

    \b
    Core:
      run       Chain generate + grade in one command
      generate  Run the agent over the eval dataset and write traces
      grade     Grade traces against checks and judges; apply the gate
      compare   Compare two eval results files
      metric    Discover evaluation metrics

    \b
    Analysis and sharing:
      analyze   Cluster failures by reason (optionally judge-summarised)
      submit    Upload a dataset and results to LangSmith (optional)

    \b
    Exit codes (run, generate, grade):
      0 gate met, 1 gate failed, 2 incomplete run, 3 configuration error
    """


eval_group.add_lazy_command(
    "run",
    "graph_agents_cli.eval.cmd_run:cmd_run",
    "Chain `eval generate` and `eval grade` in one command.",
)
eval_group.add_lazy_command(
    "generate",
    "graph_agents_cli.eval.cmd_generate:cmd_generate",
    "Run the agent over the eval dataset and write traces.",
)
eval_group.add_lazy_command(
    "grade",
    "graph_agents_cli.eval.cmd_grade:cmd_grade",
    "Grade traces against the dataset's checks and judges and apply the gate.",
)
eval_group.add_lazy_command(
    "compare",
    "graph_agents_cli.eval.cmd_compare:cmd_compare",
    "Compare two eval results files (baseline, candidate).",
)
eval_group.add_lazy_command(
    "analyze",
    "graph_agents_cli.eval.cmd_analyze:cmd_analyze",
    "Cluster failed, quality-below-threshold, error and missing cases by reason.",
)
eval_group.add_lazy_command(
    "submit",
    "graph_agents_cli.eval.cmd_submit:cmd_submit",
    "Upload the dataset and a results file to LangSmith as an experiment.",
)
eval_group.add_lazy_command(
    "metric",
    "graph_agents_cli.eval.cmd_metric:metric_group",
    "Discover evaluation metrics: deterministic checks and judges.",
)
