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

"""Read/write the installed-extension list.

Its own file rather than a block in graph-agents-cli-manifest.yaml: this is
read-modify-written on every add/remove/update, and a bug here should not be
able to reach a project's scaffold state. Same filename at both scopes, under
the project root or the user config dir, so there is nothing to branch on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

EXTENSIONS_FILE = "graph-agents-cli-extensions.yaml"


@dataclass(frozen=True)
class ExtensionEntry:
    name: str
    source: str
    ref: str
    sha: str
    scope: str


def _read_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as e:
        # add/remove/update and install's sync all read this; a hand-edited
        # file should not end them in a traceback.
        logging.warning("Ignoring unparseable %s: %s", path, e)
        return {}
    if isinstance(raw, dict):
        return raw  # type: ignore[return-value]
    return {}


def _write_manifest(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)


def read_extension_entries(manifest_path: Path) -> list[ExtensionEntry]:
    """Read extension entries from a manifest file.

    Returns an empty list when the file is missing, unparseable, or has no
    valid ``extensions:`` list.
    """
    data = _read_manifest(manifest_path)
    raw = data.get("extensions") or []
    entries: list[ExtensionEntry] = []
    if not isinstance(raw, list):
        return entries
    for item in raw:
        if not isinstance(item, dict):
            logging.warning(
                "%s: ignoring malformed `extensions:` entry %r (expected a mapping).",
                manifest_path,
                item,
            )
            continue
        entries.append(
            ExtensionEntry(
                name=str(item.get("name") or ""),
                source=str(item.get("source") or ""),
                ref=str(item.get("ref") or ""),
                sha=str(item.get("sha") or ""),
                scope=str(item.get("scope") or ""),
            )
        )
    return entries


def upsert_extension_entry(manifest_path: Path, entry: ExtensionEntry) -> None:
    """Insert or replace an extension entry in the manifest, preserving all other keys.

    Creates the manifest file (and any missing parent directories) if absent.
    """
    data = _read_manifest(manifest_path)
    existing: list[Any] = data.get("extensions") or []
    # Drop only the entry being replaced. Anything we don't recognise is kept
    # verbatim — rewriting the manifest must not delete a user's content.
    extensions: list[Any] = [
        p for p in existing if not (isinstance(p, dict) and p.get("name") == entry.name)
    ]
    extensions.append(
        {
            "name": entry.name,
            "source": entry.source,
            "ref": entry.ref,
            "sha": entry.sha,
            "scope": entry.scope,
        }
    )
    data["extensions"] = extensions
    _write_manifest(manifest_path, data)


def remove_extension_entry(manifest_path: Path, name: str) -> bool:
    """Remove an extension entry by name.

    Returns True if an entry was removed, False if no entry matched.
    """
    data = _read_manifest(manifest_path)
    existing: list[Any] = data.get("extensions") or []
    # Same rule as upsert: unrecognised entries survive, only `name` matches go.
    kept: list[Any] = [p for p in existing if not (isinstance(p, dict) and p.get("name") == name)]
    if len(kept) == len(existing):
        return False
    data["extensions"] = kept
    _write_manifest(manifest_path, data)
    return True
