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

"""Tests for the skills-version check opt-out and offline behaviour (D21)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from graph_agents_cli import _skills_check
from graph_agents_cli._tools import ToolNotFoundError
from graph_agents_cli.skills import _bundle


def _install_skill(home: Path, name: str, version: str) -> None:
    skill = home / ".agents" / "skills" / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\nmetadata:\n  version: {version}\n---\n# {name}\n", encoding="utf-8"
    )


@pytest.fixture(autouse=True)
def _fresh_stamp(home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        _skills_check, "_SKILLS_CHECK_STAMP", home / ".agents" / ".graph_agents_cli_skills_check"
    )


@pytest.fixture
def no_npx(monkeypatch: pytest.MonkeyPatch):
    def boom(*args, **kwargs):
        raise ToolNotFoundError("'npx' is not installed or not on PATH.")

    monkeypatch.setattr("graph_agents_cli._runner.run_resolved", boom)


def test_prefix_filters_installed_skills(home: Path, no_npx):
    _install_skill(home, "graph-agents-cli-workflow", "0.1.0")
    _install_skill(home, "google-agents-cli-workflow", "1.6.1")
    _install_skill(home, "other-skill", "9.9.9")
    assert _skills_check._find_installed_skills() == {"graph-agents-cli-workflow": "0.1.0"}
    assert _skills_check.SKILL_PREFIX == "graph-agents-cli-"
    assert _bundle.SKILL_PREFIX == _skills_check.SKILL_PREFIX


def test_warns_on_version_mismatch(home: Path, no_npx, capsys, monkeypatch):
    monkeypatch.setattr("graph_agents_cli.__version__", "0.1.0")
    _install_skill(home, "graph-agents-cli-eval", "0.0.9")
    _skills_check.check_skills_version()
    err = capsys.readouterr().err
    assert "Skills version mismatch" in err
    assert "graph-agents-cli-eval (v0.0.9)" in err
    assert _skills_check._SKILLS_CHECK_STAMP.is_file()


def test_opt_out_env_skips_check(home: Path, no_npx, capsys, monkeypatch):
    monkeypatch.setattr("graph_agents_cli.__version__", "0.1.0")
    _install_skill(home, "graph-agents-cli-eval", "0.0.9")
    monkeypatch.setenv("GRAPH_AGENTS_CLI_NO_UPDATE_CHECK", "1")
    _skills_check.check_skills_version()
    assert capsys.readouterr().err == ""
    assert not _skills_check._SKILLS_CHECK_STAMP.exists()


def test_ci_skips_check(home: Path, no_npx, capsys, monkeypatch):
    monkeypatch.setattr("graph_agents_cli.__version__", "0.1.0")
    _install_skill(home, "graph-agents-cli-eval", "0.0.9")
    monkeypatch.setenv("CI", "true")
    _skills_check.check_skills_version()
    assert capsys.readouterr().err == ""


def test_silent_when_npx_missing(home: Path, no_npx, capsys, caplog):
    """No skills in the store and no npx: nothing printed, nothing above DEBUG logged."""
    with caplog.at_level(logging.DEBUG):
        _skills_check.check_skills_version()
        assert _skills_check.get_installed_skills() is None
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    assert all(r.levelno <= logging.DEBUG for r in caplog.records)


def test_rate_limited_by_stamp(home: Path, no_npx, capsys, monkeypatch):
    monkeypatch.setattr("graph_agents_cli.__version__", "0.1.0")
    _install_skill(home, "graph-agents-cli-eval", "0.0.9")
    _skills_check.check_skills_version()
    assert "mismatch" in capsys.readouterr().err
    _skills_check.check_skills_version()
    assert capsys.readouterr().err == ""


def test_malformed_skill_md_is_ignored(home: Path, no_npx, tmp_path: Path):
    skill = home / ".agents" / "skills" / "graph-agents-cli-broken"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("no frontmatter here")
    assert _skills_check._parse_skill_version(skill / "SKILL.md") is None
    assert _skills_check._find_installed_skills() == {}


def test_bundle_helpers(tmp_path: Path):
    bundle = tmp_path / "data"
    (bundle / "graph-agents-cli-deploy").mkdir(parents=True)
    (bundle / "graph-agents-cli-deploy" / "SKILL.md").write_text("---\n---\n")
    (bundle / "README.md").write_text("x")
    (bundle / "graph-agents-cli-empty").mkdir()
    assert [p.name for p in _bundle.list_bundled_skills(bundle)] == ["graph-agents-cli-deploy"]
    assert _bundle.is_skill_dir(bundle / "graph-agents-cli-empty") is False
    assert _bundle.list_bundled_skills(tmp_path / "missing") == []


def test_real_bundle_is_present_and_prefixed():
    skills = _bundle.list_bundled_skills()
    assert skills, "the wheel bundle must ship at least one skill"
    assert all(p.name.startswith("graph-agents-cli-") for p in skills)
    assert _bundle.get_bundled_skills_dir() == _bundle.SKILL_BUNDLE_DIR
