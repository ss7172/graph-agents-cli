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

"""graph-agents-cli extension update command."""

from __future__ import annotations

import logging
from pathlib import Path

import click

from graph_agents_cli.__init__ import __version__
from graph_agents_cli._trust import require_confirmation
from graph_agents_cli.extension._loader import incompatible_spec
from graph_agents_cli.extension._manifest import (
    EXTENSIONS_FILE,
    ExtensionEntry,
    read_extension_entries,
    upsert_extension_entry,
)
from graph_agents_cli.extension._paths import (
    installed_scope_roots,
    vendored_extensions_dir,
)
from graph_agents_cli.extension._refs import RefParseError, anchor_local, parse_ref
from graph_agents_cli.extension._resolver import (
    ResolverError,
    expected_stamp,
    materialize,
    read_stamp,
    resolve_sha,
)
from graph_agents_cli.extension._spec import ExtensionSpecError
from graph_agents_cli.extension._trust import confirm_trust, stdin_is_interactive


def _restore_pin(entry: ExtensionEntry, root: Path) -> None:
    """Put the previously pinned copy back after refusing to advance to a new one."""
    try:
        materialize(
            anchor_local(parse_ref(entry.source, ref_override=entry.sha), root),
            entry.sha,
            vendored_extensions_dir(root),
            name=entry.name,
        )
    except ResolverError as e:
        logging.warning("Could not restore the previous pin for %r: %s", entry.name, e)


@click.command("update")
@click.argument("name", required=False)
@require_confirmation("Update extension(s) to the latest pinned ref?")
def cmd_update(
    name: str | None,
    auto_approve: bool,
) -> None:
    """Advance extension pins (re-resolve the tracked ref).

    Updates every installed extension when NAME is omitted. The tracked ref is
    resolved first: an extension whose code did not change is reported as up
    to date without a trust prompt. New third-party code needs your trust
    (a prompt, or -y); without a terminal to ask on it keeps its pin and the
    command exits 1.
    """
    updated: list[str] = []
    current: list[str] = []
    skipped: list[str] = []
    untrusted: list[str] = []
    for scope, root in installed_scope_roots():
        manifest = root / EXTENSIONS_FILE
        for entry in read_extension_entries(manifest):
            if name is not None and entry.name != name:
                continue
            try:
                ref = anchor_local(
                    parse_ref(
                        entry.source,
                        # "HEAD" means "follow latest"; pass None so resolve_sha re-resolves.
                        ref_override=(None if entry.ref == "HEAD" else entry.ref),
                    ),
                    root,
                )
            except RefParseError as e:
                logging.warning("Skipping extension %r: %s", entry.name, e)
                skipped.append(entry.name)
                continue
            try:
                # Resolving reads the source (the remote's refs, or a local tree's
                # hash); it runs none of the extension's code, so it needs no trust.
                new_sha = resolve_sha(ref)
                extension_dir = vendored_extensions_dir(root) / entry.name
                if (
                    new_sha == entry.sha
                    and extension_dir.exists()
                    and read_stamp(extension_dir) == expected_stamp(ref, new_sha)
                ):
                    # Same commit (or, for a local source, the same tree) as the
                    # copy on disk: nothing to do, nothing to claim, nothing to trust.
                    current.append(entry.name)
                    continue
            except ResolverError as e:
                logging.warning("Could not update %r: %s", entry.name, e)
                skipped.append(entry.name)
                continue
            if not confirm_trust(ref, auto_approve=auto_approve):
                click.secho(
                    f"Skipped {entry.name!r} (not trusted; the installed copy is kept).",
                    fg="yellow",
                )
                skipped.append(entry.name)
                untrusted.append(entry.name)
                continue
            try:
                # name=entry.name pins the vendored dir to the stable id (see
                # materialize) so an update survives an upstream rename.
                materialize(
                    ref,
                    new_sha,
                    vendored_extensions_dir(root),
                    name=entry.name,
                )
            except ResolverError as e:
                # One unreachable source must not abandon the others, or the
                # command reports failure after already advancing a pin.
                logging.warning("Could not update %r: %s", entry.name, e)
                skipped.append(entry.name)
                continue
            # Compat gate: if the new pin declares an `error`-mode range the
            # running CLI is outside, keep the old pin (restore its working
            # copy) instead of advancing to a version that won't run here.
            try:
                spec = incompatible_spec(vendored_extensions_dir(root) / entry.name)
            except ExtensionSpecError as e:
                _restore_pin(entry, root)
                click.secho(
                    f"Skipped {entry.name!r}: the new version's manifest is not "
                    f"valid ({e}); kept {entry.sha[:7]}.",
                    fg="yellow",
                )
                skipped.append(entry.name)
                continue
            if spec is not None:
                if spec.error_on_incompatible:
                    _restore_pin(entry, root)
                    click.secho(
                        f"Skipped {entry.name!r}: new version requires graph-agents-cli "
                        f"{spec.requires_agents_cli}, running {__version__} (kept {entry.sha[:7]}).",
                        fg="yellow",
                    )
                    skipped.append(entry.name)
                    continue
                logging.warning(
                    "Updated %r to a version requiring graph-agents-cli %s (running %s).",
                    entry.name,
                    spec.requires_agents_cli,
                    __version__,
                )
            upsert_extension_entry(
                manifest,
                ExtensionEntry(
                    name=entry.name,
                    source=entry.source,
                    ref=entry.ref,
                    sha=new_sha,
                    scope=scope,
                ),
            )
            updated.append(entry.name)
    if not updated and not skipped and not current:
        raise click.ClickException(
            f"No extension named {name!r} found." if name else "No extensions to update."
        )
    if updated:
        click.secho(f"Updated: {', '.join(updated)}.", fg="green")
    if current:
        click.secho(f"Already up to date: {', '.join(current)}.", dim=True)
    if untrusted and not stdin_is_interactive():
        # Nobody could answer: a script must not take "kept the old pin" for success.
        raise click.ClickException(
            f"Not updated: {', '.join(untrusted)} changed and needs your trust; rerun with -y "
            "to trust the new code."
        )
