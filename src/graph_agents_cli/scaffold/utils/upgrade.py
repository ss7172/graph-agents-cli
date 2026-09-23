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

"""3-way file comparison and dependency merging for upgrade and enhance."""

import fnmatch
import hashlib
import logging
import pathlib
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import yaml

MANIFEST_FILENAME = "graph-agents-cli-manifest.yaml"

# Preservation rules: what upgrade/enhance never overwrite. Patterns use the
# {agent_directory} placeholder, replaced at runtime. Everything not listed is
# "scaffolding" and gets the 3-way compare.
FILE_CATEGORIES: dict[str, list[str]] = {
    "agent_code": [  # Never modified
        "{agent_directory}/agent.py",
        "{agent_directory}/tools/**",
        "{agent_directory}/policies/**",
        # Reserved for directories the developer may create.
        "{agent_directory}/prompts/**",
        "{agent_directory}/graph/**",
    ],
    "config_files": [  # Never overwritten
        ".env",
        ".env.*",
        "api-policy.yaml",
        "deployment/helm/*/values-*.yaml",
        "deployment/argocd/**",
        "tests/eval/datasets/**",
        "tests/eval/eval_config.yaml",
    ],
    "dependencies": [  # Semantic merge
        "pyproject.toml",
        MANIFEST_FILENAME,
    ],
}


# Preserve type literals for type-safe reason matching
PreserveType = Literal["gacli_unchanged", "already_current", "unchanged_both", None]


@dataclass
class FileCompareResult:
    """Result of comparing a file across three versions."""

    path: str
    category: str
    action: Literal["auto_update", "preserve", "skip", "conflict", "new", "removed"]
    reason: str
    # For preserve actions, indicates why preserved
    preserve_type: PreserveType = None
    # For conflicts, store the content hashes
    current_hash: str | None = None
    old_template_hash: str | None = None
    new_template_hash: str | None = None


class DependencyReadError(Exception):
    """A dependency manifest could not be read: it's missing or unparseable."""


@dataclass
class DependencyResolution:
    """What the merge decided to do with one dependency.

    Statuses:
      - "added": the template introduced this dependency.
      - "updated": a template-managed dependency whose spec the template changed.
      - "removed": a template-managed dependency the new template dropped.
      - "unchanged": a template-managed dependency the template left as-is.
      - "kept": a dependency *the user* added, preserved untouched.
    """

    name: str
    status: Literal["updated", "added", "removed", "kept", "unchanged"]
    old_version: str | None = None
    new_version: str | None = None

    def full_name(self) -> str:
        """The full requirement to write: ``name[extra]version``."""
        return f"{self.name}{self.new_version or ''}"


def _expand_patterns(patterns: list[str], agent_directory: str) -> list[str]:
    """Expand {agent_directory} placeholder in patterns."""
    return [p.replace("{agent_directory}", agent_directory) for p in patterns]


def _matches_any_pattern(path: str, patterns: list[str]) -> bool:
    """Check if path matches any glob pattern, including ** recursive patterns."""
    path = path.replace("\\", "/")

    for pattern in patterns:
        pattern = pattern.replace("\\", "/")

        if fnmatch.fnmatch(path, pattern):
            return True

        if "**" in pattern:
            regex = re.escape(pattern)
            regex = regex.replace(r"\*\*/", "(?:.*/)?")  # **/ = zero or more dirs
            regex = regex.replace(r"\*\*", ".*")
            regex = regex.replace(r"\*", "[^/]*")
            if re.match(f"^{regex}$", path):
                return True

    return False


def categorize_file(path: str, agent_directory: str = "app") -> str:
    """Return category: agent_code, config_files, dependencies, or scaffolding."""
    for category, patterns in FILE_CATEGORIES.items():
        expanded = _expand_patterns(patterns, agent_directory)
        if _matches_any_pattern(path, expanded):
            return category
    return "scaffolding"


def _file_hash(file_path: pathlib.Path) -> str | None:
    """Calculate SHA256 hash of a file's contents."""
    if not file_path.exists():
        return None
    try:
        content = file_path.read_bytes()
        return hashlib.sha256(content).hexdigest()
    except Exception as e:
        logging.warning(f"Could not hash file {file_path}: {e}")
        return None


