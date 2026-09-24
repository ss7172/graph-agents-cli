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

"""Rendered-project snapshots: the real `create` against the bundled template.

Not slow and needs no network: every combination in
``scripts/regen_fixtures.py`` is rendered in process with ``--skip-deps`` and
its file list and manifest are compared to ``tests/fixtures/rendered/<combo>/``.
On a deliberate template change, run ``uv run python scripts/regen_fixtures.py``
and review the fixture diff.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import regen_fixtures as rf
import yaml


@pytest.fixture(scope="module")
def rendered(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("rendered")
    return {name: rf.render_combination(name, root / name) for name in rf.COMBINATIONS}


@pytest.mark.parametrize("name", list(rf.COMBINATIONS))
def test_file_list_matches_fixture(rendered: dict[str, Path], name: str) -> None:
    expected_files, _ = rf.read_fixture(name)
    actual_files = rf.list_files(rendered[name])
    missing = sorted(set(expected_files) - set(actual_files))
    extra = sorted(set(actual_files) - set(expected_files))
    assert not missing and not extra, (
        f"{name}: rendered file list differs from tests/fixtures/rendered/{name}/files.json\n"
        f"  missing: {missing}\n  extra: {extra}\n"
        "  (run `uv run python scripts/regen_fixtures.py` after a deliberate template change)"
    )


@pytest.mark.parametrize("name", list(rf.COMBINATIONS))
def test_manifest_matches_fixture(rendered: dict[str, Path], name: str) -> None:
    _, expected_manifest = rf.read_fixture(name)
    actual_manifest = rf.normalized_manifest(rendered[name])
    assert actual_manifest == expected_manifest, (
        f"{name}: rendered manifest differs from tests/fixtures/rendered/{name}/manifest.yaml"
    )
    assert rf.GENERATED_AT_PLACEHOLDER in actual_manifest


@pytest.mark.parametrize("name", list(rf.COMBINATIONS))
def test_layout_follows_contracts(rendered: dict[str, Path], name: str) -> None:
    """The manifest and conditional-file rules, independent of the exact fixture content."""
    combo = rf.COMBINATIONS[name]
    project = rendered[name]
    for rel in combo.expect_present:
        assert (project / rel).exists(), f"{name}: expected {rel}"
    for rel in combo.expect_absent:
        assert not (project / rel).exists(), f"{name}: did not expect {rel}"

    manifest = yaml.safe_load((project / rf.MANIFEST_FILENAME).read_text(encoding="utf-8"))
    params = manifest["create_params"]
    assert manifest["name"] == combo.project_name
    for key, value in combo.manifest.items():
        if key == "environments":
            assert ("environments" in manifest) is value, f"{name}: environments block"
            if value:
                assert set(manifest["environments"]) == {"dev", "staging", "prod"}
                for env, block in manifest["environments"].items():
                    assert block["namespace"] == f"{combo.project_name}-{env}"
        elif key == "api_policy":
            assert ("api_policy" in manifest) is value, f"{name}: api_policy block"
            if value:
                assert manifest["api_policy"]["policy_file"] == "api-policy.yaml"
        elif key == "secret_keys":
            assert manifest["secrets"]["keys"] == value, f"{name}: secrets.keys"
        elif key == "process":
            assert manifest.get("process") == value
        else:
            assert params[key] == value, f"{name}: create_params.{key}"
    assert "process" in manifest
    assert manifest["secrets"]["keys"], f"{name}: secrets.keys must not be empty"

    # Runtime files: exactly one lock, the runtime's Dockerfile.
    dockerfile = (project / "Dockerfile").read_text(encoding="utf-8")
    if params["runtime"] == "langgraph-server":
        assert "langgraph-api" in dockerfile
    else:
        assert "langgraph-api" not in dockerfile.split("\n")[0]
    assert (project / "uv.lock").is_file()
    lock_head = (project / "uv.lock").read_text(encoding="utf-8")
    assert "{{cookiecutter" not in lock_head
    assert f'name = "{combo.project_name}"' in lock_head


@pytest.mark.parametrize("name", list(rf.COMBINATIONS))
def test_the_manifest_records_the_build_and_the_snapshot_digest(
    rendered: dict[str, Path], name: str, tmp_path: Path
) -> None:
    """``cli_build`` names this build; its digest is what `scaffold upgrade` re-renders.

    The fixture manifests leave the block out (it changes with every commit),
    so it is checked here: the digest of a fresh render from the manifest's
    settings (the snapshot `scaffold upgrade` compares) equals the recorded one,
    except for a project seeded with --api-policy, whose render is more than
    that snapshot and records no digest.
    """
    from graph_agents_cli._project import read_project_config
    from graph_agents_cli.scaffold.utils import build_record
    from graph_agents_cli.scaffold.utils.generation_metadata import metadata_to_cli_args
    from graph_agents_cli.scaffold.utils.merge import run_create_command

    project = rendered[name]
    text = (project / rf.MANIFEST_FILENAME).read_text(encoding="utf-8")
    manifest = yaml.safe_load(text)
    running = build_record.running_build()
    assert manifest["cli_build"]["id"] == running.id
    assert manifest["cli_build"]["commit"] == running.commit
    # Right after cli_version, with its comment, and the other comments kept.
    assert "cli_version: '" in text and "\n# The build that rendered this project" in text
    assert text.index("cli_version:") < text.index("cli_build:") < text.index("agent_directory:")
    assert text.startswith("# Project manifest written by `graph-agents-cli create`.")

    config = read_project_config(str(project))
    assert run_create_command(metadata_to_cli_args(config), tmp_path, config.project_name)
    replayed = build_record.template_digest(tmp_path / config.project_name)
    if "--api-policy" in rf.COMBINATIONS[name].args:
        assert manifest["cli_build"]["template_digest"] is None
    else:
        assert manifest["cli_build"]["template_digest"] == replayed
        assert build_record.template_digest(project) == replayed


def test_fixture_directories_are_complete() -> None:
    present = {p.name for p in rf.FIXTURES_DIR.iterdir() if p.is_dir() and p.name != "_inputs"}
    assert present == set(rf.COMBINATIONS), (
        "tests/fixtures/rendered/ and scripts/regen_fixtures.py COMBINATIONS disagree; "
        "run `uv run python scripts/regen_fixtures.py`"
    )


# --- .github/agent.env as the GitHub workflows read it -------------------------

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BASH = shutil.which("bash")
_WORKFLOWS = rf.REPO_ROOT / "src/graph_agents_cli/scaffold"
_LOADER_STEPS = (
    (_WORKFLOWS / "base_templates/python/.github/workflows/pr_checks.yaml", "checks"),
    (_WORKFLOWS / "deployment_targets/kubernetes/python/.github/workflows/staging.yaml", "build"),
    (
        _WORKFLOWS / "deployment_targets/kubernetes/python/.github/workflows/promote-to-prod.yaml",
        "settings",
    ),
)


def _parse_github_env_file(text: str) -> dict[str, str]:
    """Port of the runner's GITHUB_ENV file parser (actions/runner, FileCommandManager).

    Each non-empty line is ``NAME=VALUE`` or opens a ``NAME<<DELIMITER`` heredoc;
    anything else fails the step with ``Invalid format '<line>'``.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    entries: dict[str, str] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if line == "":
            continue
        equals, heredoc = line.find("="), line.find("<<")
        if equals >= 0 and (heredoc < 0 or equals < heredoc):
            name, value = line.split("=", 1)
            if not name:
                raise ValueError(f"Invalid format '{line}'. Name must not be empty")
            entries[name] = value
        elif heredoc >= 0 and (equals < 0 or heredoc < equals):
            name, delimiter = line.split("<<", 1)
            body: list[str] = []
            while index < len(lines) and lines[index] != delimiter:
                body.append(lines[index])
                index += 1
            if index >= len(lines):
                raise ValueError(f"Invalid value. Matching delimiter not found '{delimiter}'")
            index += 1
            entries[name] = "\n".join(body)
        else:
            raise ValueError(f"Invalid format '{line}'")
    return entries


