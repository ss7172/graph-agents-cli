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
"""graph-agents-cli infra commands — read-only prerequisite checks (D10)."""

from __future__ import annotations

import json

import click

from graph_agents_cli._click import LazyGroup
from graph_agents_cli._output import Console
from graph_agents_cli.deploy._config import load_settings
from graph_agents_cli.infra import checks as _checks


@click.group("infra", cls=LazyGroup)
def infra_group() -> None:
    """Check cluster and repository prerequisites (read-only).

    \b
    Subcommands:
      check   Report which prerequisites exist for the selected environment and mode
    """


_STYLES = {
    _checks.OK: "green",
    _checks.MISSING: "red",
    _checks.WARN: "yellow",
    _checks.SKIP: "dim",
    _checks.INFO: "cyan",
}


@infra_group.command("check")
@click.option(
    "--env", "env", default=None, help="Environment to check the cluster for (dev, staging, prod)."
)
@click.option(
    "--profile",
    "profile",
    type=click.Choice(_checks.PROFILES),
    default=None,
    help="Also verify the named profile (D25).",
)
@click.option("--json", "as_json", is_flag=True, help="Print the report as JSON.")
def cmd_infra_check(env: str | None, profile: str | None, as_json: bool) -> None:
    """Report which prerequisites exist for the selected environment and mode."""
    settings = load_settings()
    report = _checks.run_checks(settings, env, profile)
    if as_json:
        click.echo(json.dumps(report.to_dict(), indent=2))
    else:
        _print_table(report)
    if not report.ok:
        raise SystemExit(1)


def _print_table(report: _checks.Report) -> None:
    from rich.table import Table

    console = Console()
    title = f"infra check: mode {report.mode}"
    if report.env:
        title += f", env {report.env}"
    if report.profile:
        title += f", profile {report.profile}"
    table = Table(title=title, show_lines=False)
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Required")
    table.add_column("Detail")
    for check in report.checks:
        style = _STYLES.get(check.status, "")
        table.add_row(
            check.name,
            f"[{style}]{check.status}[/{style}]",
            "yes" if check.required else "no",
            check.detail,
        )
    console.print(table)
    hints = [c for c in report.checks if c.hint and c.status in (_checks.MISSING, _checks.WARN)]
    if hints:
        console.print("Hints:")
        for check in hints:
            console.print(f"  - {check.name}: {check.hint}", highlight=False)
    if report.ok:
        console.print("All required prerequisites are present.", style="green")
    else:
        failed = ", ".join(c.name for c in report.checks if c.failed)
        console.print(f"Missing required prerequisites: {failed}", style="red")
