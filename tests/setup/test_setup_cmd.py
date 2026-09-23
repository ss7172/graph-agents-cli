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

"""Tests for `graph-agents-cli setup` and `update`: args, the install ladder, --dev."""

from __future__ import annotations

import subprocess
from pathlib import Path

import click
import pytest

from graph_agents_cli._skills_check import SKILLS_NPX_PACKAGE
from graph_agents_cli._tools import DEFAULT_SKILLS_SOURCE
from graph_agents_cli.scaffold.utils import version as version_mod
from graph_agents_cli.setup import cmd_setup, cmd_update
from graph_agents_cli.setup.cmd_setup import (
    _build_skills_args,
    _install_skills,
    _resolve_skills_source,
)
from graph_agents_cli.setup.cmd_setup import (
    cmd_setup as setup_command,
)
from graph_agents_cli.setup.cmd_update import cmd_update as update_command


def _bundle(tmp_path: Path, names=("graph-agents-cli-workflow", "graph-agents-cli-eval")) -> Path:
    bundle = tmp_path / "bundle"
    for name in names:
        (bundle / name).mkdir(parents=True)
        (bundle / name / "SKILL.md").write_text(
            f"---\nname: {name}\nmetadata:\n  version: 0.1.0\n---\n", encoding="utf-8"
        )
    (bundle / "not-a-skill").mkdir()
    return bundle


@pytest.fixture
def bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = _bundle(tmp_path)
    monkeypatch.setattr(cmd_setup, "get_bundled_skills_dir", lambda: path)
    monkeypatch.setattr(cmd_setup, "SKILL_BUNDLE_DIR", path)
    return path


# ── argument building ────────────────────────────────────────────────────────


def test_build_args_global_default():
    assert _build_skills_args("src", workspace=False, agent=()) == ["add", "src", "-y", "-g"]


def test_build_args_workspace_and_agents():
    args = _build_skills_args("src", workspace=True, agent=("claude-code", "cursor"))
    assert args == ["add", "src", "-y", "--agent", "claude-code", "--agent", "cursor"]


def test_build_args_all_agents():
    assert _build_skills_args("src", workspace=False, agent=("all",)) == [
        "add",
        "src",
        "-y",
        "--all",
        "-g",
    ]


def test_resolve_source_default_and_dev(bundle: Path):
    assert _resolve_skills_source(None, dev=False) == DEFAULT_SKILLS_SOURCE
    assert DEFAULT_SKILLS_SOURCE == "https://github.com/ss7172/graph-agents-cli"
    assert _resolve_skills_source(None, dev=True) == str(bundle)


def test_resolve_source_local_path_is_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "my-skills").mkdir()
    assert _resolve_skills_source("./my-skills", dev=False) == str(
        (tmp_path / "my-skills").resolve()
    )
    assert _resolve_skills_source("acme/skills", dev=False) == "acme/skills"


# ── install ladder ───────────────────────────────────────────────────────────


class _FakeNpx:
    """Records npx skills calls and fails the first ``fail_first`` of them."""

    def __init__(self, fail_first: int) -> None:
        self.fail_first = fail_first
        self.calls: list[list[str]] = []

    def __call__(self, args, spinner_msg):
        self.calls.append(list(args))
        if len(self.calls) <= self.fail_first:
            raise click.ClickException("npx failed")


def test_ladder_remote_succeeds(bundle: Path, monkeypatch: pytest.MonkeyPatch):
    npx = _FakeNpx(fail_first=0)
    monkeypatch.setattr(cmd_setup, "run_npx_skills", npx)
    result = _install_skills(DEFAULT_SKILLS_SOURCE, workspace=False, agent=(), allow_fallback=True)
    assert result == "remote"
    assert npx.calls == [["add", DEFAULT_SKILLS_SOURCE, "-y", "-g"]]


def test_ladder_falls_back_to_bundled_dir(bundle: Path, monkeypatch: pytest.MonkeyPatch):
    npx = _FakeNpx(fail_first=1)
    monkeypatch.setattr(cmd_setup, "run_npx_skills", npx)
    result = _install_skills(DEFAULT_SKILLS_SOURCE, workspace=False, agent=(), allow_fallback=True)
    assert result == "bundled"
    assert npx.calls[1] == ["add", str(bundle), "-y", "-g"]


