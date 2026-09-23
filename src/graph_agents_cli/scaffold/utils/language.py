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

"""Language configuration for CLI commands: Python only.

The single-entry table keeps the ``dispatch_language`` shape the command
modules were written against, so adding a language later is additive.
"""

import logging
import pathlib
import tomllib
from collections.abc import Callable, Mapping
from typing import Any

import click

SUPPORTED_LANGUAGES: tuple[str, ...] = ("python",)


def _read_python_version(root: pathlib.Path) -> str:
    """Return the ``[project].version`` from ``pyproject.toml`` or raise."""
    pyproject_path = root / "pyproject.toml"
    if not pyproject_path.exists():
        raise FileNotFoundError(f"pyproject.toml not found in {root}")
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)  # PEP 621 [project] section
    version = data.get("project", {}).get("version")
    if version and isinstance(version, str):
        return version
    raise KeyError(f"no [project].version in {pyproject_path}")


LANGUAGE_CONFIGS: dict[str, dict[str, Any]] = {
    "python": {
        "lock_file": "uv.lock",
        "lock_command": ["uv", "lock"],
        "lock_command_name": "uv lock",
        "display_name": "Python",
        "agent_file": "agent.py",
        # `app/agent.py` exports the compiled graph.
        "agent_variable": "graph",
        "agent_in_subdirectory": False,
        "version_reader": _read_python_version,
        "api_base_path": "",
        "a2a_base_path_factory": lambda app_name: f"/a2a/{app_name}",
    },
}


def get_language_config(language: str) -> dict[str, Any]:
    """Get the configuration dict for a language (Python config as fallback)."""
    return LANGUAGE_CONFIGS.get(language, LANGUAGE_CONFIGS["python"])


class UnsupportedLanguageError(click.ClickException):
    """Raised when a command has no handler for the project's language."""


def dispatch_language(
    command_name: str,
    handlers: Mapping[str, Callable | None],
    language: str,
) -> Callable:
    """Return the handler for ``language``, or raise a clear error."""
    handler = handlers.get(language)
    if handler is None:
        supported = ", ".join(sorted(k for k, v in handlers.items() if v is not None))
        raise UnsupportedLanguageError(
            f"`graph-agents-cli {command_name}` isn't supported for '{language}' "
            f"projects.\n  Supported languages: {supported}."
        )
    return handler


def find_agent_file(
    project_dir: pathlib.Path,
    agent_directory: str,
    language: str = "python",
) -> pathlib.Path | None:
    """Return ``{agent_directory}/agent.py`` when it exists, else None."""
    agent_file = project_dir / agent_directory / get_language_config(language)["agent_file"]
    return agent_file if agent_file.is_file() else None


def validate_agent_file(
    agent_file: pathlib.Path,
    language: str = "python",
) -> tuple[bool, str | None]:
    """Check that the agent file defines the exported variable (``graph``)."""
    required_var = get_language_config(language)["agent_variable"]
    try:
        content = agent_file.read_text(encoding="utf-8")
    except Exception as e:
        return False, f"Could not read {agent_file.name}: {e}"
    if required_var in content:
        return True, None
    return False, f"Missing '{required_var}' variable in {agent_file.name}"


def get_agent_file_hint(dir_path: pathlib.Path, language: str = "python") -> str:
    """Hint string for directory selection, e.g. ``' (has agent.py)'``."""
    if not dir_path.is_dir():
        return ""
    agent_file = get_language_config(language)["agent_file"]
    if (dir_path / agent_file).exists():
        return f" (has {agent_file})"
    return ""


def get_project_version(
    project_dir: str | pathlib.Path,
    default_version: str = "0.0.0",
) -> str:
    """Extract the project version from ``pyproject.toml``, falling back to ``default_version``."""
    root = pathlib.Path(project_dir)
    try:
        return _read_python_version(root)
    except Exception as e:
        logging.warning(
            "Could not read the project version (%s). Falling back to %s; set "
            "[project].version in pyproject.toml.",
            e,
            default_version,
        )
    return default_version
