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

"""Version, install-spec and update-check utilities for the CLI.

``install_spec()`` is the single place that says where the CLI is installed
from: ``setup``, ``update``, the ``scaffold upgrade`` baseline and the CI
workflows of every generated project use it. It is a pinned git reference to
the GitHub repository today and can be flipped to a package-index requirement
later without touching the callers; ``GRAPH_AGENTS_CLI_INSTALL_SPEC`` overrides
it (a private mirror, a local checkout, a wheel). A ``{version}`` in the override
is replaced by the version asked for, so one mirror setting serves every
release. Replaying an older release (the ``scaffold upgrade`` baseline, a
version-locked ``scaffold enhance``) uses ``pinned_install_spec``, which refuses
an override without ``{version}``: that override installs one fixed build, so it
cannot stand for the older version.

The update check compares the running version with the latest GitHub release.
It is opt-out through ``GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1`` (disconnected
installs) and fails silently when GitHub is unreachable.
"""

import logging
import os
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import click
from packaging import version as pkg_version

from graph_agents_cli._output import Console

console = Console(stderr=True)

PACKAGE_NAME = "graph-agents-cli"
REPO_URL = "https://github.com/ss7172/graph-agents-cli"
INSTALL_SPEC_ENV = "GRAPH_AGENTS_CLI_INSTALL_SPEC"
VERSION_PLACEHOLDER = "{version}"
LATEST_RELEASE_URL = "https://api.github.com/repos/ss7172/graph-agents-cli/releases/latest"
NO_UPDATE_CHECK_ENV = "GRAPH_AGENTS_CLI_NO_UPDATE_CHECK"
# The 0.0.0 sentinel used when a real version can't be determined: an
# uninstalled/dev checkout (get_current_version) or an unreachable release
# feed (get_latest_version). Not a real release, so it can't be fetched.
UNKNOWN_VERSION = "0.0.0"
_UPDATE_CHECK_INTERVAL = 12 * 60 * 60  # 12 hours in seconds
_UPDATE_CHECK_STAMP = Path.home() / ".config" / "graph-agents-cli" / "update_check"


class InstallSpecError(click.ClickException):
    """``GRAPH_AGENTS_CLI_INSTALL_SPEC`` cannot give the spec asked for (exit 3)."""

    exit_code = 3


def _install_spec_override() -> str:
    return os.environ.get(INSTALL_SPEC_ENV, "").strip()


def install_spec(version: str | None = None) -> str:
    """Where to install graph-agents-cli from, for ``uv tool install`` / ``uvx --from``.

    ``GRAPH_AGENTS_CLI_INSTALL_SPEC`` wins when set, with ``{version}`` replaced
    by ``version`` (``InstallSpecError`` when the override needs a version and
    none is known). Otherwise a released ``version`` pins the git tag
    ``v<version>``, and no version (or the ``0.0.0`` unknown-version sentinel)
    names the repository's default branch.
    """
    known = version if version and version != UNKNOWN_VERSION else None
    override = _install_spec_override()
    if override:
        if VERSION_PLACEHOLDER not in override:
            return override
        if known is None:
            raise InstallSpecError(
                f"{INSTALL_SPEC_ENV} contains {VERSION_PLACEHOLDER}, but no "
                f"{PACKAGE_NAME} version is known here (an uninstalled checkout, or no "
                f"release found); set it to a spec without {VERSION_PLACEHOLDER}."
            )
        return override.replace(VERSION_PLACEHOLDER, known)
    if known:
        return f"git+{REPO_URL}@v{known}"
    return f"git+{REPO_URL}"


def pinned_install_spec(version: str) -> str | None:
    """The spec that installs exactly ``version``, or None when none can be trusted to.

    For replaying an older release: the ``scaffold upgrade`` baseline and a
    version-locked ``scaffold enhance``. An override without ``{version}``
    installs one fixed build whatever version is asked, so a snapshot rendered
    by it would be taken for ``version``'s and misclassify files; None then
    (``pinned_spec_unavailable`` says why and what to do).
    """
    override = _install_spec_override()
    if override and VERSION_PLACEHOLDER not in override:
        return None
    return install_spec(version)


def pinned_spec_unavailable(version: str) -> str:
    """Why ``pinned_install_spec(version)`` is None, and the ways out."""
    return (
        f"{INSTALL_SPEC_ENV} is set without {VERSION_PLACEHOLDER}, so it cannot install "
        f"{PACKAGE_NAME} {version} specifically. Put {VERSION_PLACEHOLDER} where the "
        f"version goes (for example git+https://git.example.com/{PACKAGE_NAME}@v{VERSION_PLACEHOLDER}), "
        f"or unset it"
    )


def requirement(spec: str | None = None, extras: str | None = None) -> str:
    """A requirement string for ``spec`` (default ``install_spec()``), with optional extras.

    A URL or path spec becomes ``graph-agents-cli[extras] @ <spec>``; a
    name-based spec (``graph-agents-cli==1.2.3``) gets the extras after the name.
    """
    spec = spec or install_spec()
    if not extras:
        return spec
    if spec.startswith(PACKAGE_NAME):
        return f"{PACKAGE_NAME}[{extras}]{spec[len(PACKAGE_NAME) :]}"
    return f"{PACKAGE_NAME}[{extras}] @ {spec}"


def install_command(extras: str | None = None, *, version: str | None = None) -> str:
    """The ``uv tool install`` command line users can copy (quoted for a POSIX shell)."""
    import shlex

    try:
        spec = install_spec(version)
    except InstallSpecError:
        spec = _install_spec_override()  # a hint: the user fills in {version}
    return shlex.join(["uv", "tool", "install", "--force", requirement(spec, extras)])


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


def cli_install_spec() -> str:
    """The install spec a generated project pins: the creating CLI's own version."""
    return install_spec(get_current_version())


def get_latest_version() -> str:
    """The latest GitHub release of the CLI (tag ``v<version>``); UNKNOWN_VERSION when unknown.

    Unknown covers offline, rate-limited, no release yet and a tag that is not
    a version.
    """
    try:
        import requests

        response = requests.get(
            LATEST_RELEASE_URL,
            timeout=2,
            headers={"Accept": "application/vnd.github+json"},
        )
        if response.status_code != 200:
            return UNKNOWN_VERSION
        tag = str(response.json().get("tag_name") or "")
        latest = tag[1:] if tag.startswith("v") else tag
        pkg_version.Version(latest)
        return latest
    except Exception:
        return UNKNOWN_VERSION  # GitHub couldn't be reached or answered unexpectedly


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
                f"[yellow]Run `{PACKAGE_NAME} update` or `{install_command(version=latest)}`.[/]",
                highlight=False,
            )
    except Exception as e:
        # Don't let version checking errors affect the CLI
        logging.debug("Error checking for updates: %s", e)