def test_ladder_copies_bundle_when_npx_unavailable(
    bundle: Path, home: Path, monkeypatch: pytest.MonkeyPatch
):
    npx = _FakeNpx(fail_first=99)
    monkeypatch.setattr(cmd_setup, "run_npx_skills", npx)
    result = _install_skills(DEFAULT_SKILLS_SOURCE, workspace=False, agent=(), allow_fallback=True)
    assert result == "copy"
    assert len(npx.calls) == 2
    store = home / ".agents" / "skills"
    assert (store / "graph-agents-cli-workflow" / "SKILL.md").is_file()
    assert (store / "graph-agents-cli-eval" / "SKILL.md").is_file()
    assert not (store / "not-a-skill").exists()


def test_ladder_copy_uses_workspace_store(
    bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(cmd_setup, "run_npx_skills", _FakeNpx(fail_first=99))
    assert _install_skills("x", workspace=True, agent=(), allow_fallback=True) == "copy"
    assert (work / ".agents" / "skills" / "graph-agents-cli-workflow" / "SKILL.md").is_file()


def test_ladder_replaces_existing_copy(bundle: Path, home: Path, monkeypatch: pytest.MonkeyPatch):
    stale = home / ".agents" / "skills" / "graph-agents-cli-workflow"
    stale.mkdir(parents=True)
    (stale / "old.txt").write_text("stale")
    monkeypatch.setattr(cmd_setup, "run_npx_skills", _FakeNpx(fail_first=99))
    _install_skills("x", workspace=False, agent=(), allow_fallback=True)
    assert not (stale / "old.txt").exists()
    assert (stale / "SKILL.md").is_file()


def test_explicit_source_never_falls_back(bundle: Path, monkeypatch: pytest.MonkeyPatch):
    npx = _FakeNpx(fail_first=99)
    monkeypatch.setattr(cmd_setup, "run_npx_skills", npx)
    with pytest.raises(click.ClickException):
        _install_skills("acme/skills", workspace=False, agent=(), allow_fallback=False)
    assert len(npx.calls) == 1


def test_ladder_skips_redundant_bundled_run(bundle: Path, monkeypatch: pytest.MonkeyPatch):
    """`--dev` already points at the bundle, so rung 2 is skipped."""
    npx = _FakeNpx(fail_first=99)
    monkeypatch.setattr(cmd_setup, "run_npx_skills", npx)
    result = _install_skills(str(bundle), workspace=False, agent=(), allow_fallback=True)
    assert result == "copy"
    assert len(npx.calls) == 1


# ── the command ──────────────────────────────────────────────────────────────


def test_setup_has_no_auth_flags(runner):
    result = runner.invoke(setup_command, ["--help"])
    assert result.exit_code == 0
    assert "--skip-auth" not in result.output
    assert "--interactive" not in result.output
    for flag in ("--workspace", "--dry-run", "--dev", "--skills-source", "--agent"):
        assert flag in result.output


def test_setup_dry_run_prints_commands(runner, bundle: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(cmd_setup, "run", lambda *a, **k: calls.append(a[0]))
    monkeypatch.setattr(cmd_setup, "run_npx_skills", _FakeNpx(fail_first=0))
    monkeypatch.delenv(version_mod.INSTALL_SPEC_ENV, raising=False)
    result = runner.invoke(setup_command, ["--dry-run", "--agent", "claude-code"])
    assert result.exit_code == 0, result.output
    # The CLI installs from the pinned git spec of the running version, never a bare name.
    spec = version_mod.install_spec(version_mod.get_current_version())
    assert spec.startswith("git+https://github.com/ss7172/graph-agents-cli")
    assert f"uv tool install {spec}" in result.output
    assert f"npx -y {SKILLS_NPX_PACKAGE} add {DEFAULT_SKILLS_SOURCE} -y --agent claude-code -g" in (
        result.output
    )
    assert "No changes made" in result.output
    assert "Auth" not in result.output
    assert calls == []


def test_setup_dry_run_dev_requires_repo_root(runner, project: Path):
    result = runner.invoke(setup_command, ["--dry-run", "--dev"])
    assert result.exit_code != 0
    assert "--dev requires running from the root" in result.output


def test_setup_dry_run_dev_from_repo_root(runner, project: Path, bundle: Path):
    (project / "pyproject.toml").write_text('[project]\nname = "graph-agents-cli"\n')
    result = runner.invoke(setup_command, ["--dry-run", "--dev"])
    assert result.exit_code == 0, result.output
    assert f"uv tool install --force --editable {project}" in result.output
    assert f"add {bundle} -y -g" in result.output


def test_setup_installs_cli_and_skills(runner, bundle: Path, monkeypatch: pytest.MonkeyPatch):
    runs: list[list[str]] = []

    def fake_run(args, **kwargs):
        runs.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout="Installed graph-agents-cli", stderr="")

    monkeypatch.setattr(cmd_setup, "run", fake_run)
    npx = _FakeNpx(fail_first=0)
    monkeypatch.setattr(cmd_setup, "run_npx_skills", npx)
    monkeypatch.setenv(version_mod.INSTALL_SPEC_ENV, "/srv/mirror/graph_agents_cli.whl")
    result = runner.invoke(setup_command, ["--workspace"])
    assert result.exit_code == 0, result.output
    assert runs == [["uv", "tool", "install", "/srv/mirror/graph_agents_cli.whl"]]
    assert npx.calls == [["add", DEFAULT_SKILLS_SOURCE, "-y"]]
    assert "Authentication" not in result.output
    assert "Skills: Installed" in result.output
    assert "Scope:  workspace" in result.output


def test_setup_reports_already_installed(runner, bundle: Path, monkeypatch: pytest.MonkeyPatch):
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="already installed")

    monkeypatch.setattr(cmd_setup, "run", fake_run)
    monkeypatch.setattr(cmd_setup, "run_npx_skills", _FakeNpx(fail_first=0))
    monkeypatch.setattr(
        "graph_agents_cli.scaffold.utils.version.check_for_updates",
        lambda: (False, "0.1.0", "0.1.0"),
    )
    result = runner.invoke(setup_command, ["--workspace"])
    assert result.exit_code == 0, result.output
    assert "Already installed and up to date" in result.output


