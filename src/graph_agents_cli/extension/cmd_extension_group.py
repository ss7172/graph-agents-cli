# Copyright 2026 Google LLC
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

"""graph-agents-cli extension command group."""

import click

from graph_agents_cli._click import LazyGroup


@click.group("extension", cls=LazyGroup)
def extension_group():
    """Manage graph-agents-cli extensions (experimental).

    Experimental: the manifest schema and the command surface may still change
    in a breaking way. Pin the CLI version if you depend on either.

    \b
    Subcommands:
      add     Add an extension from a git reference or local path
      list    List active extensions and the commands they contribute
      update  Advance extension pins to the latest tracked ref
      remove  Remove an installed extension
    """


extension_group.add_lazy_command(
    "add",
    "graph_agents_cli.extension.cmd_extension_add:cmd_add",
    "Add an extension from a git reference or local path.",
)
extension_group.add_lazy_command(
    "list",
    "graph_agents_cli.extension.cmd_extension_list:cmd_list",
    "List active extensions and the commands they contribute.",
)
extension_group.add_lazy_command(
    "update",
    "graph_agents_cli.extension.cmd_extension_update:cmd_update",
    "Advance extension pins (re-resolve the tracked ref).",
)
extension_group.add_lazy_command(
    "remove",
    "graph_agents_cli.extension.cmd_extension_remove:cmd_remove",
    "Remove an installed extension (checks project then user scope).",
)
