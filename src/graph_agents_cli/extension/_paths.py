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

"""Filesystem locations for extension scopes and caches."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator
from pathlib import Path

from graph_agents_cli._project import find_project_root


def user_config_root() -> Path:
    # Roaming (APPDATA): config that should follow the user between machines.
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "graph-agents-cli"
        return Path.home() / "AppData" / "Roaming" / "graph-agents-cli"
    return Path.home() / ".config" / "graph-agents-cli"


def vendored_extensions_dir(scope_root: Path) -> Path:
    return scope_root / "extensions"


def scope_root(scope: str) -> Path | None:
    """Root for an install scope, or None for project scope outside a project.

    Returning None rather than raising leaves the wording of "you need a project"
    to the caller, since only the install commands can say what to do about it.
    """
    if scope == "user":
        return user_config_root()
    return find_project_root(Path.cwd())


def installed_scope_roots() -> Iterator[tuple[str, Path]]:
    """Yield (scope, root) for each resolvable scope, project before user.

    Skips project scope when not inside a project, so callers can iterate both
    scopes without repeating the "resolve root, skip if no project" boilerplate.
    """
    for scope in ("project", "user"):
        root = scope_root(scope)
        if root is not None:
            yield scope, root


def git_cache_root() -> Path:
    """Return the shared git cache root directory."""
    # Local (LOCALAPPDATA), not Roaming: re-clonable, and not worth syncing.
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "graph-agents-cli" / "git"
    return Path.home() / ".cache" / "graph-agents-cli" / "git"


def git_cache_dir_name(identity: str, url: str | None) -> str:
    """Return the cache directory name for a repo, as a single path component.

    ``identity`` is the readable ``org/repo``; ``url`` is the clone URL when the
    reference was given as one, and None for the github.com shorthand.

    A shorthand flattens to ``org__repo`` as it always has — the host is implied,
    so the name is unambiguous. A URL cannot do that: two hosts can serve the
    same ``org/repo``, and handing back one host's clone for the other's
    extension would run the wrong code. So a URL's name carries a digest of the
    URL, and the readable part is reduced to characters every filesystem accepts
    (a URL holds ``:`` and ``/``, and Windows rejects both).
    """
    if url is None:
        return identity.replace("/", "__")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", identity).strip("_") or "extension"
    return f"{slug}__{hashlib.sha256(url.encode('utf-8')).hexdigest()[:12]}"