def test_setup_continues_when_cli_install_fails(
    runner, bundle: Path, project: Path, monkeypatch: pytest.MonkeyPatch
):
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 2, stdout="", stderr="no network")

    monkeypatch.setattr(cmd_setup, "run", fake_run)
    monkeypatch.setattr(cmd_setup, "run_npx_skills", _FakeNpx(fail_first=99))
    result = runner.invoke(setup_command, ["--workspace"])
    assert result.exit_code == 0, result.output
    assert "Could not install graph-agents-cli automatically" in result.output
    assert "CLI:    Not installed" in result.output
    assert "copied; npx unavailable" in result.output
    # The workspace copy lands in the temp cwd, never in the checkout.
    assert (project / ".agents" / "skills" / "graph-agents-cli-workflow" / "SKILL.md").is_file()


# ── update ───────────────────────────────────────────────────────────────────


def _versions(monkeypatch: pytest.MonkeyPatch, *, current: str, latest: str) -> None:
    monkeypatch.delenv(version_mod.INSTALL_SPEC_ENV, raising=False)
    monkeypatch.setattr(version_mod, "get_current_version", lambda: current)
    monkeypatch.setattr(version_mod, "get_latest_version", lambda: latest)


def test_update_runs_npx_update_then_installs_the_latest_release(
    runner, monkeypatch: pytest.MonkeyPatch
):
    npx_calls: list[list[str]] = []
    runs: list[list[str]] = []
    monkeypatch.setattr(cmd_update, "run_npx_skills", lambda a, m: npx_calls.append(list(a)))

    def fake_run(args, **kwargs):
        runs.append(list(args))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(cmd_update, "run", fake_run)
    _versions(monkeypatch, current="0.1.0", latest="0.2.0")
    result = runner.invoke(update_command, ["--workspace", "-y"])
    assert result.exit_code == 0, result.output
    assert npx_calls == [["update"]]
    # uv tool upgrade cannot move a git-pinned install: reinstall from the release tag.
    assert runs == [
        [
            "uv",
            "tool",
            "install",
            "--force",
            "git+https://github.com/ss7172/graph-agents-cli@v0.2.0",
        ]
    ]
    assert "Skills updated" in result.output


