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

"""graph-agents-cli extension list command."""

from __future__ import annotations

from pathlib import Path

import click

from graph_agents_cli._project import find_project_root
from graph_agents_cli.extension._loader import load_extension_set
from graph_agents_cli.extension._paths import user_config_root


@click.command("list")
def cmd_list() -> None:
    """List active extensions and the commands they contribute."""
    project_root = find_project_root(Path.cwd())
    ps = load_extension_set(project_root, user_config_root())
    if not ps.extensions:
        click.echo("No extensions installed.")
        return
    click.echo("Extensions:")
    # A spec can appear more than once in ps.extensions (e.g. inline manifest plus a
    # vendored copy from the same root); deduplicate for display.
    seen: set[tuple[str, str]] = set()
    for spec in ps.extensions:
        if (spec.name, spec.scope) in seen:
            continue
        seen.add((spec.name, spec.scope))
        cmds = ", ".join(sorted(c.name for c in spec.commands)) or "(none)"
        line = f"  {spec.name}  {spec.scope}  (commands: {cmds})"
        if spec.requires_agents_cli:
            line += f"  requires: {spec.requires_agents_cli}"
        click.echo(line)
    for c in ps.conflict_rows():
        click.echo(f"  ! conflict: {c}")
    for inc in ps.incompatible_rows():
        effect = "its commands will not run" if inc["mode"] == "error" else "applied anyway"
        click.echo(
            f"  ! incompatible: {inc['extension']} requires graph-agents-cli "
            f"{inc['requires']} (running {inc['running']}) — {effect}"
        )
