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
cannot stand for the older version. ``{version}`` is a release number (the
``cli_version`` a manifest records), so the override selects releases only; a
build between two releases (same version, another commit) is named with
``scaffold upgrade --baseline-ref`` (``resolve_baseline_ref``).

The update check compares the running version with the latest GitHub release.
It is opt-out through ``GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1`` (disconnected
installs) and fails silently when GitHub is unreachable.
"""

import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass
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


class InvalidInstallSpecError(InstallSpecError):
    """``GRAPH_AGENTS_CLI_INSTALL_SPEC`` is malformed: whitespace or control characters (exit 3)."""


# A PEP 508 direct reference, `graph-agents-cli[extras] @ <url>`: the one form
# whose spec holds whitespace, exactly the single spaces around its `@`.
_DIRECT_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._,-]*\])? @ \S+")


def _invalid_character(value: str) -> str | None:
    """Describe the first character an install spec may not contain, or None.

    Control and format characters (newline, CR, NUL, escape, bidi overrides...)
    and every whitespace character are refused, except the two spaces of a
    PEP 508 direct reference (``name @ url``).
    """
    names = {
        "\n": "a newline",
        "\r": "a carriage return",
        "\0": "a NUL byte",
        "\t": "a tab",
        " ": "a space",
    }
    for position, char in enumerate(value):
        category = unicodedata.category(char)
        if char == " ":
            continue  # judged below, once nothing worse was found
        if char in names:
            what = names[char]
        elif char.isspace() or category in ("Zl", "Zp"):
            what = f"whitespace (U+{ord(char):04X})"
        elif category in ("Cc", "Cf"):
            what = f"a control character (U+{ord(char):04X})"
        else:
            continue
        return f"{what} at position {position + 1}"
    if " " in value and _DIRECT_REFERENCE.fullmatch(value) is None:
        return f"{names[' ']} at position {value.index(' ') + 1}"
    return None


def _install_spec_override() -> str:
    """The override, stripped; :class:`InvalidInstallSpecError` when it cannot be a spec.

    The spec is written into every generated project's ``.github/agent.env``
    (one NAME=VALUE per line, loaded into ``$GITHUB_ENV``) and passed to ``uv``
    as one argument: a newline would add lines of its own to the workflows'
    environment, and stray whitespace or an invisible control character would
    make a different command than the one shown. It is refused instead.
    """
    value = os.environ.get(INSTALL_SPEC_ENV, "").strip()
    problem = _invalid_character(value)
    if problem is not None:
        shown = repr(value) if len(value) <= 120 else repr(value[:117]) + "..."
        raise InvalidInstallSpecError(
            f"{INSTALL_SPEC_ENV} contains {problem}: {shown}.\n"
            "  It must be a single install spec (a git URL such as "
            f"git+https://git.example.com/{PACKAGE_NAME}@v{VERSION_PLACEHOLDER}, a local "
            f"path, a wheel, {PACKAGE_NAME}==<version>, or {PACKAGE_NAME} @ <url>) with no "
            "control characters and no whitespace (except the spaces around the @ of "
            f"'{PACKAGE_NAME} @ <url>'). Fix it or unset it."
        )
    return value


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
    return _default_install_spec(known)


def _default_install_spec(version: str | None) -> str:
    """The repository's spec: the ``v<version>`` tag, or the default branch."""
    if version and version != UNKNOWN_VERSION:
        return f"git+{REPO_URL}@v{version}"
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


@dataclass(frozen=True)
class BaselineSource:
    """The CLI build that renders an upgrade's old snapshot, as a ``uvx --from`` spec.

    ``refresh`` rebuilds a local directory whatever uv cached for it: a checkout
    at an older commit may key its build cache on ``pyproject.toml`` alone.
    """

    spec: str
    label: str
    refresh: bool = False
    commit: str | None = None


# A git ref as `--baseline-ref` accepts it: a tag, a branch or a commit.
_GIT_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/+-]*")
_FULL_COMMIT_RE = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")