def three_way_compare(
    relative_path: str,
    project_dir: pathlib.Path,
    old_template_dir: pathlib.Path,
    new_template_dir: pathlib.Path,
    agent_directory: str = "app",
) -> FileCompareResult:
    """Compare file across current, old template, and new template.

    Returns action based on:
    - current == old -> auto-update (user didn't modify)
    - old == new -> preserve (the CLI didn't change)
    - all differ -> conflict
    """
    category = categorize_file(relative_path, agent_directory)

    current_file = project_dir / relative_path
    old_template_file = old_template_dir / relative_path
    new_template_file = new_template_dir / relative_path

    if category in ("agent_code", "config_files") and not current_file.exists():
        # Never modified / never overwritten: a file the project does not have
        # yet is added only when the old snapshot did not ship it either (e.g.
        # the argocd Applications that `enhance --cd argocd` introduces). When
        # the old template shipped it and the project no longer has it, the
        # developer removed it deliberately: it is never re-added.
        new_hash = _file_hash(new_template_file)
        if new_hash is not None:
            if old_template_file.exists():
                return FileCompareResult(
                    path=relative_path,
                    category=category,
                    action="skip",
                    reason="Removed by you (never re-added)",
                    new_template_hash=new_hash,
                )
            return FileCompareResult(
                path=relative_path,
                category=category,
                action="new",
                reason="New file in the template (added, then never touched again)",
                new_template_hash=new_hash,
            )

    if category == "agent_code":
        return FileCompareResult(
            path=relative_path,
            category=category,
            action="skip",
            reason="Agent code (never modified by upgrade)",
        )

    if category == "config_files":
        return FileCompareResult(
            path=relative_path,
            category=category,
            action="skip",
            reason="Config file (user's environment settings)",
        )

    if category == "dependencies":
        return FileCompareResult(
            path=relative_path,
            category=category,
            action="preserve",
            reason="Dependencies (requires merge handling)",
        )

    current_hash = _file_hash(current_file)
    old_hash = _file_hash(old_template_file)
    new_hash = _file_hash(new_template_file)

    # New file in the template (not in project, regardless of old template)
    if current_hash is None and new_hash is not None:
        return FileCompareResult(
            path=relative_path,
            category=category,
            action="new",
            reason="New file in the template",
            new_template_hash=new_hash,
        )

    # File removed in new template
    if current_hash is not None and old_hash is not None and new_hash is None:
        if current_hash == old_hash:
            return FileCompareResult(
                path=relative_path,
                category=category,
                action="removed",
                reason="File removed from the template (you didn't modify it)",
                current_hash=current_hash,
                old_template_hash=old_hash,
            )
        return FileCompareResult(
            path=relative_path,
            category=category,
            action="conflict",
            reason="File removed from the template but you modified it",
            current_hash=current_hash,
            old_template_hash=old_hash,
        )

    # File only in current project (user-added, not part of the template)
    if current_hash is not None and old_hash is None and new_hash is None:
        return FileCompareResult(
            path=relative_path,
            category=category,
            action="skip",
            reason="User-added file (not part of the template)",
        )

    # File doesn't exist anywhere relevant
    if current_hash is None and new_hash is None:
        return FileCompareResult(
            path=relative_path,
            category=category,
            action="skip",
            reason="File not present",
        )

    # User didn't modify (current == old)
    if current_hash == old_hash and new_hash is not None:
        if old_hash == new_hash:
            return FileCompareResult(
                path=relative_path,
                category=category,
                action="preserve",
                reason="Unchanged in both project and template",
                preserve_type="unchanged_both",
                current_hash=current_hash,
                old_template_hash=old_hash,
                new_template_hash=new_hash,
            )
        return FileCompareResult(
            path=relative_path,
            category=category,
            action="auto_update",
            reason="You didn't modify this file",
            current_hash=current_hash,
            old_template_hash=old_hash,
            new_template_hash=new_hash,
        )

    # The template didn't change (old == new)
    if old_hash == new_hash and current_hash is not None:
        return FileCompareResult(
            path=relative_path,
            category=category,
            action="preserve",
            reason="The template didn't change this file",
            preserve_type="gacli_unchanged",
            current_hash=current_hash,
            old_template_hash=old_hash,
            new_template_hash=new_hash,
        )

    # Already up to date (current == new)
    if current_hash == new_hash:
        return FileCompareResult(
            path=relative_path,
            category=category,
            action="preserve",
            reason="Already up to date",
            preserve_type="already_current",
            current_hash=current_hash,
            old_template_hash=old_hash,
            new_template_hash=new_hash,
        )

    # All three differ -> conflict
    return FileCompareResult(
        path=relative_path,
        category=category,
        action="conflict",
        reason="Both you and the template modified this file",
        current_hash=current_hash,
        old_template_hash=old_hash,
        new_template_hash=new_hash,
    )


