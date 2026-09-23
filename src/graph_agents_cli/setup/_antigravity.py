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

"""Mirror globally-installed skills into Antigravity's skill directories.

Temporary workaround until `npx skills` fully supports Antigravity's IDE / CLI /
2.0 skill paths. Today `npx skills ... -g` installs global skills into the
canonical ``~/.agents/skills`` store, but Antigravity does not read from there:
the IDE and Antigravity 2.0 read ``~/.gemini/config/skills`` and the Antigravity
CLI reads ``~/.gemini/antigravity-cli/skills``. This module bridges that gap by
linking each canonical skill into both locations so a global ``graph-agents-cli setup``
makes the skills visible to Antigravity. Once upstream installs to the correct
locations directly, this module can be removed.

Both locations are mirrored ourselves rather than relying on ``npx skills``,
whose global install is currently unreliable (vercel-labs/skills#1362).

Links are directory symlinks, falling back to directory junctions on Windows
(which work without elevation). Links auto-track the canonical store, so updates
to a skill are seen by Antigravity with no re-link needed.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

# Canonical store that ``npx skills ... -g`` writes to.
_CANONICAL_SUBPATH = (".agents", "skills")

# Where Antigravity looks for global skills: IDE / 2.0 first, then the CLI.
_ANTIGRAVITY_SUBPATHS = (
    (".gemini", "config", "skills"),
    (".gemini", "antigravity-cli", "skills"),
)


def _display(path: Path, home: Path) -> str:
    """Render ``path`` with the home directory collapsed to ``~`` for output."""
    try:
        return f"~/{path.relative_to(home)}"
    except ValueError:
        return str(path)


def _is_link(path: Path) -> bool:
    """Return True if ``path`` is a symlink or a Windows directory junction.

    ``Path.is_symlink()`` does not report junctions, so fall back to
    ``os.readlink``, which succeeds for both and raises for real directories.
    """
    if path.is_symlink():
        return True
    try:
        os.readlink(path)
        return True
    except OSError:
        return False


def _link_target_into(entry: Path, source_root: Path) -> bool:
    """Return True if the link at ``entry`` points somewhere under ``source_root``."""
    try:
        target_str = os.readlink(entry)
        # Windows-specific path prefix handling:
        # - "\??\" is the NT Object Manager namespace prefix, often returned by
        #   os.readlink for Windows junctions.
        # - "\\?\" is the Win32 device namespace prefix, used for long paths.
        # Python's pathlib.Path does not recognize these prefixes as absolute roots
        # (e.g., Path("\??\C:\foo").is_absolute() may return False or behave incorrectly),
        # so we strip the 4-character prefix to get a standard absolute path.
        if target_str.startswith("\\??\\"):
            target_str = target_str[4:]
        elif target_str.startswith("\\\\?\\"):
            target_str = target_str[4:]
        target = Path(target_str)
    except OSError:
        return False
    if not target.is_absolute():
        target = entry.parent / target
    try:
        resolved_parent = target.parent.resolve()
        resolved_source = source_root.resolve()
        return resolved_source == resolved_parent or resolved_source in resolved_parent.parents
    except OSError:
        return False


def _link_dir(source: Path, link: Path) -> bool:
    """Point ``link`` at ``source`` with a directory link (junction on Windows).

    Idempotent: a link already resolving to ``source`` is left as-is. A stale or
    mis-pointing link is replaced, but a real (non-link) file or directory is left
    untouched and reported as a failure so user data is never clobbered.
    """
    try:
        if link.exists() and os.path.realpath(link) == os.path.realpath(source):
            return True
    except OSError:
        pass

    if _is_link(link):
        try:
            link.unlink()
        except OSError:
            pass
    elif link.exists():
        return False

    link.parent.mkdir(parents=True, exist_ok=True)

    try:
        os.symlink(source, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass

    # Windows without symlink privilege: a junction works without elevation.
    if os.name == "nt":
        import subprocess

        from graph_agents_cli._runner import run_resolved

        try:
            run_resolved(
                ["cmd", "/c", "mklink", "/J", str(link), str(source)],
                resolve_executable=False,
                check=True,
                capture_output=True,
                text=True,
            )
            return True
        except (OSError, subprocess.CalledProcessError):
            pass

    logging.warning(
        "Could not link skill '%s' into %s "
        "(on Windows, enable Developer Mode or run as administrator).",
        source.name,
        link.parent,
    )
    return False


def _prune_dead_links(target_root: Path, source_root: Path) -> None:
    """Remove broken links under ``target_root`` that point into ``source_root``.

    Keeps the mirror in sync when a skill is removed from the canonical store.
    Only ever removes links (symlinks/junctions), never real files or directories.
    """
    if not target_root.is_dir():
        return
    for entry in target_root.iterdir():
        if entry.exists() or not _link_target_into(entry, source_root):
            continue
        try:
            entry.unlink()
        except OSError as e:
            logging.warning("Could not remove stale skill link %s: %s", entry, e)


def link_skills_for_antigravity(
    *, home: Path | None = None, skills_dir: Path | None = None
) -> list[str]:
    """Link each canonical global skill into Antigravity's skill directories.

    No-op (returns an empty list) when Antigravity/Gemini is not present
    (no ``~/.gemini``) or when the canonical store is missing. Best-effort:
    individual link failures are logged, never raised.

    Returns a list of human-readable summary lines describing what was linked.
    """
    home = home or Path.home()

    if not (home / ".gemini").is_dir():
        return []

    source_root = skills_dir or home.joinpath(*_CANONICAL_SUBPATH)
    if not source_root.is_dir():
        # This runs right after npx installs skills, so a missing canonical store
        # means npx no longer writes there — surface it instead of silently
        # no-op'ing (see module docstring).
        logging.warning(
            "Skills store %s not found; skills were not mirrored to Antigravity.",
            source_root,
        )
        return []

    skill_dirs = [
        d for d in sorted(source_root.iterdir()) if d.is_dir() and (d / "SKILL.md").is_file()
    ]
    if not skill_dirs:
        # Same expectation as above: npx just ran, so an empty store is surprising.
        # Fall through so dead links from previously-linked skills still get pruned.
        logging.warning("Skills store %s has no skills to mirror to Antigravity.", source_root)

    summary: list[str] = []
    for subpath in _ANTIGRAVITY_SUBPATHS:
        target_root = home.joinpath(*subpath)
        # Link each skill individually rather than pointing the whole target dir
        # at the canonical store: ~/.gemini/.../skills is owned by Antigravity and
        # may already hold skills from other sources. A top-level symlink would
        # fail (dir exists) or shadow those; per-skill links coexist with them.
        linked = sum(_link_dir(skill_dir, target_root / skill_dir.name) for skill_dir in skill_dirs)
        _prune_dead_links(target_root, source_root)
        if linked:
            summary.append(f"Linked {linked} skill(s) into {_display(target_root, home)}")
    return summary
