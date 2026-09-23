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

"""Re-materialize vendored extension working copies from pinned SHAs."""

from __future__ import annotations

import logging
from pathlib import Path

import click

from graph_agents_cli.__init__ import __version__
from graph_agents_cli.extension._loader import incompatible_spec
from graph_agents_cli.extension._manifest import (
    EXTENSIONS_FILE,
    read_extension_entries,
)
from graph_agents_cli.extension._paths import user_config_root, vendored_extensions_dir
from graph_agents_cli.extension._refs import RefParseError, parse_ref
from graph_agents_cli.extension._resolver import (
    ResolverError,
    expected_stamp,
    materialize,
    read_stamp,
)
from graph_agents_cli.extension._spec import ExtensionSpecError


def _scope_roots(project_root: Path | None) -> list[Path]:
    """Roots to sync, project first. The scope label is not needed: both scopes
    store the list under the same filename."""
    roots = [] if project_root is None else [project_root]
    roots.append(user_config_root())
    return roots


def sync_extensions(project_root: Path | None) -> list[str]:
    """Ensure every recorded extension is vendored on disk.

    Reports what it restored and returns those names.
    """
    restored: list[str] = []
    for scope_root in _scope_roots(project_root):
        manifest = scope_root / EXTENSIONS_FILE
        for entry in read_extension_entries(manifest):
            extension_dir = vendored_extensions_dir(scope_root) / entry.name
            try:
                ref = parse_ref(entry.source, ref_override=entry.sha)
                # Re-materialize when the copy is missing OR no longer matches
                # its source: a bumped pin, a partial copy, or — for a local
                # path, whose source is a working tree rather than a commit —
                # any edit made to it since the copy was taken.
                if extension_dir.exists() and read_stamp(extension_dir) == expected_stamp(
                    ref, entry.sha
                ):
                    continue
                # name=entry.name pins the vendored dir to the stable id (see
                # materialize) so a restore survives an upstream rename.
                materialize(
                    ref,
                    entry.sha,
                    vendored_extensions_dir(scope_root),
                    name=entry.name,
                )
                restored.append(entry.name)
                # Never block a restore (that would brick `install`); just warn
                # if the restored pin is out of range for the running CLI.
                try:
                    stale = incompatible_spec(extension_dir)
                except ExtensionSpecError as e:
                    # Restore must never fail: it is how a bad pin gets fixed.
                    logging.warning("%s: %s.", extension_dir, e)
                    stale = None
                if stale is not None:
                    logging.warning(
                        "Restored extension %r requires graph-agents-cli %s but running %s.",
                        entry.name,
                        stale.requires_agents_cli,
                        __version__,
                    )
            except (ResolverError, RefParseError) as e:
                logging.warning(
                    "Could not restore extension %r from %s: %s",
                    entry.name,
                    entry.source,
                    e,
                )
    if restored:
        click.echo(f"Restored extensions: {', '.join(restored)}")
    return restored