def collect_all_files(
    project_dir: pathlib.Path,
    old_template_dir: pathlib.Path,
    new_template_dir: pathlib.Path,
    exclude_patterns: list[str] | None = None,
) -> set[str]:
    """Collect all unique relative file paths from all three directories."""
    if exclude_patterns is None:
        exclude_patterns = [
            ".git/**",
            ".venv/**",
            "venv/**",
            "__pycache__/**",
            "*.pyc",
            ".DS_Store",
            "*.egg-info/**",
            "uv.lock",
            ".uv/**",
            ".graph-agents-cli/**",
        ]

    all_files: set[str] = set()

    for base_dir in [project_dir, old_template_dir, new_template_dir]:
        if not base_dir.exists():
            continue
        for file_path in base_dir.rglob("*"):
            if file_path.is_file():
                relative = str(file_path.relative_to(base_dir))
                if not _matches_any_pattern(relative, exclude_patterns):
                    all_files.add(relative)

    return all_files


def _parse_dependency(dep_str: str) -> tuple[str, str, str]:
    """Parse a dependency string into (base_name, extras, version_spec).

    Examples:
        "langgraph>=1.0" -> ("langgraph", "", ">=1.0")
        "a2a-sdk[http-server]>=1.0" -> ("a2a-sdk", "[http-server]", ">=1.0")
        "pytest" -> ("pytest", "", "")
    """
    match = re.match(r"^([a-zA-Z0-9_-]+)(\[[^\]]+\])?(.*)", dep_str.strip())
    if match:
        base_name = match.group(1).lower()
        extras = match.group(2) or ""
        version = match.group(3).strip()
        return base_name, extras, version
    return dep_str.lower(), "", ""


def _load_dependencies_from_pyproject(pyproject_path: pathlib.Path) -> dict[str, str]:
    """Load dependencies as {base_name: version_spec} dict.

    Raises DependencyReadError if pyproject.toml is missing or unparseable; a
    present file with no dependencies yields {}.
    """
    if not pyproject_path.exists():
        raise DependencyReadError(f"Dependency manifest not found: {pyproject_path}")

    try:
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)

        deps = data.get("project", {}).get("dependencies", [])
        result: dict[str, str] = {}
        for dep in deps:
            name, extras, version = _parse_dependency(dep)
            result[name] = f"{extras}{version}"
        return result
    except Exception as e:
        raise DependencyReadError(f"Could not read dependencies from {pyproject_path}: {e}") from e


def _resolve_dependencies(
    current: dict[str, str],
    old: dict[str, str],
    new: dict[str, str],
) -> list[DependencyResolution]:
    """Merge deps from three sources: new template + user-added (current - old)."""
    resolutions: list[DependencyResolution] = []
    user_added = set(current) - set(old)
    gacli_managed = set(old)

    for name, new_spec in new.items():
        if name in old:
            old_spec = old[name]
            resolutions.append(
                DependencyResolution(
                    name=name,
                    status="updated" if old_spec != new_spec else "unchanged",
                    old_version=old_spec,
                    new_version=new_spec,
                )
            )
        elif name in user_added:
            user_spec = current[name]
            logging.warning(
                f"Dependency '{name}' is now added by the template ({new_spec}) "
                f"but your project already pins it ({user_spec}). Keeping your "
                f"version; if the upgrade misbehaves, reconcile it with the "
                f"template's expected {new_spec}."
            )
        else:
            resolutions.append(
                DependencyResolution(name=name, status="added", new_version=new_spec)
            )

    for name in user_added:
        user_spec = current[name]
        resolutions.append(
            DependencyResolution(
                name=name, status="kept", old_version=user_spec, new_version=user_spec
            )
        )

    for name in gacli_managed:
        if name not in new and name not in user_added:
            resolutions.append(
                DependencyResolution(name=name, status="removed", old_version=old[name])
            )

    return resolutions


