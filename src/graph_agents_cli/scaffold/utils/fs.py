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

"""Shared filesystem helpers for copying project directories."""

import fnmatch

# Directories/files to exclude when copying a project tree (backups, local
# template copies, etc.).
STANDARD_IGNORE_PATTERNS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        "node_modules",
        ".next",
        "dist",
        "build",
        ".DS_Store",
        ".vscode",
        ".idea",
        "*.egg-info",
        ".mypy_cache",
        ".ty",
        ".coverage",
        "htmlcov",
        ".tox",
        ".cache",
        # The CLI's run state in a project (pid file, artifacts): rebuilt on demand.
        ".graph-agents-cli",
        # `langgraph dev` local state.
        ".langgraph_api",
    }
)


def standard_ignore_patterns(dir: str, files: list[str]) -> list[str]:
    """Return the entries in ``files`` to skip when copying a project tree.

    Matches the signature expected by ``shutil.copytree(ignore=...)``.
    """
    return _matching(files, STANDARD_IGNORE_PATTERNS)


# Credentials, dropped on top of the standard patterns when a tree is copied
# *into another project* rather than kept for its owner. Deliberately not part
# of the set above: `create_project_backup` is the undo for `enhance` and
# `upgrade`, both of which merge `.env` from the template, and a generated
# project gitignores it, so the backup holds the only copy the user has.
SECRET_IGNORE_PATTERNS = frozenset({".env", ".env.*"})

# `.env.*` would otherwise take the documentation along with the secret. These
# hold no values, and a source that ships one means it to be read.
ENV_TEMPLATE_NAMES = frozenset({".env.example", ".env.sample", ".env.template"})


def source_ignore_patterns(dir: str, files: list[str]) -> set[str]:
    """Return the entries in ``files`` to skip when copying a source tree.

    A source is published into somebody else's project (an extension being
    vendored, say), so it leaves behind the standard junk and its credentials.
    """
    patterns = STANDARD_IGNORE_PATTERNS | SECRET_IGNORE_PATTERNS
    return set(_matching(files, patterns)) - ENV_TEMPLATE_NAMES


def _matching(files: list[str], patterns: frozenset[str]) -> list[str]:
    return [f for f in files if any(fnmatch.fnmatch(f, p) for p in patterns)]
