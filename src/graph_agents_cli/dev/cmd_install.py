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

"""graph-agents-cli install command: install project dependencies with uv."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import click

from graph_agents_cli import _tools
from graph_agents_cli._project import chdir_project_root, find_project_root
from graph_agents_cli._runner import run


@click.command("install")
@click.option(
    "--clean",
    is_flag=True,
    help="Delete and recreate the uv virtual environment (e.g. after moving the project folder).",
)
@click.option(
    "--locked",
    is_flag=True,
    help="Assert that uv.lock is up to date with pyproject.toml; fail instead of updating it.",
)
def cmd_install(clean: bool, locked: bool) -> None:
    """Install project dependencies."""
    chdir_project_root()
    # Re-materialize any missing extension working copies from their pinned
    # SHAs before syncing: the loader tells a user with a missing copy to run
    # this command. Never advances pins.
    from graph_agents_cli.extension._sync import sync_extensions

    sync_extensions(find_project_root(Path.cwd()))
    install_python(clean=clean, locked=locked)


def install_python(*, clean: bool, locked: bool) -> None:
    """Run ``uv sync`` (``--locked`` when asked), deleting ``.venv`` first under ``--clean``."""
    # Resolve uv up front: --clean deletes the venv. Fail quickly here (before
    # any destructive work) rather than when calling uv.
    _tools.require_tool("uv")
    if clean:
        _delete_venv()
    cmd = ["uv", "sync"]
    if locked:
        cmd.append("--locked")
    run(cmd, check_err_msg="Failed to install dependencies")


def _delete_venv() -> None:
    root = find_project_root()
    if not root:
        logging.warning("Could not find the project root; nothing to clean.")
        return
    venv_path = root / ".venv"
    if not venv_path.exists():
        return
    try:
        shutil.rmtree(venv_path)
    except Exception as exc:
        logging.warning("Failed to remove venv: %s", exc)