@pytest.mark.parametrize(
    ("current", "latest", "message"),
    [
        ("0.2.0", "0.2.0", "is up to date"),
        ("0.3.0", "0.2.0", "is up to date"),
        ("0.1.0", "0.0.0", "No graph-agents-cli release found"),
    ],
)
def test_update_leaves_the_cli_alone_without_a_newer_release(
    runner, monkeypatch: pytest.MonkeyPatch, current: str, latest: str, message: str
):
    runs: list[list[str]] = []
    monkeypatch.setattr(cmd_update, "run_npx_skills", lambda a, m: None)
    monkeypatch.setattr(cmd_update, "run", lambda args, **k: runs.append(list(args)))
    _versions(monkeypatch, current=current, latest=latest)
    result = runner.invoke(update_command, ["-y"])
    assert result.exit_code == 0, result.output
    assert runs == []
    assert message in result.output


def test_update_reinstalls_from_the_install_spec_override(runner, monkeypatch: pytest.MonkeyPatch):
    runs: list[list[str]] = []
    monkeypatch.setattr(cmd_update, "run_npx_skills", lambda a, m: None)

    def fake_run(args, **kwargs):
        runs.append(list(args))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(cmd_update, "run", fake_run)
    monkeypatch.setattr(version_mod, "get_latest_version", lambda: "0.0.0")
    monkeypatch.setenv(version_mod.INSTALL_SPEC_ENV, "git+https://mirror.example/gac@v9")
    result = runner.invoke(update_command, ["-y"])
    assert result.exit_code == 0, result.output
    assert runs == [["uv", "tool", "install", "--force", "git+https://mirror.example/gac@v9"]]


def test_update_with_a_version_placeholder_and_no_release(runner, monkeypatch: pytest.MonkeyPatch):
    runs: list[list[str]] = []
    monkeypatch.setattr(cmd_update, "run_npx_skills", lambda a, m: None)
    monkeypatch.setattr(cmd_update, "run", lambda args, **k: runs.append(list(args)))
    monkeypatch.setattr(version_mod, "get_latest_version", lambda: "0.0.0")
    monkeypatch.setenv(version_mod.INSTALL_SPEC_ENV, "git+https://mirror.example/gac@v{version}")
    result = runner.invoke(update_command, ["-y"])
    assert result.exit_code == 0, result.output
    assert runs == []
    assert "{version}" in result.output and "left as is" in result.output


def test_update_refuses_a_malformed_install_spec(runner, monkeypatch: pytest.MonkeyPatch):
    """Not a best-effort warning like an offline upgrade: a configuration error (exit 3)."""
    runs: list[list[str]] = []
    monkeypatch.setattr(cmd_update, "run_npx_skills", lambda a, m: None)
    monkeypatch.setattr(cmd_update, "run", lambda args, **k: runs.append(list(args)))
    monkeypatch.setattr(version_mod, "get_latest_version", lambda: "0.3.0")
    monkeypatch.setenv(version_mod.INSTALL_SPEC_ENV, "git+https://mirror.example/gac\nEVIL=1")
    result = runner.invoke(update_command, ["-y"])
    assert result.exit_code == 3, result.output
    assert "contains a newline" in result.output
    assert runs == []


def test_setup_refuses_a_malformed_install_spec(runner, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(version_mod.INSTALL_SPEC_ENV, "git+https://mirror.example/gac @v1")
    with pytest.raises(version_mod.InvalidInstallSpecError):
        cmd_setup._cli_install_args(dev=False)


def test_update_global_and_best_effort_upgrade(runner, monkeypatch: pytest.MonkeyPatch):
    npx_calls: list[list[str]] = []
    monkeypatch.setattr(cmd_update, "run_npx_skills", lambda a, m: npx_calls.append(list(a)))
    monkeypatch.setattr(cmd_update, "run", lambda args, **k: subprocess.CompletedProcess(args, 1))
    _versions(monkeypatch, current="0.1.0", latest="0.2.0")
    result = runner.invoke(update_command, ["-y"])
    assert result.exit_code == 0, result.output
    assert npx_calls == [["update", "-g"]]
    assert "Could not upgrade graph-agents-cli automatically" in result.output
    assert "uv tool install --force git+https://github.com/ss7172/graph-agents-cli@v0.2.0" in (
        result.output
    )
