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

"""The build that rendered a project, recorded in its manifest as ``cli_build``.

``cli_version`` cannot tell apart the builds made between two releases, so
``create`` also records::

    cli_build:
      id: 0.2.0+g1a2b3c4          # the build id (``graph-agents-cli --version``)
      commit: 1a2b3c4...          # its full commit; null when not built from git
      template_digest: sha256:... # what it renders for the recorded settings; or null

``template_digest`` is a digest of the snapshot ``scaffold upgrade`` renders
from the manifest's settings (every file except the manifest itself). Two
builds with the same digest render the same project, so an upgrade between
them has nothing to do; a different digest means the templates changed and the
upgrade needs the recorded build as its baseline. It is null when ``create``
rendered more than that snapshot (a seed ``--api-policy``, a local or remote
template); the upgrade then relies on the build id alone.

``scaffold upgrade`` and ``scaffold enhance`` rewrite the block after a merge;
an in-folder re-render keeps it (it does not update the existing files).
"""

from __future__ import annotations

import hashlib
import logging
import os
import pathlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import yaml

from graph_agents_cli import _build

from .keyedit import EditError, YamlText

MANIFEST_FILENAME = "graph-agents-cli-manifest.yaml"
MANIFEST_KEY = "cli_build"
DIGEST_PREFIX = "sha256:"
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")
# Never part of a render; skipped so a stray cache cannot change a digest.
_SKIPPED_DIRS = frozenset({".git", "__pycache__", ".venv", ".ruff_cache", ".pytest_cache"})
RECORD_COMMENT = (
    "The build that rendered this project and a digest of it; `scaffold upgrade` reads both"
)


@dataclass(frozen=True)
class BuildRecord:
    """The ``cli_build`` block of a manifest."""

    id: str
    commit: str | None = None
    template_digest: str | None = None

    @classmethod
    def of(cls, build: _build.BuildInfo, template_digest: str | None) -> BuildRecord:
        return cls(id=build.id, commit=build.commit, template_digest=template_digest)

    @property
    def dirty(self) -> bool:
        """Built from a tree with uncommitted changes: no commit reproduces it."""
        return self.id.endswith(".dirty")

    @property
    def is_release(self) -> bool:
        """A release build: its id is the bare version and its commit is known."""
        return self.commit is not None and "+" not in self.id

    def same_build_as(self, build: _build.BuildInfo) -> bool:
        """True when this is exactly ``build`` (a clean build with the same id)."""
        return (
            self.id == build.id and self.commit is not None and not self.dirty and not build.dirty
        )

    def as_manifest(self) -> dict[str, Any]:
        return {"id": self.id, "commit": self.commit, "template_digest": self.template_digest}


def running_build() -> _build.BuildInfo:
    """The running build, with the version the scaffold engine records.

    ``cli_version`` comes from ``version.get_current_version`` (tests change
    it); the commit facts from :func:`graph_agents_cli._build.current_build`.
    """
    from . import version as version_module

    facts = _build.current_build()
    return _build.BuildInfo(
        version=version_module.get_current_version(),
        commit=facts.commit,
        dirty=facts.dirty,
        release=facts.release,
    )


class MalformedRecordError(ValueError):
    """``cli_build`` is present but not a valid record."""


def parse_build_record(raw: Any) -> BuildRecord | None:
    """The record in a manifest's ``cli_build`` value; None when absent.

    Raises :class:`MalformedRecordError` for a value that is not a valid record,
    so a hand-edited block is reported instead of being trusted.
    """
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise MalformedRecordError(f"{MANIFEST_KEY} is not a mapping")
    build_id = raw.get("id")
    if not isinstance(build_id, str) or not build_id.strip() or any(c.isspace() for c in build_id):
        raise MalformedRecordError(f"{MANIFEST_KEY}.id is not a build id")
    commit = raw.get("commit")
    if commit is not None and (not isinstance(commit, str) or not _COMMIT_RE.fullmatch(commit)):
        raise MalformedRecordError(f"{MANIFEST_KEY}.commit is not a full git commit")
    digest = raw.get("template_digest")
    if digest is not None and (not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest)):
        raise MalformedRecordError(f"{MANIFEST_KEY}.template_digest is not a sha256 digest")
    return BuildRecord(id=build_id, commit=commit, template_digest=digest)


