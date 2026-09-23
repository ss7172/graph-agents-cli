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

"""graph-agents-cli extension remove command."""

from __future__ import annotations

import shutil

import click

from graph_agents_cli._trust import require_confirmation
from graph_agents_cli.extension._manifest import (
    EXTENSIONS_FILE,
    read_extension_entries,
    remove_extension_entry,
)
from graph_agents_cli.extension._paths import (
    installed_scope_roots,
    vendored_extensions_dir,
)
from graph_agents_cli.extension._resolver import (
    ResolverError,
    validate_extension_name,
)


@click.command("remove")
@click.argument("name")
@require_confirmation("Remove this extension (deletes its vendored copy)?")
def cmd_remove(name: str, auto_approve: bool) -> None:
    """Remove an installed extension (checks project then user scope)."""
    for scope, root in installed_scope_roots():
        manifest = root / EXTENSIONS_FILE
        entries = {e.name for e in read_extension_entries(manifest)}
        if name not in entries:
            continue
        # The name comes from a committed manifest and decides what gets
        # rmtree'd, so check it before touching anything.
        try:
            safe_name = validate_extension_name(name)
        except ResolverError as e:
            raise click.ClickException(f"Refusing to remove extension {name!r}: {e}") from e
        remove_extension_entry(manifest, name)
        extension_dir = vendored_extensions_dir(root) / safe_name
        if extension_dir.exists():
            shutil.rmtree(extension_dir)
        click.secho(f"Removed extension {name!r} ({scope} scope).", fg="green")
        return
    raise click.ClickException(f"Extension {name!r} is not installed.")
