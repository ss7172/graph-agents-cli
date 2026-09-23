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

"""Version checking utilities for the CLI.

The update check is opt-out through ``GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1``
(DECISIONS.md D21) and fails silently when the package index is unreachable.
"""

import logging
import os
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from packaging import version as pkg_version

from graph_agents_cli._output import Console

console = Console(stderr=True)

# Single source for the package name (ASSUMPTIONS item 2); consumed by the
# upgrade baseline in merge.py.
PACKAGE_NAME = "graph-agents-cli"
PACKAGE_INDEX_URL = f"https://pypi.org/pypi/{PACKAGE_NAME}/json"
NO_UPDATE_CHECK_ENV = "GRAPH_AGENTS_CLI_NO_UPDATE_CHECK"
# The 0.0.0 sentinel used when a real version can't be determined: an
# uninstalled/dev checkout (get_current_version) or an unreachable index
# (get_latest_version). Not a real release, so it can't be fetched.
UNKNOWN_VERSION = "0.0.0"
_UPDATE_CHECK_INTERVAL = 12 * 60 * 60  # 12 hours in seconds
_UPDATE_CHECK_STAMP = Path.home() / ".config" / "graph-agents-cli" / "update_check"


def update_check_disabled() -> bool:
    """True when the user opted out of the update check."""
    return os.environ.get(NO_UPDATE_CHECK_ENV) == "1"


def _update_check_is_due() -> bool:
    """Return True if enough time has elapsed since the last check."""
    try:
        last = float(_UPDATE_CHECK_STAMP.read_text().strip())
        return (time.time() - last) > _UPDATE_CHECK_INTERVAL
    except (OSError, ValueError):
        return True


def _record_update_check() -> None:
    """Write the current timestamp to the stamp file."""
    try:
        _UPDATE_CHECK_STAMP.parent.mkdir(parents=True, exist_ok=True)
        _UPDATE_CHECK_STAMP.write_text(str(time.time()))
    except OSError:
        pass


def get_current_version() -> str:
    """Get the current installed version of the package."""
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        # Package isn't installed (editable / dev checkout).
        return UNKNOWN_VERSION


def agents_cli_version_pin() -> str:
    """Return the version suffix to pin ``graph-agents-cli`` in generated projects.

    Renders to ``@<version>`` for released builds and to an empty string for
    the ``0.0.0`` unknown-version sentinel, so local renders against an
    uninstalled package leave the CI steps using an unpinned ``graph-agents-cli``.
    """
    current_version = get_current_version()
    if current_version == UNKNOWN_VERSION:
        return ""
    return f"@{current_version}"


def get_latest_version() -> str:
    """Get the latest version available on the package index; UNKNOWN_VERSION offline."""
    try:
        import requests

        response = requests.get(PACKAGE_INDEX_URL, timeout=2)
        if response.status_code == 200:
            return response.json()["info"]["version"]
        return UNKNOWN_VERSION
    except Exception:
        return UNKNOWN_VERSION  # the index couldn't be reached


def check_for_updates() -> tuple[bool, str, str]:
    """Check if a newer version of the package is available.

    Returns:
        Tuple of (needs_update, current_version, latest_version)
    """
    current = get_current_version()
    latest = get_latest_version()

    needs_update = pkg_version.parse(latest) > pkg_version.parse(current)

    return needs_update, current, latest


def display_update_message() -> None:
    """Check for updates and display a message if an update is available.

    Skipped when ``GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1`` or when a check ran
    recently; any failure (offline, index down) is logged at debug level only.
    """
    if update_check_disabled() or not _update_check_is_due():
        return

    try:
        needs_update, current, latest = check_for_updates()

        # We only record the check if we successfully queried it
        _record_update_check()

        if needs_update:
            console.print(
                f"\n[yellow]⚠️  Update available: {current} → {latest}[/]",
                highlight=False,
            )
            console.print(
                f"[yellow]Run `uv tool upgrade {PACKAGE_NAME}` to update.[/]",
                highlight=False,
            )
            console.print(
                f"[dim]If you installed differently: pip install --upgrade {PACKAGE_NAME} | pipx upgrade {PACKAGE_NAME}[/]",
                highlight=False,
            )
    except Exception as e:
        # Don't let version checking errors affect the CLI
        logging.debug("Error checking for updates: %s", e)