def merge_python_dependencies(
    current_project: pathlib.Path,
    old_template_project: pathlib.Path,
    new_template_project: pathlib.Path,
) -> list[DependencyResolution]:
    """Merge Python deps: new_template + user_added, where user_added = current - old."""
    return _resolve_dependencies(
        _load_dependencies_from_pyproject(current_project / "pyproject.toml"),
        _load_dependencies_from_pyproject(old_template_project / "pyproject.toml"),
        _load_dependencies_from_pyproject(new_template_project / "pyproject.toml"),
    )


def write_python_dependencies(
    project_dir: pathlib.Path,
    resolutions: list[DependencyResolution],
) -> bool:
    """Write merged dependencies to project_dir/pyproject.toml using the uv CLI.

    Uses ``uv add --frozen`` and ``uv remove --frozen`` so the lockfile and
    virtualenv are left untouched; only pyproject.toml is modified.
    """
    pyproject_path = project_dir / "pyproject.toml"
    if not pyproject_path.exists():
        return False

    try:
        from graph_agents_cli._runner import run_resolved
        from graph_agents_cli._tools import ToolNotFoundError

        ordered: list[DependencyResolution] = sorted(resolutions, key=lambda r: r.name)
        to_remove = [r.name for r in ordered if r.status == "removed"]
        to_add = [r.full_name() for r in ordered if r.status != "removed"]

        if to_remove:
            result = run_resolved(
                ["uv", "remove", "--frozen", *to_remove],
                cwd=project_dir,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                logging.warning(f"uv remove failed: {result.stderr}")

        if to_add:
            result = run_resolved(
                ["uv", "add", "--frozen", *to_add],
                cwd=project_dir,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                logging.warning(f"uv add failed: {result.stderr}")
                return False

        return True
    except ToolNotFoundError:
        logging.warning("uv not found; cannot write merged dependencies")
        return False
    except Exception as e:
        logging.warning(f"Could not write dependencies to {pyproject_path}: {e}")
        return False


# Per-language dependency handlers (Python only). None = not supported.
MERGE_DEPENDENCY_HANDLERS: dict[str, Callable | None] = {
    "python": merge_python_dependencies,
}

WRITE_DEPENDENCY_HANDLERS: dict[str, Callable | None] = {
    "python": write_python_dependencies,
}


def update_cli_metadata(
    project_dir: pathlib.Path,
    create_params: dict[str, Any],
    *,
    cli_version: str | None = None,
    remove_keys: list[str] | None = None,
    base_template: str | None = None,
) -> None:
    """Update specific keys in the project manifest.

    A manifest that cannot be read or written is logged and skipped: every
    caller writes metadata as a side effect of work that already succeeded.

    Args:
        project_dir: Path to the project directory
        create_params: Dict of keys to update inside create_params
        cli_version: If provided, update cli_version
        remove_keys: List of keys to remove from the create_params section
        base_template: If provided, record it at the manifest top level
    """
    manifest_path = project_dir / MANIFEST_FILENAME
    if not manifest_path.exists():
        logging.warning(f"Manifest not found: {manifest_path}")
        return

    try:
        with open(manifest_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        if cli_version:
            data["cli_version"] = cli_version

        if base_template is not None:
            data["base_template"] = base_template

        if "create_params" not in data or not isinstance(data["create_params"], dict):
            data["create_params"] = {}

        params = data["create_params"]

        for key, val in create_params.items():
            params[key] = val

        if remove_keys:
            for key in remove_keys:
                params.pop(key, None)

        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
    except Exception as e:
        logging.warning(f"Could not update the manifest {manifest_path}: {e}")


def compare_all_files(
    project_dir: pathlib.Path,
    old_template_dir: pathlib.Path,
    new_template_dir: pathlib.Path,
    agent_directory: str = "app",
) -> list[FileCompareResult]:
    """Compare all files using 3-way comparison."""
    all_files = collect_all_files(project_dir, old_template_dir, new_template_dir)

    results = []
    for relative_path in sorted(all_files):
        result = three_way_compare(
            relative_path,
            project_dir,
            old_template_dir,
            new_template_dir,
            agent_directory,
        )
        results.append(result)

    return results


def group_results_by_action(
    results: list[FileCompareResult],
) -> dict[str, list[FileCompareResult]]:
    """Group results by action type."""
    groups: dict[str, list[FileCompareResult]] = {
        "auto_update": [],
        "preserve": [],
        "skip": [],
        "conflict": [],
        "new": [],
        "removed": [],
    }

    for result in results:
        groups[result.action].append(result)

    return groups