def _check_git_ref(ref: str, flag: str) -> None:
    if not _GIT_REF_RE.fullmatch(ref) or ".." in ref or ref.endswith((".lock", "/", ".")):
        raise InstallSpecError(
            f"{flag} {ref!r} is not a git ref (a tag, a branch or a commit such as 1a2b3c4)."
        )


def _resolve_local_commit(clone: Path, ref: str, flag: str) -> str:
    """``ref``'s full commit in the clone at ``clone`` (InstallSpecError when it has none)."""
    from graph_agents_cli._runner import run_resolved

    try:
        result = run_resolved(
            ["git", "-C", str(clone), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as e:
        raise InstallSpecError(
            f"{flag}: git could not run to look up {ref!r} in {clone}: {e}"
        ) from e
    commit = (result.stdout or "").strip()
    if result.returncode != 0 or not _FULL_COMMIT_RE.fullmatch(commit):
        raise InstallSpecError(
            f"{flag}: {clone} has no commit named {ref!r} (is it a clone of {PACKAGE_NAME} "
            "that holds that commit? `git fetch` it first)."
        )
    return commit


def commit_install_spec(commit: str) -> str:
    """The spec of one commit of the default repository."""
    return f"git+{REPO_URL}@{commit}"


def resolve_baseline_ref(ref: str, *, flag: str = "--baseline-ref") -> BaselineSource:
    """The build ``scaffold upgrade --baseline-ref`` names.

    * a full install spec (``git+https://...@<ref>``, ``git+file://...``,
      ``graph-agents-cli==X``, ``graph-agents-cli @ <url>``): used as it is;
    * an existing path (a checkout, an unpacked sdist, a wheel): that build;
    * ``<clone>@<ref>`` where ``<clone>`` is a local git clone: ``ref``'s commit
      in it (looked up now, so a typo fails before anything is built);
    * anything else is a tag, branch or commit of the default repository.

    Raises :class:`InstallSpecError` (exit 3) when ``ref`` cannot name a build.
    """
    value = ref.strip()
    # Plain spaces can be part of a path; every other whitespace or control
    # character is refused, and a spec follows the install-spec rules.
    problem = _invalid_character(value.replace(" ", "_"))
    if not value or problem is not None:
        raise InstallSpecError(
            f"{flag} must name a {PACKAGE_NAME} build"
            + (f"; it contains {problem}." if problem else ".")
        )
    if "://" in value or value.startswith(("git+", PACKAGE_NAME)):
        problem = _invalid_character(value)
        if problem is not None:
            raise InstallSpecError(
                f"{flag} {value!r} is not an install spec: it contains {problem}."
            )
        return BaselineSource(spec=value, label=value)
    path = Path(value).expanduser()
    if path.exists():
        resolved = path.resolve()
        return BaselineSource(spec=str(resolved), label=f"the build at {resolved}", refresh=True)
    if "@" in value:
        clone_text, _, git_ref = value.rpartition("@")
        clone = Path(clone_text).expanduser()
        if clone_text and clone.is_dir():
            _check_git_ref(git_ref, flag)
            commit = _resolve_local_commit(clone.resolve(), git_ref, flag)
            return BaselineSource(
                spec=f"git+{clone.resolve().as_uri()}@{commit}",
                label=f"commit {commit[:12]} of {clone.resolve()}",
                commit=commit,
            )
        if clone_text and ("/" in clone_text or clone_text.startswith((".", "~"))):
            raise InstallSpecError(f"{flag}: {clone_text} is not a directory (a local clone).")
    _check_git_ref(value, flag)
    commit = value if _FULL_COMMIT_RE.fullmatch(value) else None
    return BaselineSource(
        spec=f"git+{REPO_URL}@{value}", label=f"{value} of {REPO_URL}", commit=commit
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
    except InvalidInstallSpecError:
        # A hint must not fail (or print the malformed override): the command that
        # needs the spec reports the error itself.
        spec = _default_install_spec(version)
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
