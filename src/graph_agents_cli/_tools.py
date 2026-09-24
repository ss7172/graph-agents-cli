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

"""Tool resolution utilities.

Every external program the CLI shells out to (helm, kubectl, docker, gh, uv,
npx, ...) is resolved through :func:`require_tool`, which caches the resolved
path and, on Windows, retries with a cleaned PATH. Nothing here imports a
model SDK, LangChain, LangGraph, or a Kubernetes/Docker client.
"""

import os
import re
import shlex
import shutil
from functools import cache

import click

_tool_paths: dict[str, str] = {}

# Matches ANSI escape sequences (CSI/SGR), used to scrub any residual color
# codes from captured subprocess output. We disable color at the source via
# NO_COLOR/FORCE_COLOR, but strip defensively in case the tool emits them anyway
# (e.g. on Windows PowerShell, where embedded escapes have caused error lines
# to render as the previous line's color).
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

# Where `graph-agents-cli setup` fetches the skills from by default (pinned to the
# running release's tag, see `default_skills_source`).
# Keep in sync with extension._refs.FIRST_PARTY_REPO.
DEFAULT_SKILLS_SOURCE = "https://github.com/ss7172/graph-agents-cli"


def default_skills_source(version: str | None) -> str:
    """The first-party skills matching CLI ``version``: ``<repo>#v<version>``.

    A release installs the skills of its own tag, so they never drift from the
    CLI when the default branch moves on. A development build (a source
    checkout, ``0.0.0-dev``, an unknown ``0.0.0``, a ``.devN`` or ``+local``
    version) has no matching tag and uses the default branch.
    """
    from packaging.version import InvalidVersion, Version

    try:
        parsed = Version(version or "")
    except InvalidVersion:
        return DEFAULT_SKILLS_SOURCE
    if parsed.is_devrelease or parsed.local or parsed == Version("0.0.0"):
        return DEFAULT_SKILLS_SOURCE
    return f"{DEFAULT_SKILLS_SOURCE}#v{version}"


_UV_HINT = "Install uv (https://docs.astral.sh/uv/getting-started/installation/) and ensure it is in your PATH."
_NODE_HINT = "Install Node.js (https://nodejs.org/en/download) and ensure it is in your PATH."

# Default installation hints for the tools the CLI shells out to.
# These are used as fallbacks in require_tool when no specific hint is provided,
# ensuring helpful error messages even when tools are resolved implicitly (e.g. via run_resolved).
DEFAULT_INSTALL_HINTS = {
    "helm": "Install Helm (https://helm.sh/docs/intro/install/) and ensure it is in your PATH.",
    "kubectl": "Install kubectl (https://kubernetes.io/docs/tasks/tools/) and ensure it is in your PATH.",
    "docker": "Install Docker (https://docs.docker.com/get-docker/) or a Docker-compatible CLI named 'docker' and ensure it is in your PATH.",
    "argocd": "Install the Argo CD CLI (https://argo-cd.readthedocs.io/en/stable/cli_installation/) and ensure it is in your PATH.",
    "gh": "Install the GitHub CLI (https://cli.github.com/) and ensure it is in your PATH.",
    "uv": _UV_HINT,
    # uvx is shipped by uv, so the install instructions are the same.
    "uvx": f"uvx is part of uv. {_UV_HINT}",
    "npx": f"npx is part of Node.js. {_NODE_HINT}",
    "npm": f"npm is part of Node.js. {_NODE_HINT}",
    "node": _NODE_HINT,
    "git": "Install Git (https://git-scm.com/downloads) and ensure it is in your PATH.",
    "langgraph": (
        "langgraph is a project dependency (langgraph-cli), not a global tool: run "
        "'graph-agents-cli install' inside the project, then invoke it as 'uv run langgraph'."
    ),
}


class ToolNotFoundError(click.ClickException):
    """Raised when a required external tool is not found on PATH."""

    pass


@cache
def _get_cleaned_path() -> str:
    """Returns a cleaned and expanded version of the PATH environment variable.

    This function performs the following steps:
    1. Splits the PATH environment variable using the OS-specific path separator.
    2. Strips quotes from each path segment.
    3. Filters out empty path segments.
    4. Expands environment variables (like $HOME or %USERPROFILE%) within each segment.
    5. Reconstructs the PATH string with the cleaned segments.
    """
    raw_path = os.environ.get("PATH", "")
    parts = raw_path.split(os.pathsep)
    cleaned_parts = []

    for part in parts:
        # Strip quotes from each path segment.
        part = part.strip('"').strip("'")
        if not part:
            continue
        cleaned_parts.append(os.path.expandvars(part))
    return os.pathsep.join(cleaned_parts)


def is_windows() -> bool:
    """Returns True if the current operating system is Windows."""
    return os.name == "nt"


def install_hint(name: str) -> str:
    """Return the default installation hint for ``name`` (empty when unknown)."""
    return DEFAULT_INSTALL_HINTS.get(name, "")


def require_tool(name: str, install_hint: str = "") -> str:
    """Finds a required external tool on the system PATH and returns its path.

    This function performs the following steps:
    1. Checks if the tool's path is already cached in `_tool_paths`.
    2. If not cached, searches for the tool using `shutil.which` with the default PATH.
    3. If not found and running on Windows, searches again using `shutil.which` with a cleaned PATH.
    4. If still not found, raises a `ToolNotFoundError` with an install hint
       (the explicit ``install_hint`` or the default for that tool).
    5. If found, caches the path and returns it.
    """
    if name in _tool_paths:
        return _tool_paths[name]

    path = shutil.which(name)

    if path is None and is_windows():
        path = shutil.which(name, path=_get_cleaned_path())

    if path is None:
        msg = f"'{name}' is not installed or not on PATH."
        hint = install_hint or DEFAULT_INSTALL_HINTS.get(name, "")
        if hint:
            msg += f"\n  {hint}"
        raise ToolNotFoundError(msg)

    _tool_paths[name] = path
    return path


def tool_available(name: str) -> bool:
    """Return True when ``name`` resolves on PATH, without raising."""
    try:
        require_tool(name)
    except ToolNotFoundError:
        return False
    return True


def run_npx_skills(args: list[str], spinner_msg: str):
    """Run an npx skills command, streaming output in real-time.

    Always starts with ``["npx", "-y", SKILLS_NPX_PACKAGE]`` and appends
    the additional ``args`` provided.

    Raises:
        click.ClickException: If the npx process exits non-zero.
    """
    from graph_agents_cli._runner import run_resolved
    from graph_agents_cli._skills_check import SKILLS_NPX_PACKAGE

    full_args = ["npx", "-y", SKILLS_NPX_PACKAGE, *args]
    click.secho(f"  ▸ {shlex.join(full_args)}", fg="cyan", dim=True)

    try:
        run_resolved(full_args, check=True, encoding="utf-8", errors="replace")
    except Exception as e:
        raise click.ClickException("Error running npx skills") from e
