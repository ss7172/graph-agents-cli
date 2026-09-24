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

"""`extension add/update/remove`, restore, and overrides through the root group."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from graph_agents_cli import main as main_module
from graph_agents_cli.extension import _schema
from graph_agents_cli.extension._manifest import EXTENSIONS_FILE, read_extension_entries
from graph_agents_cli.extension._sync import sync_extensions

from .conftest import write_extension


def cli(*args: str, input: str | None = None):
    """One CLI invocation: like a new process, extensions are discovered afresh."""
    group = main_module.main
    group._extensions_applied = False
    group._overrides.clear()
    for command in list(group.commands.values()):
        overrides = getattr(command, "_overrides", None)
        if isinstance(overrides, dict):
            overrides.clear()
    return CliRunner().invoke(group, list(args), input=input)


def _entries(root: Path) -> list:
    return read_extension_entries(root / EXTENSIONS_FILE)


def test_add_accepts_a_bare_absolute_path(project: Path, tmp_path: Path) -> None:
    """It used to fail with "could not resolve 'HEAD' in '/abs/dir'"."""
    src = write_extension(tmp_path / "ext", "team-tools", add={"hello": ["echo", "hi-there"]})
    result = cli("extension", "add", str(src), "-y")
    assert result.exit_code == 0, result.output
    (entry,) = _entries(project)
    assert entry.name == "team-tools" and entry.sha == "local"
    assert entry.source == "local@../ext"  # recorded against the project root
    assert (project / "extensions" / "team-tools" / "graph-agents-cli-extension.yaml").exists()


def test_add_outside_a_project_is_a_configuration_error(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 3 like every command that needs a project, and before any trust prompt."""
    src = write_extension(tmp_path / "ext", "team-tools", add={"hello": ["echo", "hi"]})
    elsewhere = tmp_path / "not-a-project"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    result = cli("extension", "add", str(src), "-i", input="y\n")
    assert result.exit_code == 3, result.output
    assert "No graph-agents-cli-manifest.yaml found" in result.output
    assert "--global" in result.output
    assert "trust" not in result.output.lower()
    assert not (elsewhere / "extensions").exists()


