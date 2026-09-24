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

"""graph-agents-cli lint command: ruff plus the API-policy check of every tool."""

from __future__ import annotations

from pathlib import Path

import click

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
    """
    chdir_project_root()
    cfg = read_project_config()
    ensure_no_legacy_api_policy(Path.cwd())
    if not policy_only:
        lint_python(fix=fix)
    violations = run_policy_check(
        Path.cwd(),
        cfg.agent_directory,
        policy_file=cfg.api_policy_file or POLICY_FILENAME,
        runtime=cfg.runtime,
        policy_declared=bool(cfg.api_policy_file),
    )
    if violations:
        raise click.ClickException(
            f"API policy check failed: {violations} violation(s). Fix the tool declarations, "
            "or change api-policy.yaml with `graph-agents-cli api` (the commands above) in a "
            "reviewed pull request."
        )


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
