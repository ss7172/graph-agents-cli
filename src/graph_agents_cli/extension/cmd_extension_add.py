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

"""graph-agents-cli extension add command."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import click

from graph_agents_cli.__init__ import __version__
from graph_agents_cli._project import find_project_root
from graph_agents_cli._trust import require_confirmation
from graph_agents_cli.extension._compat import is_compatible
from graph_agents_cli.extension._loader import (
    RECOVERY_COMMANDS,
    load_extension_set,
    load_extension_spec,
)
from graph_agents_cli.extension._manifest import (
    EXTENSIONS_FILE,
    ExtensionEntry,
    read_extension_entries,
    upsert_extension_entry,
)
from graph_agents_cli.extension._paths import (
    scope_root,
    user_config_root,
    vendored_extensions_dir,
)
from graph_agents_cli.extension._refs import RefParseError, parse_ref
from graph_agents_cli.extension._resolver import ResolverError, materialize, resolve_sha
from graph_agents_cli.extension._spec import (
    EXTENSION_FILE,
    ExtensionSpec,
    ExtensionSpecError,
)
from graph_agents_cli.extension._trust import confirm_trust


def _parse_or_fail(extension_dir: Path) -> ExtensionSpec | None:
    """Parse the vendored manifest, turning a bad one into a failed install.

    Parsed once here and handed to both gates, so a manifest is not read twice
    and the two cannot disagree about what it says.
    """
    try:
        return load_extension_spec(extension_dir)
    except ExtensionSpecError as e:
        raise click.ClickException(
            f"{EXTENSION_FILE} is not valid: {e}\nNothing was installed."
        ) from e


def _check_compat(spec: ExtensionSpec | None) -> None:
    """Gate a freshly vendored extension against the running CLI version.

    `error` mode raises so the install fails; `warn` mode logs and proceeds.
    This is the deliberate install gate, the only place an incompatibility
    hard-fails; at runtime it only ever warns.
    """
    if (
        spec is None
        or not spec.requires_agents_cli
        or is_compatible(__version__, spec.requires_agents_cli)
    ):
        return
    msg = (
        f"Extension requires graph-agents-cli {spec.requires_agents_cli} but you're "
        f"running {__version__}."
    )
    if spec.error_on_incompatible:
        raise click.ClickException(
            f"{msg} Not installing (on_incompatible: error). "
            "Pin a compatible ref with --ref, or upgrade the CLI."
        )
    logging.warning("%s Installing anyway (on_incompatible: warn).", msg)


def _check_reserved_commands(spec: ExtensionSpec | None) -> None:
    """Refuse an extension that claims a command used to repair extensions.

    Caught at install because that is where a human can read why. The loader
    ignores such a claim too, for manifests edited by hand.
    """
    if spec is None:
        return
    reserved = sorted({c.name for c in spec.commands} & RECOVERY_COMMANDS)
    if reserved:
        raise click.ClickException(
            f"Extension claims {', '.join(repr(r) for r in reserved)}, which "
            "cannot be overridden: these are how you repair or remove an "
            "extension, so an extension owning them could own its own uninstall."
        )


def _check_command_conflicts(
    project_root: Path | None, spec: ExtensionSpec | None, scope: str
) -> None:
    """Raise if the candidate claims a command *another* extension already owns.

    Re-adding the same name at the same scope is a replace, which is how a pin
    moves, so the candidate must not be read as conflicting with the copy it is
    about to overwrite.
    """
    if spec is None:
        return
    installed = load_extension_set(project_root, user_config_root())
    clashes = sorted(
        (contribution.name, owner.extension_name)
        for contribution in spec.commands
        if (owner := installed.commands.get(contribution.name)) is not None
        and owner.scope == scope
        and owner.extension_name != spec.name
    )
    if clashes:
        raise click.ClickException(
            "Command conflict(s) with an already-installed extension: "
            + ", ".join(f"{cmd} (owned by {owner!r})" for cmd, owner in clashes)
            + ". Remove or rename the conflicting extension."
        )


@click.command("add")
@click.argument("reference")
@click.option(
    "--global",
    "global_",
    is_flag=True,
    default=False,
    help=(
        "Install for all projects (user scope, global/org-wide). Default is "
        "project scope (committed to this repo only), so the extension cannot "
        "affect your other projects."
    ),
)
@click.option("--ref", "ref_override", default=None, help="Pin a branch, tag, or SHA.")
@require_confirmation("Install this extension?")
def cmd_add(
    reference: str,
    global_: bool,
    ref_override: str | None,
    auto_approve: bool,
) -> None:
    """Add an extension from a git reference or local path."""
    scope = "user" if global_ else "project"
    try:
        ref = parse_ref(reference, ref_override=ref_override)
    except RefParseError as e:
        raise click.ClickException(str(e)) from e

    if not confirm_trust(ref, auto_approve=auto_approve):
        raise click.ClickException("Aborted: extension not trusted.")

    root = scope_root(scope)
    if root is None:
        raise click.ClickException(
            "No graph-agents-cli-manifest.yaml found. Run inside a project (extensions "
            "install at project scope by default), or pass --global to install "
            "for all projects."
        )
    project_root = root if scope == "project" else find_project_root(Path.cwd())

    try:
        sha = resolve_sha(ref)
        name, extension_dir = materialize(ref, sha, vendored_extensions_dir(root))
    except ResolverError as e:
        raise click.ClickException(str(e)) from e

    # Every gate runs before anything is recorded, so a refused install leaves
    # no entry to roll back. The vendored copy has to exist first (the gates read
    # its manifest), so that is the only thing to clean up.
    try:
        spec = _parse_or_fail(extension_dir)
        _check_compat(spec)
        _check_reserved_commands(spec)
        _check_command_conflicts(project_root, spec, scope)
    except click.ClickException as e:
        if extension_dir.exists():
            shutil.rmtree(extension_dir)
        # Removing the copy of an extension that is still recorded leaves the
        # entry pointing at nothing, and its commands silently fall back to the
        # built-ins until the copy is back.
        if any(entry.name == name for entry in read_extension_entries(root / EXTENSIONS_FILE)):
            raise click.ClickException(
                f"{e.format_message()}\n"
                f"  {name!r} is still recorded; restore its copy with: graph-agents-cli install"
            ) from e
        raise

    entry = ExtensionEntry(name=name, source=ref.raw, ref=ref.ref or "HEAD", sha=sha, scope=scope)
    upsert_extension_entry(root / EXTENSIONS_FILE, entry)

    click.secho(f"Added extension {name!r} ({scope} scope) from {ref.raw}.", fg="green")
    click.secho(
        "Extensions are experimental; the manifest format may still change.",
        dim=True,
    )
