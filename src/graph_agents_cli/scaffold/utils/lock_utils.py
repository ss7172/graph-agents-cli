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

"""Bundled uv lock files for the ``langgraph`` template (DECISIONS.md Section 7 item 14).

The template ships one lock per runtime, ``uv-fastapi.lock`` and
``uv-langgraph-server.lock``; ``create`` renames the matching one to
``uv.lock``. ``generate_locks.py`` regenerates them from the template's
``pyproject.toml``.
"""

from __future__ import annotations

import pathlib

from jinja2 import Environment, StrictUndefined

from graph_agents_cli._defaults import RUNTIMES

LANGGRAPH_TEMPLATE_NAME = "langgraph"

# Project name used while resolving; replaced by the cookiecutter placeholder
# in the committed lock, which `create` fills with the real project name.
LOCK_PROJECT_NAME = "locked-template"
LOCK_PROJECT_PLACEHOLDER = "{{cookiecutter.project_name}}"


def _scaffold_root() -> pathlib.Path:
    """Return the root of the scaffold package directory."""
    return pathlib.Path(__file__).resolve().parent.parent


def langgraph_template_dir() -> pathlib.Path:
    """The bundled ``agents/langgraph`` template directory."""
    return _scaffold_root() / "agents" / LANGGRAPH_TEMPLATE_NAME


def lock_filename(runtime: str) -> str:
    """The bundled lock name for ``runtime``: ``uv-<runtime>.lock``."""
    if runtime not in RUNTIMES:
        raise ValueError(f"Unknown runtime '{runtime}'. Expected one of: {', '.join(RUNTIMES)}")
    return f"uv-{runtime}.lock"


LOCK_FILENAMES: dict[str, str] = {runtime: lock_filename(runtime) for runtime in RUNTIMES}


def render_pyproject(
    template_path: pathlib.Path,
    runtime: str,
    *,
    project_name: str = LOCK_PROJECT_NAME,
    **overrides: object,
) -> str:
    """Render the template's ``pyproject.toml`` for ``runtime``.

    Uses the same variables ``create`` passes to cookiecutter (CONTRACTS
    section 3) with placeholder values, so the resolved lock matches a
    rendered project. ``overrides`` replace individual variables.
    """
    from .template import build_cookiecutter_context

    context = build_cookiecutter_context(
        project_name=project_name,
        agent_name=LANGGRAPH_TEMPLATE_NAME,
        runtime=runtime,
        deployment_target="kubernetes",
        template_config={"settings": {"agent_directory": "app", "tags": ["langgraph"]}},
    )
    # cookiecutter wraps list variables as single-element choices; unwrap for jinja.
    for key, value in list(context.items()):
        if isinstance(value, list) and len(value) == 1 and isinstance(value[0], list):
            context[key] = value[0]
    context.update(overrides)

    env = Environment(undefined=StrictUndefined, keep_trailing_newline=True, autoescape=False)
    template = env.from_string(template_path.read_text(encoding="utf-8"))
    return template.render(cookiecutter=context)


def replace_lock_project_name(lock_text: str, project_name: str) -> str:
    """Fill the project-name placeholder of a bundled lock for a rendered project."""
    return lock_text.replace(LOCK_PROJECT_PLACEHOLDER, project_name)
