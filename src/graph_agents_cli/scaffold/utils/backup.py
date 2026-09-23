# Copyright 2026 Google LLC
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

"""Shared backup utility for project directories."""

import datetime
import pathlib
import shutil
from collections.abc import Callable

import click

from graph_agents_cli._output import Console

from .fs import standard_ignore_patterns

BACKUP_BASE_DIR = pathlib.Path.home() / ".graph-agents-cli" / "backups"


def create_project_backup(
    project_dir: pathlib.Path,
    console: Console | None = None,
    auto_approve: bool = False,
    interactive: bool = False,
) -> pathlib.Path | None:
    """Create a backup of the project directory.

    Backs up to ~/.graph-agents-cli/backups/<project-name>_<timestamp>/.

    Args:
        project_dir: Path to the project directory to back up.
        console: Rich console for output. Created if not provided.
        auto_approve: If True, skip confirmation prompts on failure.
        interactive: If True, show interactive prompt on backup failure.

    Returns:
        Path to the backup directory on success, None if backup failed
        but user chose to continue.

    Raises:
        click.Abort: If backup failed and user chose to cancel (interactive mode only).
    """
    if console is None:
        console = Console()

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = BACKUP_BASE_DIR / f"{project_dir.name}_{timestamp}"

    console.print("📦 [blue]Creating backup before modification...[/blue]")

    try:
        BACKUP_BASE_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copytree(project_dir, backup_dir, ignore=standard_ignore_patterns)
        console.print(f"Backup created: [cyan]{backup_dir}[/cyan]")
        return backup_dir
    except Exception as e:
        console.print(f"⚠️  [yellow]Warning: Could not create backup: {e}[/yellow]")
        if interactive:
            if not click.confirm("Continue without backup?", default=True):
                raise click.Abort() from e
        return None


def make_backup_pre_apply_hook(
    *,
    console: Console,
    auto_approve: bool,
    interactive: bool,
) -> Callable[[pathlib.Path], bool]:
    """Build a ``run_three_way_merge`` pre-apply hook that backs up the project.

    The merge invokes the hook right before it writes changes; returning False
    aborts the merge. This wraps ``create_project_backup`` so a user cancelling
    the "continue without backup?" prompt cleanly stops the operation.

    Args:
        console: Rich console for output.
        auto_approve: If True, skip confirmation prompts on failure.
        interactive: If True, prompt the user on backup failure.

    Returns:
        A hook taking the project directory and returning True to proceed,
        False if the user cancelled.
    """

    def _backup(proj_dir: pathlib.Path) -> bool:
        try:
            create_project_backup(
                proj_dir,
                console=console,
                auto_approve=auto_approve,
                interactive=interactive,
            )
            return True
        except click.Abort:
            return False  # user cancelled

    return _backup
