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

"""Fixtures for the extension subsystem: an isolated home, a project, local sources."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from graph_agents_cli import main as main_module


def write_extension(
    directory: Path,
    name: str,
    *,
    add: dict[str, list[str]] | None = None,
    override: dict[str, list[str]] | None = None,
    requires: str | None = None,
    on_incompatible: str = "warn",
) -> Path:
    """Write a local extension source with the given commands; return its directory."""
    directory.mkdir(parents=True, exist_ok=True)
    manifest: dict = {"schema": "graph-agents-cli-extension/v1alpha1", "name": name}
    commands: dict = {}
    if add:
        commands["add"] = {k: {"run": v} for k, v in add.items()}
    if override:
        commands["override"] = {k: {"run": v} for k, v in override.items()}
    if commands:
        manifest["commands"] = commands
    if requires:
        manifest["requires"] = {"agents_cli": requires, "on_incompatible": on_incompatible}
    (directory / "graph-agents-cli-extension.yaml").write_text(yaml.safe_dump(manifest))
    return directory


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """User scope and git cache under a temp home, never the developer's."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.setenv("GRAPH_AGENTS_CLI_NO_UPDATE_CHECK", "1")
    monkeypatch.delenv("GRAPH_AGENTS_CLI_DISABLE_OVERRIDES", raising=False)
    return home


@pytest.fixture
def project(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "graph-agents-cli-manifest.yaml").write_text("name: proj\n")
    monkeypatch.chdir(root)
    return root


@pytest.fixture(autouse=True)
def fresh_root_group() -> Iterator[None]:
    """Extensions are applied once per process on the shared root group: reset it."""

    def _reset() -> None:
        group = main_module.main
        group._extensions_applied = False
        group._overrides.clear()
        for command in list(group.commands.values()):
            overrides = getattr(command, "_overrides", None)
            if isinstance(overrides, dict):
                overrides.clear()

    _reset()
    yield
    _reset()
