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

"""Shared fixtures for the setup / login / skills-check tests.

No network, no cluster, no model key: HOME is redirected to a temp dir, the
update check is disabled, and every env variable the checks read is cleared.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

_ENV_KEYS = (
    "MODEL_PROVIDER",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "MODEL_API_KEY",
    "OPENAI_BASE_URL",
    "JUDGE_MODEL_PROVIDER",
    "JUDGE_API_KEY",
    "JUDGE_BASE_URL",
    "TRACING_ENABLED",
    "LANGSMITH_API_KEY",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "GITHUB_ACTIONS",
    "CI",
    "BUILD_ID",
    "GITLAB_CI",
    "GRAPH_AGENTS_CLI_NO_UPDATE_CHECK",
    "API_KEY",
    "AUTH_POLICY",
)


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    # Never touch the real terminal width / colour handling.
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
    return home


@pytest.fixture
def home(_isolated_env: Path) -> Path:
    return _isolated_env


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty directory used as cwd, with no manifest (outside a project)."""
    root = tmp_path / "work"
    root.mkdir()
    monkeypatch.chdir(root)
    return root


def write_manifest(root: Path, **create_params: object) -> Path:
    """Write a minimal graph-agents-cli-manifest.yaml with the given create_params."""
    import yaml

    params = {
        "deployment_target": "kubernetes",
        "runtime": "fastapi",
        "model_provider": "openai",
        "cd": "skip",
    }
    params.update(create_params)
    data = {
        "name": root.name,
        "cli_version": "0.1.0",
        "agent_directory": "app",
        "create_params": params,
        "environments": {
            "dev": {"context": "kind-dev", "namespace": f"{root.name}-dev"},
        },
    }
    path = root / "graph-agents-cli-manifest.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def env_without(*keys: str) -> dict[str, str]:
    """Copy of os.environ without ``keys`` (for subprocess-free env fixtures)."""
    return {k: v for k, v in os.environ.items() if k not in keys}
