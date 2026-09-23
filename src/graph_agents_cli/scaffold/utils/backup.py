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

"""Shared backup utility for project directories.

A backup is the undo of ``enhance`` and ``upgrade``, so it copies the whole
project, ``.env`` included (both commands merge it, and a generated project
gitignores it, so the backup may hold the user's only copy). Because of that it
is private: the backups directory and every backup are 0700 and every ``.env*``
file in them 0600. Only the newest ``KEEP_BACKUPS`` backups of a project are
kept, so credentials do not pile up in the home directory.

A backup is named ``<directory>_<project id>_<timestamp>[_n]``. The project id
is a hash of the project's resolved absolute path and its manifest ``name``, so
two checkouts with the same directory name (``my-agent`` in two places) never
count as one project: pruning only ever deletes backups carrying the id of the
project being backed up. Backups named without an id (made by an older CLI)
are never pruned.
"""

import contextlib
import datetime
import hashlib
import os
import pathlib
import re
import shutil
from collections.abc import Callable

import click
import yaml

from graph_agents_cli._output import Console

from .fs import standard_ignore_patterns

BACKUP_BASE_DIR = pathlib.Path.home() / ".graph-agents-cli" / "backups"
# Backups kept per project (by project id); older ones are deleted.
KEEP_BACKUPS = 5
_PRIVATE_DIR = 0o700
_PRIVATE_FILE = 0o600
_MANIFEST_FILENAME = "graph-agents-cli-manifest.yaml"
_ID_LENGTH = 12


def project_backup_id(project_dir: pathlib.Path) -> str:
    """A stable id for ``project_dir``: its resolved absolute path plus its manifest ``name``."""
    name = ""
    try:
        data = yaml.safe_load((project_dir / _MANIFEST_FILENAME).read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("name") is not None:
            name = str(data["name"])
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        pass  # no (readable) manifest: the path alone identifies the directory
    key = f"{project_dir.resolve()}\0{name}".encode()
    return hashlib.sha256(key).hexdigest()[:_ID_LENGTH]


def _backup_prefix(project_dir: pathlib.Path) -> str:
    return f"{project_dir.name}_{project_backup_id(project_dir)}"


def create_project_backup(
    project_dir: pathlib.Path,
    console: Console | None = None,
    auto_approve: bool = False,
    interactive: bool = False,
) -> pathlib.Path | None:
    """Create a backup of the project directory.

    Backs up to ~/.graph-agents-cli/backups/<directory>_<project id>_<timestamp>/.

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
    prefix = _backup_prefix(project_dir)
    backup_dir = BACKUP_BASE_DIR / f"{prefix}_{timestamp}"
    suffix = 1
    while backup_dir.exists():  # two backups within the same second
        suffix += 1
        backup_dir = BACKUP_BASE_DIR / f"{prefix}_{timestamp}_{suffix}"

    console.print("📦 [blue]Creating backup before modification...[/blue]")

    try:
        BACKUP_BASE_DIR.mkdir(parents=True, exist_ok=True)
        _make_private_dir(BACKUP_BASE_DIR)
        shutil.copytree(project_dir, backup_dir, ignore=standard_ignore_patterns)
        _make_private_tree(backup_dir)
        console.print(f"Backup created: [cyan]{backup_dir}[/cyan]")
        pruned = prune_backups(project_dir, keep=KEEP_BACKUPS, current=backup_dir)
        if pruned:
            console.print(
                f"[dim]Removed {len(pruned)} older backup(s) of {project_dir.name} "
                f"(the newest {KEEP_BACKUPS} are kept).[/dim]"
            )
        return backup_dir
    except Exception as e:
        console.print(f"⚠️  [yellow]Warning: Could not create backup: {e}[/yellow]")
        if interactive:
            if not click.confirm("Continue without backup?", default=True):
                raise click.Abort() from e
        return None


def _make_private_dir(path: pathlib.Path) -> None:
    with contextlib.suppress(OSError):
        os.chmod(path, _PRIVATE_DIR)


def _make_private_tree(root: pathlib.Path) -> None:
    """0700 on the backup and its directories, 0600 on the env files it holds."""
    _make_private_dir(root)
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            _make_private_dir(pathlib.Path(dirpath) / name)
        for name in filenames:
            if name.startswith(".env"):
                with contextlib.suppress(OSError):
                    os.chmod(pathlib.Path(dirpath) / name, _PRIVATE_FILE)


def prune_backups(
    project_dir: pathlib.Path, *, keep: int = KEEP_BACKUPS, current: pathlib.Path | None = None
) -> list[pathlib.Path]:
    """Delete all but the newest ``keep`` backups of ``project_dir``; return what was removed.

    Only directories named exactly ``<directory>_<project id>_<YYYYmmdd>_<HHMMSS>[_n]``
    with this project's id are considered: another project is never touched,
    whether its name shares a prefix or is the same (another checkout), backups
    without an id are left alone, and ``current`` (the backup just made) is
    never removed.
    """
    if not BACKUP_BASE_DIR.is_dir():
        return []
    prefix = _backup_prefix(project_dir)
    pattern = re.compile(rf"^{re.escape(prefix)}_(\d{{8}}_\d{{6}})(?:_(\d+))?$")
    found: list[tuple[str, int, pathlib.Path]] = []
    for entry in BACKUP_BASE_DIR.iterdir():
        match = pattern.match(entry.name)
        if match and entry.is_dir() and not entry.is_symlink():
            found.append((match.group(1), int(match.group(2) or 1), entry))
    found.sort(reverse=True)
    removed: list[pathlib.Path] = []
    for _stamp, _n, entry in found[max(keep, 1) :]:
        if current is not None and entry == current:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        removed.append(entry)
    return removed


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
