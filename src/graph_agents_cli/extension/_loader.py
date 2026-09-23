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

"""Discover active extensions across scopes into a resolved ExtensionSet."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

from graph_agents_cli.__init__ import __version__
from graph_agents_cli.extension._compat import is_compatible
from graph_agents_cli.extension._manifest import EXTENSIONS_FILE
from graph_agents_cli.extension._paths import vendored_extensions_dir
from graph_agents_cli.extension._resolver import ResolverError, validate_extension_name
from graph_agents_cli.extension._spec import (
    EXTENSION_FILE,
    ExtensionCommand,
    ExtensionSpec,
    ExtensionSpecError,
    parse_extension_spec,
)

# Commands no extension may override, ever. They are how a user removes or
# repairs an extension, so letting one own them means a broken extension can own
# its own uninstall. Refused at `extension add` and ignored here, rather than
# only when the extension is out of range: an in-range extension owning
# `extension remove` is the same trap.
RECOVERY_COMMANDS = frozenset(
    {
        "install",
        "extension",
        "extension.add",
        "extension.list",
        "extension.remove",
        "extension.update",
    }
)


@dataclass(frozen=True)
class ResolvedCommand:
    contribution: ExtensionCommand
    extension_name: str
    scope: str
    extension_root: Path
    # Set when the owning extension declared `on_incompatible: error` and the
    # running CLI is outside its range. `main` installs a command that fails
    # with an explanation instead of running the extension's vector — falling back
    # to the built-in would silently do something else (an ADK scaffold in a
    # LangChain project, say).
    blocked_requires: str | None = None


@dataclass(frozen=True)
class ExtensionSet:
    # Tuples (and frozen specs) because a set is parsed once and then read by
    # main, info and extension list; no consumer may mutate it.
    extensions: tuple[ExtensionSpec, ...]
    commands: dict[str, ResolvedCommand]
    conflicts: tuple[str, ...]
    # Extensions whose declared range excludes `running_version`.
    incompatible: tuple[ExtensionSpec, ...] = ()
    running_version: str = __version__

    def command_rows(self) -> list[dict]:
        """Contributed commands, one JSON-ready row each, sorted by command name.

        A blocked row is a command the CLI will refuse to run (its extension is out
        of range in `error` mode), so it is reported but not as active.
        """
        requires_by_name = {
            spec.name: spec.requires_agents_cli
            for spec in self.extensions
            if spec.requires_agents_cli
        }
        return [
            {
                "command": name,
                "extension": rc.extension_name,
                "scope": rc.scope,
                "kind": rc.contribution.kind,
                "run": list(rc.contribution.run),
                "requires": requires_by_name.get(rc.extension_name),
                "blocked": rc.blocked_requires is not None,
            }
            for name, rc in sorted(self.commands.items())
        ]

    def conflict_rows(self) -> list[str]:
        """Command names claimed by more than one extension at one scope."""
        return list(self.conflicts)

    def incompatible_rows(self) -> list[dict]:
        """Out-of-range extensions, one JSON-ready row each."""
        return [
            {
                "extension": spec.name,
                "scope": spec.scope,
                "requires": spec.requires_agents_cli,
                "running": self.running_version,
                "mode": "error" if spec.error_on_incompatible else "warn",
            }
            for spec in self.incompatible
        ]


def incompatible_spec(extension_dir: Path) -> ExtensionSpec | None:
    """The spec if it is out of range for this CLI, else None.

    Raises ExtensionSpecError if the manifest is there but unparseable; an
    absent manifest, or one with no `requires`, returns None.
    """
    spec = load_extension_spec(extension_dir)
    if (
        spec
        and spec.requires_agents_cli
        and not is_compatible(__version__, spec.requires_agents_cli)
    ):
        return spec
    return None


def _load_yaml(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, RecursionError, UnicodeDecodeError, yaml.YAMLError) as e:
        logging.warning("%s: could not read (%s); ignoring.", path, e)
        return {}
    return data if isinstance(data, dict) else {}


def load_extension_spec(root: Path) -> ExtensionSpec | None:
    """Parse an extension's manifest.

    None when there is no manifest; raises ExtensionSpecError when there is a bad one.

    Everything on the load path catches it and carries on. `extension add` and
    `extension update` let it surface, so an install fails instead of reporting
    success for an extension that contributes nothing.
    """
    path = root / EXTENSION_FILE
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, RecursionError, UnicodeDecodeError, yaml.YAMLError) as e:
        # Unreadable, undecodable and unparseable are all as fatal as each other
        # for `add`, and all as skippable for the load path, so they have to
        # arrive as the same error type. RecursionError covers pathological
        # nesting, which yaml raises straight through.
        raise ExtensionSpecError(f"could not be read or parsed: {e}") from e
    if data is not None and not isinstance(data, dict):
        raise ExtensionSpecError(f"expected a mapping, found {type(data).__name__}")
    return parse_extension_spec(data or {}, root=root, default_name=root.name)


def _loaded_spec(root: Path) -> ExtensionSpec | None:
    """`load_extension_spec`, fail-soft: a bad manifest warns and is skipped."""
    try:
        return load_extension_spec(root)
    except ExtensionSpecError as e:
        logging.warning("%s: %s; ignoring this extension.", root / EXTENSION_FILE, e)
        return None


def _vendored_specs(scope_root: Path) -> list[ExtensionSpec]:
    """Load specs for extensions recorded under `extensions:` and vendored on disk."""
    manifest = scope_root / EXTENSIONS_FILE
    if not manifest.exists():
        return []
    data = _load_yaml(manifest)
    raw = data.get("extensions") or []
    specs: list[ExtensionSpec] = []
    if not isinstance(raw, list):
        return specs
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        try:
            safe_name = validate_extension_name(item["name"])
        except ResolverError as e:
            logging.warning("Skipping extension with %s", e)
            continue
        extension_dir = vendored_extensions_dir(scope_root) / safe_name
        try:
            spec = load_extension_spec(extension_dir)
        except ExtensionSpecError as e:
            logging.warning("%s: %s; ignoring this extension.", extension_dir, e)
            continue

        if spec is None:
            logging.warning(
                "Extension %r is recorded in %s but not vendored at %s; "
                "run `graph-agents-cli install`.",
                item["name"],
                manifest,
                extension_dir,
            )
            continue
        # The manifest id is the extension's stable identity (used by `extension
        # remove`/`update`); keep it even if the upstream yaml `name` was
        # renamed after the extension was pinned.
        specs.append(replace(spec, name=safe_name))
    return specs


def load_extension_set(project_root: Path | None, user_root: Path | None) -> ExtensionSet:
    # Lowest precedence first (user), highest last (project), so project-scope
    # entries win by overwriting user-scope ones in the loop below.
    discovered: list[ExtensionSpec] = []
    for root, scope in ((user_root, "user"), (project_root, "project")):
        if root is None:
            continue
        inline = _loaded_spec(root)
        if inline is not None:
            discovered.append(replace(inline, scope=scope))
        for spec in _vendored_specs(root):
            discovered.append(replace(spec, scope=scope))

    commands: dict[str, ResolvedCommand] = {}
    # (scope, command_name) -> extension_name that first claimed it at that scope.
    claimed_same_scope: dict[tuple[str, str], str] = {}
    conflicts: list[str] = []
    incompatible: list[ExtensionSpec] = []

    for spec in discovered:
        scope = spec.scope
        # An extension whose declared `requires.agents_cli` range excludes the
        # running CLI is recorded here. "warn" applies its contributions anyway;
        # "error" keeps the command claimed but marks it blocked, so invoking it
        # fails with an explanation rather than silently running the built-in,
        # which for an override does something else entirely.
        blocked_requires: str | None = None
        if spec.requires_agents_cli and not is_compatible(__version__, spec.requires_agents_cli):
            incompatible.append(spec)
            if spec.error_on_incompatible:
                blocked_requires = spec.requires_agents_cli
        for contribution in spec.commands:
            key = contribution.name
            same_scope_key = (scope, key)
            if same_scope_key in claimed_same_scope:
                conflicts.append(f"{scope}:{key}")
                logging.warning(
                    "Command %r claimed by multiple %s-scope extensions "
                    "(%s and %s); ignoring the later one.",
                    key,
                    scope,
                    claimed_same_scope[same_scope_key],
                    spec.name,
                )
                continue
            claimed_same_scope[same_scope_key] = spec.name
            if key in RECOVERY_COMMANDS:
                # `extension add` refuses these, so reaching here means a
                # hand-edited manifest or one installed by an older CLI.
                logging.warning(
                    "Extension %r claims %r, which cannot be overridden (it is how "
                    "you repair or remove an extension); using the built-in.",
                    spec.name,
                    key,
                )
                continue
            commands[key] = ResolvedCommand(
                contribution=contribution,
                extension_name=spec.name,
                scope=scope,
                extension_root=spec.root,
                blocked_requires=blocked_requires,
            )

    return ExtensionSet(
        extensions=tuple(discovered),
        commands=commands,
        conflicts=tuple(conflicts),
        incompatible=tuple(incompatible),
    )
