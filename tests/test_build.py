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

"""The build identity: ids, recorded facts, git facts, --version and the uv cache keys."""

from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from graph_agents_cli import _build
from graph_agents_cli._build import BuildInfo, build_of_package, git_facts

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMIT = "1a2b3c4d5e6f" + "0" * 28


def test_build_ids() -> None:
    assert BuildInfo("0.2.0").id == "0.2.0"  # unknown source
    assert BuildInfo("0.2.0", COMMIT).id == "0.2.0+g1a2b3c4"
    assert BuildInfo("0.2.0", COMMIT, dirty=True).id == "0.2.0+g1a2b3c4.dirty"
    assert BuildInfo("0.2.0", COMMIT, release=True).id == "0.2.0"
    assert BuildInfo("0.2.0", COMMIT, release=True).is_release
    assert not BuildInfo("0.2.0").is_release
    # A release flag never hides uncommitted changes.
    assert BuildInfo("0.2.0", COMMIT, dirty=True, release=True).id == "0.2.0+g1a2b3c4.dirty"
    assert "not a release" in BuildInfo("0.2.0", COMMIT).describe()
    assert "source unknown" in BuildInfo("0.2.0").describe()


def test_recorded_facts_win(tmp_path: Path) -> None:
    package = tmp_path / "site-packages" / "graph_agents_cli"
    package.mkdir(parents=True)
    (package / _build.BUILD_INFO_FILENAME).write_text(
        json.dumps({"commit": COMMIT, "dirty": False, "release": False})
    )
    assert build_of_package(package, "0.2.0") == BuildInfo("0.2.0", COMMIT)


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({"commit": "short", "dirty": False}),
        json.dumps(["a", "list"]),
    ],
)
def test_unreadable_facts_mean_an_unknown_build(tmp_path: Path, content: str) -> None:
    package = tmp_path / "site-packages" / "graph_agents_cli"
    package.mkdir(parents=True)
    (package / _build.BUILD_INFO_FILENAME).write_text(content)
    assert build_of_package(package, "0.2.0") == BuildInfo("0.2.0")


def test_recorded_facts_without_an_explicit_clean_flag_count_as_dirty(tmp_path: Path) -> None:
    package = tmp_path / "graph_agents_cli"
    package.mkdir()
    (package / _build.BUILD_INFO_FILENAME).write_text(
        json.dumps({"commit": COMMIT, "release": True})
    )
    build = build_of_package(package, "0.2.0")
    assert build.dirty and not build.is_release


