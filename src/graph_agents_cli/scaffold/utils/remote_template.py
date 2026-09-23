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

"""Remote template specs (``org/repo/path@ref``, browser URLs) and fetching."""

import logging
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from typing import Any

import click

DEFAULT_BASE_TEMPLATE = "langgraph"


@dataclass
class RemoteTemplateSpec:
    """Parsed remote template specification."""

    repo_url: str
    template_path: str
    git_ref: str


def is_template_spec(value: str) -> bool:
    """True for a value naming a template to fetch rather than one this CLI ships."""
    return value.startswith("local@") or parse_agent_spec(value) is not None


def parse_agent_spec(agent_spec: str) -> RemoteTemplateSpec | None:
    """Parse an agent specification; None when it names a bundled or local template."""
    if agent_spec.startswith("local@"):
        return None

    # GitHub /tree/ URL pattern
    tree_pattern = r"^(https?://[^/]+/[^/]+/[^/]+)/tree/([^/]+)/(.*)$"
    match = re.match(tree_pattern, agent_spec)
    if match:
        return RemoteTemplateSpec(
            repo_url=match.group(1),
            template_path=match.group(3).strip("/"),
            git_ref=match.group(2),
        )

    # General remote pattern: <repo_url>[/<path>][@<ref>]
    remote_pattern = r"^(https?://[^/]+/[^/]+/[^/@]+)(?:/(.*?))?(?:@([^/]+))?/?$"
    match = re.match(remote_pattern, agent_spec)
    if match:
        repo_url = match.group(1)
        template_path = match.group(2) or ""
        git_ref = match.group(3) or "main"

        if "@" in template_path:
            path_parts = template_path.split("@")
            template_path = path_parts[0]
            git_ref = path_parts[1]

        return RemoteTemplateSpec(
            repo_url=repo_url,
            template_path=template_path.strip("/"),
            git_ref=git_ref,
        )

    # GitHub shorthand: <org>/<repo>[/<path>][@<ref>]
    github_shorthand_pattern = r"^([^/]+)/([^/@]+)(?:/(.*?))?(?:@([^/]+))?/?$"
    match = re.match(github_shorthand_pattern, agent_spec)
    if match and "/" in agent_spec:
        org = match.group(1)
        repo = match.group(2)
        return RemoteTemplateSpec(
            repo_url=f"https://github.com/{org}/{repo}",
            template_path=match.group(3) or "",
            git_ref=match.group(4) or "main",
        )

    return None


def check_and_execute_with_version_lock(
    template_dir: pathlib.Path,
    original_agent_spec: str | None = None,
    locked: bool = False,
    project_name: str | None = None,
) -> bool:
    """Log a template's pinned graph-agents-cli version; always proceed with the running code.

    Returns:
        Always False (proceed with the vendored code).
    """
    if locked or os.environ.get("GRAPH_AGENTS_CLI_SKIP_VERSION_LOCK") == "1":
        return False

    version = parse_cli_version_from_lock(template_dir / "uv.lock")
    if version:
        logging.debug(
            "Remote template specifies graph-agents-cli version %s; using the running code instead.",
            version,
        )
    return False


def fetch_remote_template(
    spec: RemoteTemplateSpec,
    original_agent_spec: str | None = None,
    locked: bool = False,
    project_name: str | None = None,
) -> tuple[pathlib.Path, pathlib.Path]:
    """Clone a remote template and return (template directory, temp directory to clean up)."""
    temp_dir = tempfile.mkdtemp(prefix="gacli_remote_template_")
    temp_path = pathlib.Path(temp_dir)
    repo_path = temp_path / "repo"

    try:
        clone_url = spec.repo_url

        clone_cmd = [
            "git",
            "clone",
            "--depth",
            "1",
            "--single-branch",
            "--branch",
            spec.git_ref,
            clone_url,
            str(repo_path),
        ]

        from graph_agents_cli._runner import run_resolved

        logging.debug("Attempting to clone remote template with Git: %s", shlex.join(clone_cmd))
        # GIT_TERMINAL_PROMPT=0 prevents git from prompting for credentials
        result = run_resolved(
            clone_cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )

        # If clone with --single-branch fails, retry without it (for tags)
        if result.returncode != 0:
            if "Remote branch" in result.stderr or "not found" in result.stderr:
                logging.debug(
                    "Clone with --single-branch failed, retrying without it (git_ref '%s' is likely a tag)",
                    spec.git_ref,
                )
                clone_cmd_without_single_branch = [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "--branch",
                    spec.git_ref,
                    clone_url,
                    str(repo_path),
                ]
                run_resolved(
                    clone_cmd_without_single_branch,
                    capture_output=True,
                    text=True,
                    check=True,
                    encoding="utf-8",
                    env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                )
                logging.debug("Git clone successful (without --single-branch).")
            else:
                raise subprocess.CalledProcessError(
                    result.returncode, clone_cmd, result.stdout, result.stderr
                )
        else:
            logging.debug("Git clone successful.")
    except subprocess.CalledProcessError as e:
        shutil.rmtree(temp_path, ignore_errors=True)
        raise RuntimeError(f"Git clone failed: {e.stderr.strip()}") from e

    try:
        template_dir = repo_path / spec.template_path if spec.template_path else repo_path

        if not template_dir.exists():
            raise FileNotFoundError(
                f"Template path not found in the repository: {spec.template_path}"
            )

        if check_and_execute_with_version_lock(
            template_dir, original_agent_spec, locked, project_name
        ):
            shutil.rmtree(temp_path, ignore_errors=True)
            sys.exit(0)

        return template_dir, temp_path
    except Exception as e:
        shutil.rmtree(temp_path, ignore_errors=True)
        raise RuntimeError(
            f"An unexpected error occurred after fetching remote template: {e}"
        ) from e


