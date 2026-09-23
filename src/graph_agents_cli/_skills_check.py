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

"""Skills version drift detection for graph-agents-cli.

Owns the ``graph-agents-cli-`` skills prefix filter that ``info`` relies on.
The check is opt-out for disconnected installs: it is
skipped when ``GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1`` or a CI marker is set, and
it degrades silently (debug log only) when ``npx`` or the network is absent.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import click
import yaml

# Pin the skills npm package to avoid executing an unverified version.
SKILLS_NPX_PACKAGE = "skills@1.5.9"

SKILL_PREFIX = "graph-agents-cli-"

# Opt-out for the update and skills-version checks (disconnected installs). `main.py` reads the
# same variable before importing this module; the check re-reads it so that a
# direct call honours the opt-out too.
NO_UPDATE_CHECK_ENV = "GRAPH_AGENTS_CLI_NO_UPDATE_CHECK"

_SKILLS_CHECK_INTERVAL = 12 * 60 * 60  # 12 hours in seconds
_SKILLS_CHECK_STAMP = Path.home() / ".agents" / ".graph_agents_cli_skills_check"


def _parse_skill_version(skill_md: Path) -> str | None:
    """Extract ``metadata.version`` from a SKILL.md YAML frontmatter.

    Returns the version string, or ``None`` on any failure.
    Logs at debug level when the file exists but cannot be parsed.
    """
    try:
        content = skill_md.read_text(encoding="utf-8")
    except OSError:
        return None

    if not content.startswith("---"):
        logging.debug("Malformed skill file (no frontmatter): %s", skill_md)
        return None
    parts = content.split("---", 2)
    if len(parts) < 3:
        logging.debug("Malformed skill file (incomplete frontmatter): %s", skill_md)
        return None
    try:
        frontmatter = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        logging.debug("Malformed skill file (invalid YAML): %s", skill_md)
        return None

    metadata = (frontmatter or {}).get("metadata") or {}
    version = metadata.get("version") if isinstance(metadata, dict) else None
    if not version:
        logging.debug("Malformed skill file (missing metadata.version): %s", skill_md)
    return str(version) if version else None


def _find_installed_skills() -> dict[str, str]:
    """Return ``{skill_name: version}`` for every installed graph-agents-cli skill.

    Fast path (~8 ms): scan ``~/.agents/skills/graph-agents-cli-*/SKILL.md``
    directly.  Falls back to ``npx skills list --json`` (~400 ms+) if
    the well-known directory is empty or missing, which is more robust
    when skills are installed to a non-default location.
    """
    import json

    result: dict[str, str] = {}

    # Fast path: well-known global install location
    skills_dir = Path.home() / ".agents" / "skills"
    if skills_dir.is_dir():
        for skill_dir in sorted(skills_dir.iterdir()):
            if not skill_dir.name.startswith(SKILL_PREFIX):
                continue
            version = _parse_skill_version(skill_dir / "SKILL.md")
            if version:
                result[skill_dir.name] = version

    if result:
        return result

    # Slow path: ask npx skills for actual install locations
    try:
        from graph_agents_cli._runner import run_resolved

        proc = run_resolved(
            ["npx", "-y", SKILLS_NPX_PACKAGE, "list", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        if proc.returncode != 0:
            return {}
        entries = json.loads(proc.stdout)
    except Exception as e:
        # Broad by design: this freshness check runs on every CLI invocation and
        # must never abort a user's command, so any failure (no npx, no network)
        # degrades silently to "no skills found" rather than propagating.
        logging.debug("Could not query installed skills via npx: %s", e)
        return {}

    if not isinstance(entries, list):
        return {}

    for entry in entries:
        if not isinstance(entry, dict):
            logging.debug("Skipping malformed skills entry (expected an object): %r", entry)
            continue
        name = entry.get("name", "")
        if not name.startswith(SKILL_PREFIX):
            continue
        path = entry.get("path")
        if not path:
            continue
        version = _parse_skill_version(Path(path) / "SKILL.md")
        if version:
            result[name] = version

    return result


def get_installed_skills() -> list[dict] | None:
    """Return installed graph-agents-cli skills as a list of dicts.

    Returns None if the query fails (no ``npx``, no network, malformed output).
    """
    import json

    try:
        from graph_agents_cli._runner import run_resolved

        result = run_resolved(
            ["npx", "-y", SKILLS_NPX_PACKAGE, "list", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        if result.returncode != 0:
            logging.debug(
                "npx skills list failed (exit %d): %s",
                result.returncode,
                (result.stderr or "").strip(),
            )
            return None
        skills = json.loads(result.stdout)
        return [
            s
            for s in skills
            if isinstance(s, dict) and str(s.get("name", "")).startswith(SKILL_PREFIX)
        ]
    except Exception as e:
        logging.debug("Could not query installed skills: %s", e)
        return None


def _is_ci() -> bool:
    """Return True when running in a CI/automation environment.

    The skills version-drift warning is a local developer-experience nicety: in
    CI there is no human to act on it and skills are not installed, so the check
    only adds noise (and may spawn a doomed ``npx`` subprocess). We skip it when
    a well-known CI marker is present.
    """
    return any(os.environ.get(var) for var in ("CI", "BUILD_ID", "GITHUB_ACTIONS", "GITLAB_CI"))


def _is_opted_out() -> bool:
    """Return True when the user disabled the check."""
    return os.environ.get(NO_UPDATE_CHECK_ENV) == "1"


def _skills_check_is_due() -> bool:
    """Return True if enough time has elapsed since the last check."""
    try:
        last = float(_SKILLS_CHECK_STAMP.read_text().strip())
        return (time.time() - last) > _SKILLS_CHECK_INTERVAL
    except (OSError, ValueError):
        return True


def _record_skills_check() -> None:
    """Write the current timestamp to the stamp file."""
    try:
        _SKILLS_CHECK_STAMP.parent.mkdir(parents=True, exist_ok=True)
        _SKILLS_CHECK_STAMP.write_text(str(time.time()))
    except OSError:
        pass


def check_skills_version() -> None:
    """Warn if any installed skill version doesn't match the running CLI version.

    Rate-limited to once per 12 hours via a timestamp file so it can
    run globally on every command without adding latency.

    Scans all installed ``graph-agents-cli-*`` skills, compares each
    ``metadata.version`` with the running ``__version__``, and lists
    the mismatched ones.  Never blocks execution. Skipped entirely in CI
    and when ``GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1``. Silent offline.
    """
    if _is_opted_out() or _is_ci() or not _skills_check_is_due():
        return

    try:
        installed = _find_installed_skills()
    except Exception as e:  # never abort the user's command
        logging.debug("Skills check skipped: %s", e)
        return
    if not installed:
        return

    from graph_agents_cli import __version__

    _record_skills_check()

    mismatched = {name: ver for name, ver in installed.items() if ver != __version__}
    if not mismatched:
        return

    lines = [f"  - {name} (v{ver})" for name, ver in mismatched.items()]
    click.echo(
        f"\n⚠️  Skills version mismatch — CLI is v{__version__}, "
        f"but {len(mismatched)} skill(s) differ:\n"
        + "\n".join(lines)
        + "\n   Run 'graph-agents-cli update' to sync.\n",
        err=True,
    )
