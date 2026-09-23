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

"""graph-agents-cli scaffold command group."""

import click

from graph_agents_cli._click import LazyGroup


@click.group("scaffold", cls=LazyGroup)
def scaffold_group():
    """Scaffold, enhance, and upgrade agent projects.

    \b
    Subcommands:
      create   Create a new agent project
      enhance  Add or change the deployment target, CD mode, or runtime of a project
      upgrade  Upgrade project to a newer graph-agents-cli version
    """


scaffold_group.add_lazy_command(
    "create",
    "graph_agents_cli.scaffold.commands.create:create",
    "Create a LangGraph agent project from a template.",
)
scaffold_group.add_lazy_command(
    "enhance",
    "graph_agents_cli.scaffold.commands.enhance:enhance",
    "Add or change the deployment target, CD mode, or runtime of an existing project.",
)
scaffold_group.add_lazy_command(
    "upgrade",
    "graph_agents_cli.scaffold.commands.upgrade:upgrade",
    "Upgrade project to a newer graph-agents-cli version.",
)