def test_relative_paths_are_recorded_against_the_project_root(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_extension(tmp_path / "shared" / "tools", "tools")
    (project / "app" / "deep").mkdir(parents=True)
    monkeypatch.chdir(project / "app" / "deep")
    result = cli("extension", "add", "../../../shared/tools", "-y")
    assert result.exit_code == 0, result.output
    assert _entries(project)[0].source == "local@../shared/tools"

    # A restore run from anywhere finds the source again.
    shutil.rmtree(project / "extensions" / "tools")
    monkeypatch.chdir(tmp_path)
    assert sync_extensions(project) == ["tools"]
    assert (project / "extensions" / "tools").is_dir()


@pytest.mark.parametrize("reference", ["/nonexistent/ext", "local@missing-dir"])
def test_a_local_path_that_is_not_an_extension_is_a_config_error(
    project: Path, reference: str
) -> None:
    result = cli("extension", "add", reference, "-y")
    assert result.exit_code == 3, result.output
    assert "local extension path not found" in result.output
    assert not (project / EXTENSIONS_FILE).exists()


def test_a_repo_shorthand_that_is_also_a_local_directory_gets_a_hint(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graph_agents_cli.extension import cmd_extension_add
    from graph_agents_cli.extension._resolver import ResolverError

    def unreachable(ref):
        raise ResolverError(f"could not resolve 'HEAD' in {ref.repo!r}; check the repo and ref.")

    monkeypatch.setattr(cmd_extension_add, "resolve_sha", unreachable)
    (project / "acme" / "tools").mkdir(parents=True)
    result = cli("extension", "add", "acme/tools", "-y")
    assert result.exit_code == 2, result.output
    assert "use local@acme/tools" in result.output


def test_update_says_already_up_to_date_when_nothing_changed(project: Path, tmp_path: Path) -> None:
    """It used to print 'Updated: team-tools.' after changing nothing."""
    src = write_extension(tmp_path / "ext", "team-tools", add={"hello": ["echo", "v1"]})
    assert cli("extension", "add", str(src), "-y").exit_code == 0
    result = cli("extension", "update", "-y")
    assert result.exit_code == 0, result.output
    assert "Already up to date: team-tools." in result.output
    assert "Updated:" not in result.output

    write_extension(src, "team-tools", add={"hello": ["echo", "v2"]})
    result = cli("extension", "update", "-y")
    assert result.exit_code == 0, result.output
    assert "Updated: team-tools." in result.output
    vendored = yaml.safe_load(
        (project / "extensions" / "team-tools" / "graph-agents-cli-extension.yaml").read_text()
    )
    assert vendored["commands"]["add"]["hello"]["run"] == ["echo", "v2"]


def test_update_checks_for_changes_before_asking_for_trust(project: Path, tmp_path: Path) -> None:
    """Nothing changed: no trust prompt, even without a terminal or -y."""
    src = write_extension(tmp_path / "ext", "team-tools", add={"hello": ["echo", "v1"]})
    assert cli("extension", "add", str(src), "-y").exit_code == 0
    result = cli("extension", "update", "team-tools", input="")
    assert result.exit_code == 0, result.output
    assert "Already up to date: team-tools." in result.output
    assert "trust" not in result.output.lower()


def test_update_of_changed_code_without_a_terminal_keeps_the_pin_and_exits_1(
    project: Path, tmp_path: Path
) -> None:
    src = write_extension(tmp_path / "ext", "team-tools", add={"hello": ["echo", "v1"]})
    assert cli("extension", "add", str(src), "-y").exit_code == 0
    before = _entries(project)[0].sha
    write_extension(src, "team-tools", add={"hello": ["echo", "v2"]})
    result = cli("extension", "update", input="")
    assert result.exit_code == 1, result.output
    assert "Pass -y" in result.output and "rerun with -y" in result.output
    assert "Updated:" not in result.output
    assert _entries(project)[0].sha == before
    vendored = yaml.safe_load(
        (project / "extensions" / "team-tools" / "graph-agents-cli-extension.yaml").read_text()
    )
    assert vendored["commands"]["add"]["hello"]["run"] == ["echo", "v1"]
    # -y trusts the new code.
    result = cli("extension", "update", "-y")
    assert result.exit_code == 0, result.output
    assert "Updated: team-tools." in result.output


def test_add_without_a_terminal_says_to_pass_y(project: Path, tmp_path: Path) -> None:
    src = write_extension(tmp_path / "ext", "team-tools", add={"hello": ["echo", "v1"]})
    result = cli("extension", "add", str(src), input="y\n")
    assert result.exit_code == 1, result.output
    assert "Pass -y" in result.output
    assert not _entries(project)


def test_added_and_overriding_commands_run_and_can_be_bypassed(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = write_extension(
        tmp_path / "ext",
        "gate",
        add={"hello": ["echo", "hello-from-extension"]},
        override={"lint": ["echo", "team lint"]},
    )
    assert cli("extension", "add", str(src), "-y").exit_code == 0

    result = cli("hello")
    assert result.exit_code == 0, result.output
    assert "hello-from-extension" in result.output
    result = cli("lint")
    assert "team lint" in result.output
    # A clean notice, not Python's "WARNING:root:" (the root handler is set up
    # outside tests; here the message itself is what matters).
    assert "WARNING:root" not in result.output

    monkeypatch.setenv("GRAPH_AGENTS_CLI_DISABLE_OVERRIDES", "1")
    result = cli("lint", "--help")
    assert "team lint" not in result.output


def test_reserved_commands_cannot_be_claimed(project: Path, tmp_path: Path) -> None:
    src = write_extension(tmp_path / "ext", "evil", override={"extension.remove": ["true"]})
    result = cli("extension", "add", str(src), "-y")
    assert result.exit_code == 1, result.output
    assert "cannot be overridden" in result.output
    assert not (project / "extensions" / "evil").exists()
    assert _entries(project) == []


def test_an_out_of_range_error_mode_extension_is_refused_at_add(
    project: Path, tmp_path: Path
) -> None:
    src = write_extension(
        tmp_path / "ext", "future", add={"x": ["true"]}, requires=">=99", on_incompatible="error"
    )
    result = cli("extension", "add", str(src), "-y")
    assert result.exit_code == 1, result.output
    assert "Not installing (on_incompatible: error)" in result.output


def test_list_and_remove(project: Path, tmp_path: Path) -> None:
    src = write_extension(tmp_path / "ext", "team-tools", add={"hello": ["echo", "x"]})
    assert cli("extension", "add", str(src), "-y").exit_code == 0
    result = cli("extension", "list")
    assert result.exit_code == 0, result.output
    assert "team-tools  project  (commands: hello)" in result.output
    result = cli("extension", "remove", "team-tools", "-y")
    assert result.exit_code == 0, result.output
    assert _entries(project) == []
    assert not (project / "extensions" / "team-tools").exists()


def test_checked_in_schema_matches_the_models() -> None:
    """The published schema is generated from the wire models; its $id names this repo."""
    assert _schema.SCHEMA_PATH.is_file(), _schema.SCHEMA_PATH
    assert _schema.SCHEMA_PATH.read_text(encoding="utf-8") == _schema.render()
    assert "ss7172/graph-agents-cli" in _schema.SCHEMA_ID
