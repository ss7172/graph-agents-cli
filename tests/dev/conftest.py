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

"""Shared fixtures: a recorded ``run_resolved`` and a fake project config."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from graph_agents_cli import _runner, _tools


class RecordedRuns:
    """Records every subprocess the CLI would have started."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.returncodes: list[int] = []

    def __call__(self, args, *, resolve_executable=True, **kwargs):
        self.calls.append({"args": list(args), **kwargs})
        code = self.returncodes.pop(0) if self.returncodes else 0
        return subprocess.CompletedProcess(args, code, stdout="", stderr="")

    @property
    def commands(self) -> list[list[str]]:
        return [c["args"] for c in self.calls]


@pytest.fixture
def recorded_runs(monkeypatch) -> RecordedRuns:
    rec = RecordedRuns()
    monkeypatch.setattr(_runner, "run_resolved", rec)
    # Tools are never resolved against PATH in tests.
    monkeypatch.setattr(_tools, "require_tool", lambda name, install_hint="": f"/fake/bin/{name}")
    return rec


@pytest.fixture
def fake_project(monkeypatch, tmp_path: Path) -> SimpleNamespace:
    """chdir into a temp project and stub the manifest reader in every dev module."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "graph-agents-cli-manifest.yaml").write_text("name: my-agent\n")
    (tmp_path / "app").mkdir()
    cfg = SimpleNamespace(
        project_name="my-agent",
        agent_directory="app",
        runtime="fastapi",
        checkpointer="memory",
        registry="ghcr.io/acme",
        language="python",
        api_policy_file=None,
    )
    from graph_agents_cli.dev import cmd_build, cmd_install, cmd_lint, cmd_playground

    for module in (cmd_build, cmd_install, cmd_lint, cmd_playground):
        monkeypatch.setattr(module, "chdir_project_root", lambda *a, **k: None)
        if hasattr(module, "read_project_config"):
            monkeypatch.setattr(module, "read_project_config", lambda *a, **k: cfg)
        if hasattr(module, "require_agent_directory"):
            monkeypatch.setattr(module, "require_agent_directory", lambda cfg: None)
    return SimpleNamespace(root=tmp_path, cfg=cfg)