# --- git facts of a checkout ------------------------------------------------


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "tag.gpgsign=false",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    root = tmp_path / "checkout"
    package = root / "src" / "graph_agents_cli"
    package.mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'graph-agents-cli'\n")
    (root / "README.md").write_text("readme\n")
    (package / "__init__.py").write_text("X = 1\n")
    (root / ".gitignore").write_text("__pycache__/\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "one")
    return root


def test_a_clean_commit_is_identified_by_it(checkout: Path) -> None:
    facts = git_facts(checkout, "0.2.0")
    assert facts is not None
    head = _git(checkout, "rev-parse", "HEAD")
    assert facts["commit"] == head and facts["dirty"] is False and facts["release"] is False
    build = build_of_package(checkout / "src" / "graph_agents_cli", "0.2.0")
    assert build.id == f"0.2.0+g{head[:7]}"


def test_the_release_tag_makes_a_release_build(checkout: Path) -> None:
    _git(checkout, "tag", "v0.2.0")
    build = build_of_package(checkout / "src" / "graph_agents_cli", "0.2.0")
    assert build.is_release and build.id == "0.2.0"
    # Another version's tag is not this release.
    assert not build_of_package(checkout / "src" / "graph_agents_cli", "0.3.0").is_release


@pytest.mark.parametrize(
    "change",
    [
        lambda root: (root / "src" / "graph_agents_cli" / "__init__.py").write_text("X = 2\n"),
        lambda root: (root / "src" / "graph_agents_cli" / "new.py").write_text("Y = 1\n"),
        lambda root: (root / "pyproject.toml").write_text("[project]\nname = 'other'\n"),
    ],
    ids=["edited", "untracked", "pyproject"],
)
def test_uncommitted_source_changes_make_a_dirty_build(checkout: Path, change) -> None:
    _git(checkout, "tag", "v0.2.0")
    change(checkout)
    build = build_of_package(checkout / "src" / "graph_agents_cli", "0.2.0")
    assert build.dirty and not build.is_release
    assert build.id.endswith(".dirty") and "+g" in build.id


def test_changes_outside_the_package_sources_are_not_dirty(checkout: Path) -> None:
    (checkout / "README.md").write_text("edited\n")
    (checkout / "src" / "graph_agents_cli" / "__pycache__").mkdir()
    (checkout / "src" / "graph_agents_cli" / "__pycache__" / "x.pyc").write_bytes(b"\0")
    assert git_facts(checkout, "0.2.0")["dirty"] is False  # type: ignore[index]


def test_a_directory_inside_another_repository_is_not_a_checkout(checkout: Path) -> None:
    nested = checkout / "src" / "vendored"
    (nested / "src" / "graph_agents_cli").mkdir(parents=True)
    (nested / "pyproject.toml").write_text("[project]\n")
    assert git_facts(nested, "0.2.0") is None
    assert build_of_package(nested / "src" / "graph_agents_cli", "0.2.0") == BuildInfo("0.2.0")


def test_a_tree_without_git_is_an_unknown_build(tmp_path: Path) -> None:
    package = tmp_path / "src" / "graph_agents_cli"
    package.mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    assert build_of_package(package, "0.2.0") == BuildInfo("0.2.0")


# --- --version and info -----------------------------------------------------


def test_version_prints_the_build_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from graph_agents_cli import main as main_module

    monkeypatch.setattr(_build, "current_build", lambda: BuildInfo("0.2.0", COMMIT))
    result = CliRunner().invoke(main_module.main, ["--version"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "graph-agents-cli, version 0.2.0+g1a2b3c4"

    monkeypatch.setattr(_build, "current_build", lambda: BuildInfo("0.2.0", COMMIT, release=True))
    result = CliRunner().invoke(main_module.main, ["--version"])
    # The release workflow checks that a tagged build ends with "version <tag version>".
    assert result.output.strip() == "graph-agents-cli, version 0.2.0"


def test_info_shows_the_build(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from graph_agents_cli.info import cmd_info

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cmd_info, "current_build", lambda: BuildInfo("0.2.0", COMMIT, dirty=True))
    monkeypatch.setattr(cmd_info, "get_installed_skills", lambda: [])
    text = CliRunner().invoke(cmd_info.cmd_info, [])
    assert text.exit_code == 0, text.output
    assert "CLI build:          0.2.0+g1a2b3c4.dirty (commit 1a2b3c4d5e6f with uncommitted" in (
        text.output
    )
    as_json = CliRunner().invoke(cmd_info.cmd_info, ["--json"])
    assert json.loads(as_json.output)["cli_build"] == {
        "id": "0.2.0+g1a2b3c4.dirty",
        "commit": COMMIT,
        "dirty": True,
        "release": False,
    }


# --- packaging ----------------------------------------------------------------


def test_uv_rebuilds_a_checkout_whenever_its_commit_or_sources_change() -> None:
    """Without these keys uv reinstalls a stale cached wheel of a moved checkout."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    keys = pyproject["tool"]["uv"]["cache-keys"]
    assert {"git": {"commit": True, "tags": True}} in keys
    assert {"file": "src/**/*"} in keys
    assert {"file": "pyproject.toml"} in keys and {"file": "hatch_build.py"} in keys
    # The hook that records the build, and the sdist that must carry it.
    assert "custom" in pyproject["tool"]["hatch"]["build"]["hooks"]
    assert "/hatch_build.py" in pyproject["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    assert (REPO_ROOT / "hatch_build.py").is_file()
