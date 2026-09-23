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

"""Fixtures for the scaffold tests.

The engine is pointed at a scratch scaffold root that holds the
``tests/fixtures/mini_agent`` template as ``agents/mini_agent`` plus minimal
``base_templates`` and ``deployment_targets`` layers, so the tests never
depend on the real bundled templates (owned by the template engineer) and
never touch the network, a cluster, or a model key.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
from collections.abc import Callable

import pytest
import yaml
from click.testing import CliRunner, Result

from graph_agents_cli.scaffold.commands.create import create
from graph_agents_cli.scaffold.utils import template

FIXTURES_DIR = pathlib.Path(__file__).resolve().parent.parent / "fixtures"
MINI_AGENT_DIR = FIXTURES_DIR / "mini_agent"

# A representative project manifest using the supported scaffold variables.
SHARED_MANIFEST_TEMPLATE = """\
name: '{{cookiecutter.project_name}}'
cli_version: '{{cookiecutter.package_version}}'
agent_directory: '{{cookiecutter.agent_directory}}'
base_template: '{{cookiecutter.recorded_base_template}}'
generated_at: '{{cookiecutter.generated_at}}'
language: '{{cookiecutter.language}}'
create_params:
  deployment_target: '{{cookiecutter.deployment_target}}'
  runtime: '{{cookiecutter.runtime}}'
  model_provider: '{{cookiecutter.model_provider}}'
  model: '{{cookiecutter.model}}'
  checkpointer: '{{cookiecutter.checkpointer}}'
  registry: '{{cookiecutter.registry}}'
  cd: '{{cookiecutter.cd}}'
  auth_policy: '{{cookiecutter.auth_policy}}'
  auth_policy_implemented: {{ 'true' if cookiecutter.auth_policy_implemented else 'false' }}
  agent_guidance_filename: '{{cookiecutter.agent_guidance_filename}}'
{%- if cookiecutter.deployment_target == 'kubernetes' %}
environments:
  dev: { context: "", namespace: {{cookiecutter.project_name}}-dev }
  staging: { context: "", namespace: {{cookiecutter.project_name}}-staging }
  prod: { context: "", namespace: {{cookiecutter.project_name}}-prod }
{%- endif %}
secrets:
  keys: {{ cookiecutter.secret_keys }}
  owner: ""
{%- if cookiecutter.has_product_policy %}
product_api:
  policy_file: product-policy.yaml
{%- endif %}
process: {{ ("'" ~ cookiecutter.process ~ "'") if cookiecutter.process else 'null' }}
"""

ENV_EXAMPLE_TEMPLATE = """\
APP_ENV=dev
MODEL_PROVIDER={{cookiecutter.model_provider}}
MODEL_NAME={{cookiecutter.model}}
{{cookiecutter.provider_key_var}}=
CHECKPOINTER=memory
AUTH_POLICY={{cookiecutter.auth_policy}}
"""

GUIDANCE_TEMPLATE = """\
# {{cookiecutter.project_name}}

process: {{ cookiecutter.process or 'none' }}
"""


def build_scaffold_root(root: pathlib.Path) -> pathlib.Path:
    """Create a scratch scaffold root with the mini agent and minimal base layers."""
    shutil.copytree(MINI_AGENT_DIR, root / "agents" / "mini_agent")

    shared = root / "base_templates" / "_shared"
    shared.mkdir(parents=True)
    (shared / "graph-agents-cli-manifest.yaml").write_text(SHARED_MANIFEST_TEMPLATE)

    python_base = root / "base_templates" / "python"
    python_base.mkdir(parents=True)
    (python_base / ".env.example").write_text(ENV_EXAMPLE_TEMPLATE)
    (python_base / "{{cookiecutter.agent_guidance_filename}}").write_text(GUIDANCE_TEMPLATE)
    (python_base / ".gitignore").write_text(".env\n.venv/\n")

    k8s_layer = (
        root / "deployment_targets" / "kubernetes" / "python" / "deployment" / "helm" / "mini"
    )
    k8s_layer.mkdir(parents=True)
    (k8s_layer / "values-prod.yaml").write_text(
        "env:\n  APP_ENV: prod\npostgresql:\n  enabled: false\n"
    )
    (k8s_layer / "values-staging.yaml").write_text(
        "env:\n  APP_ENV: staging\npostgresql:\n  enabled: false\n"
    )
    (root / "deployment_targets" / "none" / "python").mkdir(parents=True)
    return root


@pytest.fixture
def scaffold_root(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Point the template engine at a scratch scaffold root for the test."""
    root = build_scaffold_root(tmp_path / "scaffold_root")
    monkeypatch.setattr(template, "SCAFFOLD_ROOT", root)
    return root


@pytest.fixture(autouse=True)
def no_subprocesses(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may spawn a real subprocess.

    ``git remote get-url origin`` fails (no origin), so the registry default is
    the placeholder unless a test overrides ``run_resolved`` itself.
    """
    import graph_agents_cli._runner as runner

    def fake_run_resolved(args, *, resolve_executable=True, **kwargs):
        if args[:3] == ["git", "remote", "get-url"]:
            return subprocess.CompletedProcess(args, 128, stdout="", stderr="fatal: No such remote")
        raise AssertionError(f"unexpected subprocess in test: {args}")

    monkeypatch.setattr(runner, "run_resolved", fake_run_resolved)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Backups and stamps go under a temp home, never the developer's."""
    from graph_agents_cli.scaffold.utils import backup

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(backup, "BACKUP_BASE_DIR", home / ".graph-agents-cli" / "backups")
    monkeypatch.setenv("GRAPH_AGENTS_CLI_NO_UPDATE_CHECK", "1")
    return home


CreateRunner = Callable[..., tuple[Result, pathlib.Path]]


@pytest.fixture
def run_create(scaffold_root: pathlib.Path, tmp_path: pathlib.Path) -> CreateRunner:
    """Invoke ``create`` against the mini agent; returns (result, project_path)."""

    def _run(
        *args: str, name: str = "my-agent", agent: str = "mini_agent"
    ) -> tuple[Result, pathlib.Path]:
        out_dir = tmp_path / "out"
        out_dir.mkdir(exist_ok=True)
        runner = CliRunner()
        argv = [name, "--agent", agent, "--output-dir", str(out_dir), "-y", "--skip-checks", *args]
        result = runner.invoke(create, argv, catch_exceptions=False)
        return result, out_dir / name

    return _run


def read_manifest(project: pathlib.Path) -> dict:
    with open(project / "graph-agents-cli-manifest.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)
