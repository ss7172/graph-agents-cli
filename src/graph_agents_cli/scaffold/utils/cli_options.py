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

"""Shared Click options for ``create`` and ``scaffold enhance`` (DECISIONS.md section 6)."""

from collections.abc import Callable

import click

from graph_agents_cli._defaults import (
    AUTH_POLICIES,
    CD_MODES,
    CHECKPOINTERS,
    DEFAULT_AGENT_GUIDANCE_FILENAME,
    DEPLOYMENT_TARGETS,
    MODEL_PROVIDERS,
    RUNTIMES,
)


def shared_template_options(f: Callable) -> Callable:
    """Decorator adding the options shared by template-based commands."""
    # Apply options in reverse order since decorators are applied bottom-up
    f = click.option("--debug", is_flag=True, help="Enable debug logging")(f)
    f = click.option(
        "--skip-deps",
        is_flag=True,
        help="Skip base template dependency installation (used when reusing saved config)",
        default=False,
        hidden=True,
    )(f)
    f = click.option(
        "-s",
        "--skip-checks",
        is_flag=True,
        help="Skip preflight checks (uv on PATH)",
        default=False,
    )(f)
    f = click.option(
        "--auto-approve",
        "--yes",
        "-y",
        is_flag=True,
        default=False,
        help="Non-interactive: skip prompts and use defaults",
    )(f)
    f = click.option(
        "--interactive",
        "-i",
        is_flag=True,
        default=False,
        help="Enable interactive prompts for human use",
    )(f)
    f = click.option(
        "--base-template",
        "-bt",
        help="Base template to use (overrides template default, only for remote templates)",
    )(f)
    f = click.option(
        "--agent-guidance-filename",
        default=DEFAULT_AGENT_GUIDANCE_FILENAME,
        show_default=True,
        help="Filename for agent guidance (e.g. GEMINI.md, CLAUDE.md, AGENTS.md)",
    )(f)
    f = click.option(
        "--agent-directory",
        "-dir",
        help="Name of the agent directory (overrides template default)",
    )(f)
    f = click.option(
        "--prototype",
        "-p",
        is_flag=True,
        help=(
            "Minimal project: deployment target defaults to 'none' unless given, "
            "CD is forced to 'skip'"
        ),
        default=False,
    )(f)
    f = click.option(
        "--process",
        "process",
        help=(
            "Governing process document (path or string) recorded as process: in the "
            "manifest and rendered into the guidance file"
        ),
    )(f)
    f = click.option(
        "--product-policy",
        "product_policy",
        type=click.Path(exists=True, dir_okay=False, resolve_path=True),
        help="Seed product-policy.yaml from this file (DECISIONS.md D28)",
    )(f)
    f = click.option(
        "--auth-policy",
        type=click.Choice(list(AUTH_POLICIES)),
        help="Authentication policy (default: shared-bearer)",
    )(f)
    f = click.option(
        "--cd",
        "cd",
        type=click.Choice(list(CD_MODES)),
        help="Continuous delivery mode (default: skip; requires --deployment-target kubernetes)",
    )(f)
    f = click.option(
        "--registry",
        help="Container registry <url/org> (default: ghcr.io/<git origin owner>)",
    )(f)
    f = click.option(
        "--deployment-target",
        "-d",
        type=click.Choice(list(DEPLOYMENT_TARGETS)),
        help="Deployment target (default: kubernetes)",
    )(f)
    f = click.option(
        "--checkpointer",
        type=click.Choice(list(CHECKPOINTERS)),
        help="Deployed checkpointer (default: postgres for kubernetes, memory for none)",
    )(f)
    f = click.option(
        "--model",
        help="Model name (default: the provider's default model)",
    )(f)
    f = click.option(
        "--model-provider",
        type=click.Choice(list(MODEL_PROVIDERS)),
        help="Model provider (default: openai; prompted in interactive mode)",
    )(f)
    f = click.option(
        "--runtime",
        type=click.Choice(list(RUNTIMES)),
        help="Application runtime (default: fastapi)",
    )(f)
    return f
