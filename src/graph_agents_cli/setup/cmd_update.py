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

"""graph-agents-cli update command — update the skills (npx skills) and the CLI itself.

The CLI is reinstalled from ``install_spec()`` pinned to the latest GitHub
release (``uv tool install --force``) when that release is newer than the
running version; ``GRAPH_AGENTS_CLI_INSTALL_SPEC`` forces a reinstall from the
override. ``uv tool upgrade`` cannot move a git-pinned install to a new tag,
which is why the install is forced.
"""

from __future__ import annotations

import os
import shlex

import click
from packaging import version as pkg_version

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

    Refreshes the installed skills via npx skills, then reinstalls the CLI
    from the latest GitHub release (best effort) and, when it did, installs
    that release's skills (pinned to its tag) so the two stay in step.
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
    from graph_agents_cli.scaffold.utils.version import (
        INSTALL_SPEC_ENV,
        UNKNOWN_VERSION,
        InstallSpecError,
        InvalidInstallSpecError,
        get_current_version,
        get_latest_version,
        install_spec,
    )

    latest = get_latest_version()
    overridden = bool(os.environ.get(INSTALL_SPEC_ENV, "").strip())
    if latest == UNKNOWN_VERSION and not overridden:
        click.secho(
            f"  No {PACKAGE_NAME} release found on GitHub (offline, or none published yet); "
            "the CLI was left as is.",
            dim=True,
        )
        return
    current = get_current_version()
    if (
        not overridden
        and current != UNKNOWN_VERSION
        and pkg_version.parse(latest) <= pkg_version.parse(current)
    ):
        click.secho(f"  {PACKAGE_NAME} {current} is up to date.", dim=True)
        return
    try:
        spec = install_spec(None if latest == UNKNOWN_VERSION else latest)
    except InvalidInstallSpecError:
        # A malformed override is a configuration error (exit 3), not a
        # transient failure of the best-effort upgrade.
        raise
    except InstallSpecError as exc:  # {version} in the override, no release known
        click.secho(f"  {exc.format_message()} The CLI was left as is.", fg="yellow")
        return
    cmd = ["uv", "tool", "install", "--force", spec]
    result = run(cmd, check=False)
    if result.returncode != 0:
        click.secho(
            f"  Could not upgrade {PACKAGE_NAME} automatically "
            f"(exit code {result.returncode}); run '{shlex.join(cmd)}' manually.",
            fg="yellow",
        )
        return
    if latest == UNKNOWN_VERSION:
        return
    # Skills are pinned to a release tag: move them to the one just installed.
    from graph_agents_cli._tools import default_skills_source

    skills_args = ["add", default_skills_source(latest), "-y"]
    if not workspace:
        skills_args.append("-g")
    try:
        run_npx_skills(skills_args, f"Installing the skills of {latest}")
    except click.ClickException as exc:
        click.secho(
            f"  Could not install the skills of {latest} ({exc.format_message()}); run "
            f"'{PACKAGE_NAME} setup' with the new CLI.",
            fg="yellow",
        )
