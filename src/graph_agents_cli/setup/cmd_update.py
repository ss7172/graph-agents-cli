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

"""graph-agents-cli update command — update skills via npx skills CLI."""

from __future__ import annotations

import click

from graph_agents_cli._runner import run
from graph_agents_cli._tools import run_npx_skills
from graph_agents_cli._trust import require_confirmation

PACKAGE_NAME = "graph-agents-cli"


@click.command("update")
@click.option(
    "--workspace",
    is_flag=True,
    default=False,
    help="Update workspace-level skills instead of global.",
)
@require_confirmation(f"This will force-reinstall {PACKAGE_NAME} skills to all detected IDEs.")
def cmd_update(workspace, auto_approve):
    """Force reinstall skills to all detected coding agents.

    Updates all installed skills to their latest versions via npx skills,
    then upgrades the CLI itself with `uv tool upgrade` (best effort).
    """
    click.echo()
    args = ["update"]
    if not workspace:
        args.append("-g")

    run_npx_skills(args, "Updating skills")

    # Temporary until npx skills supports Antigravity's IDE / CLI / 2.0 paths:
    # refresh the mirrored skill links so the IDE/2.0 and CLI see the update.
    # TODO: remove once Antigravity/npx align on skill paths.
    if not workspace:
        from graph_agents_cli.setup._antigravity import link_skills_for_antigravity

        for line in link_skills_for_antigravity():
            click.echo(f"  {line}")

    # Only reached if run_npx_skills did not raise (i.e. all skills updated
    # successfully). Avoid bold colors here: bold-bright-green renders as
    # hard-to-read white-on-green in some Windows terminals.
    click.echo()
    click.secho("✓ Skills updated.", fg="green")

    # Best-effort CLI upgrade: a failure (offline, not installed with uv tool)
    # must not turn a successful skills update into a non-zero exit.
    click.echo()
    result = run(["uv", "tool", "upgrade", PACKAGE_NAME], check=False)
    if result.returncode != 0:
        click.secho(
            f"  Could not upgrade {PACKAGE_NAME} automatically "
            f"(exit code {result.returncode}); run 'uv tool upgrade {PACKAGE_NAME}' manually.",
            fg="yellow",
        )
