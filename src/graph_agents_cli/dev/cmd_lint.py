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

"""graph-agents-cli lint command: ruff, the API-policy check of every tool, the response schema."""

from __future__ import annotations

from pathlib import Path

import click

from graph_agents_cli import _response_schema
from graph_agents_cli._api_policy import POLICY_FILENAME, ensure_no_legacy_api_policy
from graph_agents_cli._project import chdir_project_root, read_project_config
from graph_agents_cli._runner import run
from graph_agents_cli.dev.policy_check import run_policy_check


@click.command("lint")
@click.option("--fix", is_flag=True, default=False, help="Auto-fix lint and formatting issues.")
@click.option(
    "--policy-only",
    "policy_only",
    is_flag=True,
    default=False,
    help="Run only the API-policy check (skip ruff).",
)
def cmd_lint(fix: bool, policy_only: bool) -> None:
    """Run code quality checks and the API-policy check.

    \b
    ruff check .            (--fix applies fixes)
    ruff format . --check   (--fix reformats in place)
    API-policy check        api-policy.yaml must pass the strict schema, and
                            every API_CALLS entry of every tool must name a
                            declared API and be allowed by its rules (and
                            exist in its OpenAPI spec when one is named)
    Response schema         <agent directory>/response_schema.json, when the
                            project has one (structured final answers), must
                            be a schema the agent starts with; a warning when
                            agent.py does not pass response_format() to the
                            agent or has no StructuredAnswer() in its
                            middleware

    \b
    Exit codes:
      0  clean
      1  a refused call or an unreadable API_CALLS, or ruff failed
      3  configuration error: an invalid api-policy.yaml or response
         schema, the retired product policy, or not in a project
    """
    chdir_project_root()
    cfg = read_project_config()
    ensure_no_legacy_api_policy(Path.cwd())
    check_response_schema(Path.cwd(), cfg.agent_directory)
    if not policy_only:
        lint_python(fix=fix)
    violations = run_policy_check(
        Path.cwd(),
        cfg.agent_directory,
        policy_file=cfg.api_policy_file or POLICY_FILENAME,
        runtime=cfg.runtime,
        policy_declared=bool(cfg.api_policy_file),
        auth_policy=cfg.auth_policy,
    )
    if violations:
        raise click.ClickException(
            f"API policy check failed: {violations} violation(s). Fix the tool declarations, "
            "or change api-policy.yaml with `graph-agents-cli api` (the commands above) in a "
            "reviewed pull request."
        )


def check_response_schema(project_root: Path, agent_directory: str) -> None:
    """The project's response schema, when it has one: refused (exit 3) unless the agent starts
    with it; a warning when ``agent.py`` does not build the agent with ``response_format()`` or
    lacks ``StructuredAnswer()`` in its middleware."""
    path = _response_schema.schema_file(project_root, agent_directory)
    if not path.is_file():
        return
    _response_schema.load_schema(path)
    problems = _response_schema.wiring_problems(project_root, agent_directory)
    if problems:
        click.secho(
            f"Warning: {agent_directory}/{_response_schema.SCHEMA_FILENAME} declares structured "
            f"answers, but {'; and '.join(problems)}. Pass "
            "response_format=response_format(model, tools) to create_agent and put "
            "StructuredAnswer() last in middleware(); see the template's "
            "app_utils/structured.py.",
            fg="yellow",
            err=True,
        )
    else:
        click.echo(f"Response schema: {agent_directory}/{_response_schema.SCHEMA_FILENAME} OK")


def lint_python(*, fix: bool) -> None:
    """Run ``ruff check`` and ``ruff format`` in the project."""
    if fix:
        run(["uv", "run", "ruff", "check", ".", "--fix"], check_err_msg="Ruff check --fix failed")
        run(["uv", "run", "ruff", "format", "."], check_err_msg="Ruff format failed")
    else:
        run(["uv", "run", "ruff", "check", "."], check_err_msg="Ruff check failed")
        run(
            ["uv", "run", "ruff", "format", ".", "--check"],
            check_err_msg="Ruff format check failed",
        )
