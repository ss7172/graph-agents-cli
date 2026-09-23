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
"""Fixtures: a recording fake for run_resolved/require_tool and a scaffolded temp project.

No test here needs a network, a cluster, or a model key.
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from graph_agents_cli import _project, _runner, _tools


@dataclass
class FakeRunner:
    """Records every command and answers from a list of (pattern, rc, stdout, stderr)."""

    calls: list[tuple[list[str], dict[str, Any]]] = field(default_factory=list)
    responses: list[tuple[Callable[[str], bool], int, str, str]] = field(default_factory=list)
    missing_tools: set[str] = field(default_factory=set)
    env_file_contents: list[str] = field(default_factory=list)

    def respond(
        self,
        pattern: str | Callable[[str], bool],
        *,
        rc: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        match = pattern if callable(pattern) else (lambda joined, p=pattern: p in joined)
        # Later registrations win so tests can override defaults.
        self.responses.insert(0, (match, rc, stdout, stderr))

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        args = list(args)
        self.calls.append((args, kwargs))
        if args and args[0] in self.missing_tools:
            raise _tools.ToolNotFoundError(f"'{args[0]}' is not installed or not on PATH.")
        for arg in args:
            if arg.startswith("--from-env-file="):
                path = Path(arg.split("=", 1)[1])
                if path.is_file():
                    self.env_file_contents.append(path.read_text())
        joined = shlex.join(args)
        for match, rc, out, err in self.responses:
            if match(joined):
                return subprocess.CompletedProcess(args, rc, out, err)
        return subprocess.CompletedProcess(args, 0, "", "")

    def require_tool(self, name: str, install_hint: str = "") -> str:
        if name in self.missing_tools:
            raise _tools.ToolNotFoundError(f"'{name}' is not installed or not on PATH.")
        return name

    @property
    def joined(self) -> list[str]:
        return [shlex.join(c) for c, _ in self.calls]

    def find(self, prefix: str) -> list[str]:
        return [j for j in self.joined if j.startswith(prefix)]

    def any(self, fragment: str) -> bool:
        return any(fragment in j for j in self.joined)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    runner = FakeRunner()
    monkeypatch.setattr(_runner, "run_resolved", runner)
    monkeypatch.setattr(_tools, "require_tool", runner.require_tool)
    return runner


DEFAULT_KEYS = ["OPENAI_API_KEY", "JUDGE_API_KEY", "POSTGRES_DSN", "API_KEY", "LANGSMITH_API_KEY"]


def make_cfg(**overrides: Any) -> SimpleNamespace:
    create_params = {
        "deployment_target": "kubernetes",
        "runtime": "fastapi",
        "model_provider": "openai",
        "model": "gpt-5-mini",
        "checkpointer": "postgres",
        "registry": "ghcr.io/my-org",
        "cd": "skip",
        "auth_policy": "shared-bearer",
        "auth_policy_implemented": True,
        "agent_guidance_filename": "GEMINI.md",
    }
    create_params.update(overrides.pop("create_params", {}))
    cfg = SimpleNamespace(
        project_name="my-agent",
        deployment_target=create_params["deployment_target"],
        runtime=create_params["runtime"],
        model_provider=create_params["model_provider"],
        registry=create_params["registry"],
        auth_policy=create_params["auth_policy"],
        auth_policy_implemented=create_params["auth_policy_implemented"],
        create_params=create_params,
        environments={
            "dev": {"context": "kind-dev", "namespace": "my-agent-dev"},
            "staging": {"context": "staging-cluster", "namespace": "my-agent-staging"},
            "prod": {"context": "prod-cluster", "namespace": "my-agent-prod"},
        },
        secret_keys=list(DEFAULT_KEYS),
        secrets={"keys": list(DEFAULT_KEYS), "owner": "platform-team"},
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


VALUES_DEV = """# dev overrides
env:
  APP_ENV: dev   # playground on
image:
  # tag is written by deploy
  tag: "old-tag"  # keep me
postgresql:
  enabled: true
gateway:
  enabled: false
"""

VALUES = """image:
  repository: ghcr.io/my-org/my-agent
  tag: latest
  pullPolicy: IfNotPresent
imagePullSecrets: []
gateway:
  enabled: true
  className: ""
  parentRef:
    name: gw   # the scaffolded staging/prod values leave this blank; deploy refuses that (exit 3)
ingress:
  enabled: false
tls:
  certManager:
    enabled: false
hpa:
  enabled: false
tracing:
  enabled: false
  otlpEndpoint: ""
env:
  APP_ENV: prod
  MODEL_PROVIDER: openai
"""


@pytest.fixture
def cfg_factory() -> Callable[..., SimpleNamespace]:
    return make_cfg


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A scaffolded-looking project in a temp dir; ``cfg`` is what read_project_config returns."""
    (tmp_path / "graph-agents-cli-manifest.yaml").write_text("name: my-agent\n")
    chart = tmp_path / "deployment" / "helm" / "my-agent"
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: my-agent\nversion: 0.1.0\n")
    (chart / "values.yaml").write_text(VALUES)
    (chart / "values-dev.yaml").write_text(VALUES_DEV)
    (chart / "values-staging.yaml").write_text(
        "image:\n  tag: staging-old\npostgresql:\n  enabled: false\n"
    )
    (chart / "values-prod.yaml").write_text(
        "image:\n  tag: prod-old\npostgresql:\n  enabled: false\n"
    )
    (tmp_path / "Dockerfile").write_text("FROM python:3.12\n")
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=sk-test\nPOSTGRES_DSN='postgresql://u:p@db/agent'\nNOT_ALLOWED=leak\nJUDGE_API_KEY=\n"
    )
    monkeypatch.chdir(tmp_path)
    cfg = make_cfg()
    monkeypatch.setattr(_project, "read_project_config", lambda *a, **k: cfg)
    return SimpleNamespace(root=tmp_path, chart=chart, cfg=cfg)