def _detect_flat_structure(template_dir: pathlib.Path) -> bool:
    """True when ``agent.py`` sits in the template root with no agent subdirectory."""
    if not (template_dir / "agent.py").exists():
        return False

    folder_name = template_dir.name.replace("-", "_")
    for subdir in ("app", folder_name):
        subdir_path = template_dir / subdir
        if subdir_path.is_dir() and (subdir_path / "agent.py").exists():
            return False

    logging.debug(
        "Detected flat structure in '%s': agent.py in root, no agent subdirectory",
        template_dir,
    )
    return True


def load_remote_template_config(
    template_dir: pathlib.Path,
    cli_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Load a fetched template's configuration with CLI overrides.

    Sources, first found wins: ``.template/templateconfig.yaml``, the template's
    ``graph-agents-cli-manifest.yaml``, or a ``[tool.graph-agents-cli]`` section in
    its ``pyproject.toml``. CLI overrides take precedence over all of them.
    """
    config: dict[str, Any] = {
        "base_template": DEFAULT_BASE_TEMPLATE,
        "name": template_dir.name,
        "description": "",
        "agent_directory": "app",
    }
    has_explicit_config = False

    template_config_path = template_dir / ".template" / "templateconfig.yaml"
    manifest_path = template_dir / "graph-agents-cli-manifest.yaml"
    pyproject_path = template_dir / "pyproject.toml"

    if template_config_path.exists():
        try:
            import yaml

            with open(template_config_path, encoding="utf-8") as f:
                template_config = yaml.safe_load(f) or {}
            has_explicit_config = bool(template_config)
            if template_config:
                config.update(template_config)
                logging.debug("Found explicit .template/templateconfig.yaml configuration")
        except Exception as e:
            # ClickException so the commands that surface it print the template
            # author's typo as an error rather than a traceback.
            raise click.ClickException(f"{template_config_path.name} is not valid YAML: {e}") from e
    elif manifest_path.exists():
        try:
            import yaml

            with open(manifest_path, encoding="utf-8") as f:
                manifest_config = yaml.safe_load(f) or {}
            has_explicit_config = bool(manifest_config)
            if manifest_config:
                config.update(manifest_config)
                logging.debug("Found explicit graph-agents-cli-manifest.yaml configuration")
        except Exception as e:
            logging.error(f"Error loading graph-agents-cli-manifest.yaml config: {e}")
    elif pyproject_path.exists():
        try:
            with open(pyproject_path, "rb") as f:
                pyproject_data = tomllib.load(f)

            toml_config = pyproject_data.get("tool", {}).get("graph-agents-cli", {})
            project_info = pyproject_data.get("project", {})
            has_explicit_config = bool(toml_config)

            if toml_config:
                config.update(toml_config)
                logging.debug("Found explicit [tool.graph-agents-cli] configuration")

            if "name" not in toml_config and "name" in project_info:
                config["name"] = project_info["name"]

            if "description" not in toml_config and "description" in project_info:
                config["description"] = project_info["description"]

            logging.debug("Loaded template config from %s", pyproject_path)
        except Exception as e:
            logging.error(f"Error loading pyproject.toml config: {e}")
    else:
        logging.warning(
            "%s declares no graph-agents-cli configuration, so it is treated as a "
            "%s template. Add .template/templateconfig.yaml to say otherwise.",
            template_dir.name,
            DEFAULT_BASE_TEMPLATE,
        )

    # Flat structure: agent.py in the root with no subdirectory
    if not has_explicit_config and _detect_flat_structure(template_dir):
        folder_name = template_dir.name.replace("-", "_")
        config.setdefault("settings", {})
        config["settings"]["agent_directory"] = folder_name
        config["settings"]["source_agent_directory"] = "."
        config["is_flat_structure"] = True
        logging.debug(
            "Detected flat structure: source='.', target='%s'",
            folder_name,
        )

    config["has_explicit_config"] = bool(has_explicit_config)

    if cli_overrides:
        config = merge_template_configs(config, cli_overrides)
        logging.debug("Applied CLI overrides: %s", cli_overrides)

    return config


def get_base_template_name(config: dict[str, Any]) -> str:
    """Base template name from a remote template config (default ``langgraph``)."""
    return str(config.get("base_template") or DEFAULT_BASE_TEMPLATE)


def merge_template_configs(
    base_config: dict[str, Any], remote_config: dict[str, Any]
) -> dict[str, Any]:
    """Deep-merge ``remote_config`` over a copy of ``base_config``."""
    import copy

    def deep_merge(d1: dict[str, Any], d2: dict[str, Any]) -> dict[str, Any]:
        for k, v in d2.items():
            if k in d1 and isinstance(d1[k], dict) and isinstance(v, dict):
                d1[k] = deep_merge(d1[k], v)
            else:
                d1[k] = v
        return d1

    merged_config = copy.deepcopy(base_config)
    return deep_merge(merged_config, remote_config)


def parse_cli_version_from_lock(uv_lock_path: pathlib.Path) -> str | None:
    """Parse the graph-agents-cli version pinned in a ``uv.lock``, if any."""
    if not uv_lock_path.exists():
        return None

    try:
        with open(uv_lock_path, "rb") as f:
            lock_data = tomllib.load(f)

        for package in lock_data.get("package", []):
            if package.get("name") == "graph-agents-cli":
                version = package.get("version")
                if version:
                    logging.debug("Found graph-agents-cli version %s in uv.lock", version)
                    return version
    except Exception as e:
        logging.warning(f"Error parsing uv.lock file {uv_lock_path}: {e}")

    return None