def recorded_build_for(config: Any) -> BuildRecord | None:
    """The build a project's manifest records, when it belongs to its ``cli_version``.

    ``config`` is a ``ProjectConfig``. A record of another version than
    ``cli_version`` (the version was edited by hand, or the block copied from
    another project) cannot describe the project: :class:`MalformedRecordError`
    says so, and the caller falls back to the version.
    """
    record = parse_build_record(config.cli_build)
    if record is not None and record.id.split("+", 1)[0] != config.cli_version:
        raise MalformedRecordError(
            f"{MANIFEST_KEY}.id {record.id!r} is not a build of cli_version {config.cli_version!r}"
        )
    return record


def read_manifest_data(project_dir: pathlib.Path) -> dict[str, Any]:
    """The project's manifest as a mapping ({} when missing or unreadable)."""
    try:
        with open(project_dir / MANIFEST_FILENAME, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def read_build_record(project_dir: pathlib.Path) -> BuildRecord | None:
    """The project's recorded build (None when absent; raises when malformed)."""
    return parse_build_record(read_manifest_data(project_dir).get(MANIFEST_KEY))


def template_digest(tree: pathlib.Path) -> str:
    """A digest of every file of a rendered tree except its manifest.

    Paths and contents both count (a renamed file changes it); caches and
    ``.git`` are skipped. Symbolic links count by their target.
    """
    entries: list[tuple[str, bytes]] = []
    for dirpath, dirnames, filenames in os.walk(tree):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIPPED_DIRS)
        for name in filenames:
            path = pathlib.Path(dirpath) / name
            relative = path.relative_to(tree).as_posix()
            if relative == MANIFEST_FILENAME:
                continue
            if path.is_symlink():
                content = b"link:" + os.readlink(path).encode("utf-8", "surrogateescape")
            elif path.is_file():
                content = hashlib.sha256(path.read_bytes()).digest()
            else:
                continue
            entries.append((relative, content))
    digest = hashlib.sha256()
    for relative, content in sorted(entries):
        digest.update(relative.encode("utf-8", "surrogateescape") + b"\0")
        digest.update(content + b"\0")
    return DIGEST_PREFIX + digest.hexdigest()


def write_build_record(project_dir: pathlib.Path, record: BuildRecord | None) -> None:
    """Set (or, with None, remove) ``cli_build`` in the project's manifest.

    The text is edited in place, keeping comments and key order; a manifest the
    edit cannot handle safely is rewritten through the YAML library instead. A
    manifest that cannot be read or written is logged and skipped (callers
    record the build as a side effect of work that already succeeded).
    """
    path = project_dir / MANIFEST_FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        logging.warning("Could not read %s to record the CLI build: %s", path, e)
        return
    try:
        editor = YamlText(text)
        if record is None:
            if MANIFEST_KEY not in editor.data:
                return
            editor.delete((MANIFEST_KEY,))
            # The comment this module wrote above the block goes with it.
            editor.text = editor.text.replace(f"# {RECORD_COMMENT}\n", "", 1)
        else:
            editor.set(
                (MANIFEST_KEY,),
                record.as_manifest(),
                after="cli_version",
                comment=RECORD_COMMENT,
                block=True,
            )
        new_text = editor.text
    except EditError as e:
        logging.debug("Rewriting %s without its comments: %s", path, e)
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError as load_error:
            logging.warning(
                "%s is not valid YAML; the CLI build was not recorded: %s", path, load_error
            )
            return
        if not isinstance(data, dict):
            logging.warning("%s is not a mapping; the CLI build was not recorded", path)
            return
        if record is None:
            data.pop(MANIFEST_KEY, None)
        else:
            data[MANIFEST_KEY] = record.as_manifest()
        new_text = yaml.safe_dump(data, default_flow_style=False, sort_keys=False)
    if new_text == text:
        return
    try:
        path.write_text(new_text, encoding="utf-8")
    except OSError as e:
        logging.warning("Could not record the CLI build in %s: %s", path, e)
