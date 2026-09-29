# Copyright 2026 graph-agents-cli contributors
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

"""Print the graph-agents-cli command tree as JSON (run with the CLI build's own Python).

    <cli python> clitree.py  ->  {"commands": {"eval run": {"options": [...], "hidden": [...],
                                  "group": false}, ...}, "env_vars": [...]}

Standalone on purpose: the fact-check runs in SkillOpt's environment, which does not have the
CLI installed, so it asks the CLI build's interpreter for the tree instead of importing it.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def main() -> int:
    import click

    import graph_agents_cli
    from graph_agents_cli.main import main as root

    commands: dict[str, dict] = {}

    def walk(cmd: click.Command, path: list[str], ctx: click.Context) -> None:
        options, hidden = [], []
        for param in cmd.params:
            if isinstance(param, click.Option):
                names = [*param.opts, *param.secondary_opts]
                (hidden if param.hidden else options).extend(names)
        options += ["--help", "-h"]
        is_group = isinstance(cmd, click.Group)
        commands[" ".join(path)] = {
            "options": sorted(set(options)),
            "hidden": sorted(set(hidden)),
            "group": is_group,
        }
        if is_group:
            for name in cmd.list_commands(ctx):
                sub = cmd.get_command(ctx, name)
                if sub is not None and not getattr(sub, "hidden", False):
                    walk(sub, [*path, name], click.Context(sub, parent=ctx, info_name=name))

    walk(root, [], click.Context(root, info_name="graph-agents-cli"))
    package = Path(graph_agents_cli.__file__).parent
    env_vars: set[str] = set()
    for source in package.rglob("*"):
        if not source.is_file() or source.suffix in (".pyc", ".lock", ".tgz"):
            continue
        env_vars.update(
            re.findall(r"GRAPH_AGENTS_CLI_[A-Z0-9_]+", source.read_text(errors="replace"))
        )
    json.dump({"commands": commands, "env_vars": sorted(env_vars)}, sys.stdout, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
