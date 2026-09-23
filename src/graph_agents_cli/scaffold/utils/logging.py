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

"""Welcome banner for the scaffold commands."""

import random

from graph_agents_cli._output import Console

console = Console()

MOTTOS = [
    "Your agents are cleared for takeoff.",
    "Launching agents into production, one deploy at a time.",
    "Houston, we have an agent.",
    "To production... and beyond!",
    "One small step for code, one giant leap for agents.",
    "Graphs in, agents out.",
    "3... 2... 1... Agent deployed!",
    "The sky is not the limit when you have agents.",
]


def _get_version() -> str:
    """Get the package version, with fallback to 'dev'."""
    from graph_agents_cli.scaffold.utils.version import UNKNOWN_VERSION, get_current_version

    version = get_current_version()
    return "dev" if version == UNKNOWN_VERSION else version


def display_welcome_banner(
    *,
    agent: str | None = None,
    enhance_mode: bool = False,
    upgrade_mode: bool = False,
    quiet: bool = False,
) -> None:
    """Display the graph-agents-cli welcome banner.

    Args:
        agent: Optional agent specification (unused in the text, kept for callers).
        enhance_mode: Whether this is for enhancement mode.
        upgrade_mode: Whether this is for an upgrade.
        quiet: If True, print only the version line (auto-approve/programmatic mode).
    """
    version = _get_version()
    motto = random.choice(MOTTOS)

    if quiet:
        console.print(f"[bold blue]graph-agents-cli[/] [dim]v{version}[/]")
        return

    if enhance_mode:
        line1 = "Enhancing your project with deployment and delivery scaffolding."
    elif upgrade_mode:
        line1 = "Upgrading your project to the current templates."
    else:
        line1 = "Create LangGraph agents that run on your Kubernetes cluster."

    console.print()
    console.print(f"[bold blue]graph-agents-cli[/] [dim]v{version}[/]")
    console.print(f'[italic dim]"{motto}"[/]')
    console.print(f"[dim]{line1}[/]")
    console.print()