def _loader_script(workflow: Path, job: str) -> str:
    """The `Load project settings` step of ``job`` (every workflow reads agent.env with it)."""
    data = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    steps = [s for s in data["jobs"][job]["steps"] if s.get("name") == "Load project settings"]
    assert len(steps) == 1, workflow.name
    return steps[0]["run"]


def _load_like_the_workflows(project: Path, tmp_path: Path) -> dict[str, str]:
    """Run every workflow's agent.env loader on ``project`` as the runner does; GITHUB_ENV parsed."""
    results = []
    for workflow, job in _LOADER_STEPS:
        step = tmp_path / f"{workflow.stem}-load.sh"
        step.write_text(_loader_script(workflow, job), encoding="utf-8")
        github_env = tmp_path / f"{workflow.stem}-github-env"
        github_env.write_text("", encoding="utf-8")
        proc = subprocess.run(
            [_BASH or "bash", "--noprofile", "--norc", "-eo", "pipefail", str(step)],
            cwd=project,
            env={"PATH": os.environ.get("PATH", ""), "GITHUB_ENV": str(github_env)},
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"{workflow.name}: {proc.stderr}"
        results.append(_parse_github_env_file(github_env.read_text(encoding="utf-8")))
    assert all(r == results[0] for r in results), "the workflows load agent.env differently"
    return results[0]


@pytest.mark.skipif(_BASH is None, reason="bash is not on PATH")
@pytest.mark.parametrize("name", list(rf.COMBINATIONS))
def test_agent_env_loads_into_github_env(
    rendered: dict[str, Path], name: str, tmp_path: Path
) -> None:
    """Every rendered agent.env, read by the workflows' loader, parses with the runner's rules."""
    entries = _load_like_the_workflows(rendered[name], tmp_path)
    assert all(_ENV_NAME.match(key) for key in entries), entries
    spec = entries["GRAPH_AGENTS_CLI_SPEC"]
    assert spec.startswith("git+https://github.com/ss7172/graph-agents-cli"), spec
    assert "CLI_VERSION_PIN" not in entries
    if rf.COMBINATIONS[name].manifest.get("environments", True):
        assert {"IMAGE_REPOSITORY", "RELEASE_NAME", "CHART_PATH", "RUNTIME", "CD"} <= set(entries)
    # The raw file (with its comment lines) is not valid GITHUB_ENV input: the
    # workflows must go through the loader, never append the file as is.
    raw = (rendered[name] / ".github" / "agent.env").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid format"):
        _parse_github_env_file(raw)


# A mirror given as a PEP 508 requirement: spaces and `@` in the value, which a
# shell `source` of agent.env would have run as a command.
_PEP508_OVERRIDE = (
    "graph-agents-cli @ git+https://git.example.com/mirror/graph-agents-cli@v{version}"
)


@pytest.mark.skipif(_BASH is None, reason="bash is not on PATH")
@pytest.mark.parametrize("name", ["server-helm-push", "fastapi-argocd-custom", "fastapi-none-jwt"])
def test_an_install_spec_override_reaches_every_workflow_intact(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from graph_agents_cli.__init__ import __version__
    from graph_agents_cli.main import main

    monkeypatch.setenv("GRAPH_AGENTS_CLI_INSTALL_SPEC", _PEP508_OVERRIDE)
    result = CliRunner().invoke(
        main,
        rf.create_args(name, tmp_path / "out"),
        env={"GRAPH_AGENTS_CLI_NO_UPDATE_CHECK": "1"},
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    project = tmp_path / "out" / rf.COMBINATIONS[name].project_name
    entries = _load_like_the_workflows(project, tmp_path)
    assert entries["GRAPH_AGENTS_CLI_SPEC"] == _PEP508_OVERRIDE.replace("{version}", __version__)
