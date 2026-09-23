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

"""Locate the bundled graph-agents-cli skills.

The canonical skills live in ``data/`` next to this file
(``src/graph_agents_cli/skills/data/``). Because that directory is inside the
package, it's automatically bundled in the wheel - so ``graph-agents-cli setup``
can install skills with no ``git`` and no network (disconnected installs).

The repository keeps a byte-identical copy under ``skills/`` at the repo root
for ``npx skills add <repo>`` and the plugin manifests; CONTRIBUTING.md
describes how the two are kept in sync.

This module is just a small helper to locate the skills dir based on a relative
path from this module.
"""

from __future__ import annotations

from pathlib import Path

SKILL_PREFIX = "graph-agents-cli-"

# Directory bundled in the wheel that holds one ``graph-agents-cli-*`` skill per
# subdirectory.
SKILL_BUNDLE_DIR = Path(__file__).resolve().parent / "data"


def is_skill_dir(path: Path) -> bool:
    """Return True if ``path`` is a bundled skill directory.

    A skill is a ``graph-agents-cli-*`` directory containing a ``SKILL.md``
    spec.
    """
    return path.is_dir() and path.name.startswith(SKILL_PREFIX) and (path / "SKILL.md").is_file()


def list_bundled_skills(bundle_dir: Path | None = None) -> list[Path]:
    """Return the bundled skill directories, sorted by name."""
    root = bundle_dir or SKILL_BUNDLE_DIR
    if not root.is_dir():
        return []
    return sorted(d for d in root.iterdir() if is_skill_dir(d))


def get_bundled_skills_dir() -> Path | None:
    """Return the bundle directory when it exists and contains at least one skill.

    Returns None otherwise, so callers can fall through to the next install
    strategy instead of running ``npx skills add`` against an empty directory.
    """
    return SKILL_BUNDLE_DIR if list_bundled_skills() else None
