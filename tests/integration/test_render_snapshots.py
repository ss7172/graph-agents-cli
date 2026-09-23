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

import re
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


def test_fixture_directories_are_complete() -> None:
    present = {p.name for p in rf.FIXTURES_DIR.iterdir() if p.is_dir() and p.name != "_inputs"}
    assert present == set(rf.COMBINATIONS), (
        "tests/fixtures/rendered/ and scripts/regen_fixtures.py COMBINATIONS disagree; "
        "run `uv run python scripts/regen_fixtures.py`"
    )


# --- .github/agent.env as the GitHub runner reads it ---------------------------

# pr_checks.yaml loads agent.env with: grep -Ev '^[[:space:]]*(#|$)' .github/agent.env >> "$GITHUB_ENV"
_WORKFLOW_FILTER = re.compile(r"^[ \t\n\r\f\v]*(#|$)")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _workflow_filter(text: str) -> str:
    """What the pr_checks `grep -Ev` keeps."""
    return "".join(
        line for line in text.splitlines(keepends=True) if not _WORKFLOW_FILTER.match(line)
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


def test_the_workflow_filter_is_what_pr_checks_runs() -> None:
    pr_checks = (
        rf.REPO_ROOT
        / "src/graph_agents_cli/scaffold/base_templates/python/.github/workflows/pr_checks.yaml"
    ).read_text(encoding="utf-8")
    assert "grep -Ev '^[[:space:]]*(#|$)' .github/agent.env" in pr_checks


@pytest.mark.parametrize("name", list(rf.COMBINATIONS))
def test_agent_env_loads_into_github_env(rendered: dict[str, Path], name: str) -> None:
    """Every rendered agent.env, filtered as pr_checks does, parses with the runner's rules."""
    text = (rendered[name] / ".github" / "agent.env").read_text(encoding="utf-8")
    entries = _parse_github_env_file(_workflow_filter(text))
    assert all(_ENV_NAME.match(key) for key in entries), entries
    spec = entries["GRAPH_AGENTS_CLI_SPEC"]
    assert spec.startswith("git+https://github.com/ss7172/graph-agents-cli"), spec
    assert "CLI_VERSION_PIN" not in entries
    if rf.COMBINATIONS[name].manifest.get("environments", True):
        assert {"IMAGE_REPOSITORY", "RELEASE_NAME", "CHART_PATH", "RUNTIME", "CD"} <= set(entries)
    # Unfiltered, the comment line is exactly what broke pr_checks before.
    with pytest.raises(ValueError, match="Invalid format"):
        _parse_github_env_file(text)
