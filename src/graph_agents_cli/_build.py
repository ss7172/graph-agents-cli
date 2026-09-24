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

"""Which build of graph-agents-cli is running.

A release and the builds made between two releases share one version string,
so the version alone cannot tell them apart. Every wheel therefore records the
source it was built from in ``graph_agents_cli/_build_info.json``, written by
``hatch_build.py`` at the repository root: the git commit, whether the files
that go into the wheel had uncommitted changes, and whether the commit carries
the release tag ``v<version>``. A source checkout (an editable install, ``uv
run`` in the repository) reads the same facts from git when asked.

The build id is the version for a release build, ``<version>+g<commit7>`` for
any other commit and ``<version>+g<commit7>.dirty`` when the tree had
uncommitted changes. A build whose source is unknown (built from a tree without
git and without the file) is identified by its version only and has no commit.

The build hook loads this module from the source tree by path, before the
package's dependencies exist, so it uses the standard library only.
"""

from __future__ import annotations

import functools
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

PACKAGE_NAME = "graph-agents-cli"
BUILD_INFO_FILENAME = "_build_info.json"
BUILD_INFO_FORMAT = 1
# The paths whose content goes into the wheel (relative to the repository root):
# an uncommitted change under one of them makes the build "dirty".
SOURCE_PATHS = ("src", "pyproject.toml", "hatch_build.py")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")  # SHA-1 or SHA-256 object names
_GIT_TIMEOUT_S = 15
# Variables that would point git at another repository than the one asked about.
_GIT_REDIRECTS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY")


@dataclass(frozen=True)
class BuildInfo:
    """The version plus the source facts of one build."""

    version: str
    commit: str | None = None
    dirty: bool = False
    release: bool = False

    @property
    def id(self) -> str:
        """``0.2.0`` (release or unknown source), ``0.2.0+g1a2b3c4`` or ``0.2.0+g1a2b3c4.dirty``."""
        if self.commit is None or (self.release and not self.dirty):
            return self.version
        return f"{self.version}+g{self.commit[:7]}{'.dirty' if self.dirty else ''}"

    @property
    def is_release(self) -> bool:
        """Built from the commit the release tag ``v<version>`` names, with no local change."""
        return self.commit is not None and self.release and not self.dirty

    def describe(self) -> str:
        """One line for ``info``: the id and where it comes from."""
        if self.commit is None:
            return f"{self.id} (source unknown: not built from a git checkout)"
        if self.is_release:
            return f"{self.id} (release, commit {self.commit[:12]})"
        if self.dirty:
            return f"{self.id} (commit {self.commit[:12]} with uncommitted changes)"
        return f"{self.id} (commit {self.commit[:12]}, not a release)"


def _git(root: Path, *args: str) -> str | None:
    """``git -C root <args>`` stdout (stripped), or None when git fails or is missing."""
    env = {k: v for k, v in os.environ.items() if k not in _GIT_REDIRECTS}
    env["GIT_OPTIONAL_LOCKS"] = "0"  # a status must not rewrite the index
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            env=env,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def git_facts(root: str | os.PathLike[str], version: str) -> dict[str, object] | None:
    """The build facts of the git checkout whose top level is ``root``; None when it is not one.

    ``dirty`` is true when a file under :data:`SOURCE_PATHS` differs from the
    commit (untracked files included, ignored ones not); when git cannot say,
    the build counts as dirty. ``release`` is true only for a clean tree whose
    commit carries the tag ``v<version>``.
    """
    root_path = Path(root).resolve()
    top = _git(root_path, "rev-parse", "--show-toplevel")
    if not top or Path(top).resolve() != root_path:
        return None  # not a checkout, or a directory inside another repository
    commit = _git(root_path, "rev-parse", "--verify", "HEAD^{commit}")
    if not commit or _COMMIT_RE.fullmatch(commit) is None:
        return None
    status = _git(root_path, "status", "--porcelain", "--untracked-files=all", "--", *SOURCE_PATHS)
    dirty = status is None or bool(status)
    tags = (_git(root_path, "tag", "--points-at", "HEAD") or "").split()
    return {
        "format": BUILD_INFO_FORMAT,
        "version": version,
        "commit": commit,
        "dirty": dirty,
        "release": not dirty and f"v{version}" in tags,
    }


def _from_facts(version: str, facts: object) -> BuildInfo | None:
    if not isinstance(facts, dict):
        return None
    commit = facts.get("commit")
    if not isinstance(commit, str) or _COMMIT_RE.fullmatch(commit) is None:
        return None
    dirty = facts.get("dirty") is not False  # anything but an explicit false counts as dirty
    return BuildInfo(
        version=version,
        commit=commit,
        dirty=dirty,
        release=facts.get("release") is True and not dirty,
    )


def _installed_version() -> str:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as dist_version

    try:
        return dist_version(PACKAGE_NAME)
    except PackageNotFoundError:
        return "0.0.0"  # the unknown-version sentinel of scaffold.utils.version


def build_of_package(package_dir: Path, version: str) -> BuildInfo:
    """The build of the package at ``package_dir``.

    The recorded ``_build_info.json`` wins (an installed wheel); a package that
    lives at ``<checkout>/src/graph_agents_cli`` reads the checkout's git facts;
    anything else is a build of unknown source.
    """
    info_file = package_dir / BUILD_INFO_FILENAME
    if info_file.is_file():
        try:
            recorded = _from_facts(version, json.loads(info_file.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            recorded = None
        if recorded is not None:
            return recorded
    root = package_dir.parent.parent
    if package_dir.parent.name == "src" and (root / "pyproject.toml").is_file():
        from_git = _from_facts(version, git_facts(root, version))
        if from_git is not None:
            return from_git
    return BuildInfo(version=version)


@functools.cache
def current_build() -> BuildInfo:
    """The running build (computed once per process)."""
    return build_of_package(Path(__file__).resolve().parent, _installed_version())
