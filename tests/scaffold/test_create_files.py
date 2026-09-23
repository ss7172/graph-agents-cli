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

"""Rendered files of ``create``: conditional files, runtime selection, manifest, policy."""

from __future__ import annotations

import pathlib
import subprocess

import pytest

from graph_agents_cli._defaults import DEFAULT_REGISTRY_PLACEHOLDER, default_secret_keys
from graph_agents_cli._project import read_project_config
from graph_agents_cli.scaffold.commands import create as create_module
from graph_agents_cli.scaffold.utils.template import CONDITIONAL_FILES, COPY_WITHOUT_RENDER

from .conftest import MINI_AGENT_DIR, CreateRunner, read_manifest


def _exists(project: pathlib.Path, rel: str) -> bool:
    return (project / rel).exists()


@pytest.mark.parametrize("cd", ["skip", "argocd", "helm-push"])
def test_conditional_files_per_cd_on_kubernetes(run_create: CreateRunner, cd: str) -> None:
    result, project = run_create("--deployment-target", "kubernetes", "--cd", cd)
    assert result.exit_code == 0, result.output

    # pr_checks always; staging/promote/CODEOWNERS only with a CD mode
    assert _exists(project, ".github/workflows/pr_checks.yaml")
    assert _exists(project, ".github/workflows/staging.yaml") == (cd != "skip")
    assert _exists(project, ".github/workflows/promote-to-prod.yaml") == (cd != "skip")
    assert _exists(project, ".github/CODEOWNERS") == (cd != "skip")
    # argocd manifests only with argocd
    assert _exists(project, "deployment/argocd/application-dev.yaml") == (cd == "argocd")
    # the chart always ships for kubernetes, including the target layer's values files
    assert _exists(project, "deployment/helm/mini/Chart.yaml")
    assert _exists(project, "deployment/helm/mini/values-dev.yaml")
    assert _exists(project, "deployment/helm/mini/values-prod.yaml")
    assert _exists(project, "deployment/helm/mini/values-staging.yaml")
    # nothing conditional leaks as unused_*
    assert not list(project.rglob("unused_*"))
    assert read_manifest(project)["create_params"]["cd"] == cd


def test_conditional_files_for_target_none(run_create: CreateRunner) -> None:
    result, project = run_create("--deployment-target", "none")
    assert result.exit_code == 0, result.output
    assert not _exists(project, "deployment")
    assert _exists(project, ".github/workflows/pr_checks.yaml")
    assert not _exists(project, ".github/workflows/staging.yaml")
    assert not _exists(project, ".github/CODEOWNERS")
    assert not _exists(project, "product-policy.yaml")
    manifest = read_manifest(project)
    assert "environments" not in manifest
    assert manifest["create_params"]["registry"] == ""


def test_conditional_files_table_matches_contract() -> None:
    assert set(CONDITIONAL_FILES) == {
        ".github/workflows/staging.yaml",
        ".github/workflows/promote-to-prod.yaml",
        ".github/CODEOWNERS",
        "deployment/argocd",
        "deployment",
        "product-policy.yaml",
    }
    for pattern in (
        "deployment/helm/*/templates/*",
        "deployment/helm/*/templates/**/*",
        "*.tpl",
        ".github/workflows/*",
        "*.lock",
        "*.ipynb",
        "node_modules/**",
        ".venv/**",
        "__pycache__/**",
        ".git/*",
    ):
        assert pattern in COPY_WITHOUT_RENDER


@pytest.mark.parametrize("runtime", ["fastapi", "langgraph-server"])
def test_runtime_selects_dockerfile_and_lock(run_create: CreateRunner, runtime: str) -> None:
    result, project = run_create("--runtime", runtime, "--checkpointer", "postgres")
    assert result.exit_code == 0, result.output

    dockerfile = (project / "Dockerfile").read_text()
    assert f"# {runtime} dockerfile" in dockerfile
    assert not _exists(project, "Dockerfile.langgraph-server")

    lock = (project / "uv.lock").read_text()
    assert f"# bundled lock: {runtime}" in lock
    assert 'name = "my-agent"' in lock
    assert "{{cookiecutter.project_name}}" not in lock
    assert not _exists(project, "uv-fastapi.lock")
    assert not _exists(project, "uv-langgraph-server.lock")

    rendered = (project / "app" / "fast_api_app.py").read_text()
    assert f'RUNTIME = "{runtime}"' in rendered


def test_missing_bundled_lock_warns_and_continues(
    run_create: CreateRunner, scaffold_root: pathlib.Path
) -> None:
    agent = scaffold_root / "agents" / "mini_agent"
    (agent / "uv-fastapi.lock").unlink()
    (agent / "uv-langgraph-server.lock").unlink()
    result, project = run_create()
    assert result.exit_code == 0, result.output
    assert "No bundled lock" in result.output
    assert not _exists(project, "uv.lock")


def test_helm_templates_and_workflows_are_copied_verbatim(run_create: CreateRunner) -> None:
    result, project = run_create("--cd", "argocd")
    assert result.exit_code == 0, result.output

    for rel in (
        "deployment/helm/mini/templates/deployment.yaml",
        "deployment/helm/mini/templates/_helpers.tpl",
        ".github/workflows/pr_checks.yaml",
        ".github/workflows/staging.yaml",
        ".github/workflows/promote-to-prod.yaml",
    ):
        assert (project / rel).read_bytes() == (MINI_AGENT_DIR / rel).read_bytes(), rel

    deployment = (project / "deployment/helm/mini/templates/deployment.yaml").read_text()
    assert '{{ include "mini.fullname" . }}' in deployment
    assert "{{- include" in deployment
    staging = (project / ".github/workflows/staging.yaml").read_text()
    assert "${{ secrets.GITHUB_TOKEN }}" in staging
    assert "${{ github.sha }}" in staging

    # Files outside the verbatim set ARE rendered.
    assert (
        (project / "deployment/helm/mini/Chart.yaml")
        .read_text()
        .startswith("apiVersion: v2\nname: my-agent\n")
    )
    assert (
        "ghcr.io/CHANGE-ME/my-agent" in (project / "deployment/helm/mini/values.yaml").read_text()
    )
    assert "name: my-agent-dev" in (project / "deployment/argocd/application-dev.yaml").read_text()
    assert "Mini agent for my-agent" in (project / "app" / "agent.py").read_text()


def test_manifest_content_kubernetes(run_create: CreateRunner) -> None:
    result, project = run_create(
        "--runtime",
        "langgraph-server",
        "--model-provider",
        "gemini",
        "--model",
        "gemini-x",
        "--registry",
        "registry.example.com/team",
        "--cd",
        "helm-push",
        "--auth-policy",
        "product-session",
        "--process",
        "docs/workflow.md",
        "--agent-guidance-filename",
        "CLAUDE.md",
    )
    assert result.exit_code == 0, result.output
    manifest = read_manifest(project)

    assert manifest["name"] == "my-agent"
    assert manifest["cli_version"]
    assert manifest["agent_directory"] == "app"
    assert manifest["base_template"] == "mini_agent"
    assert manifest["generated_at"]
    assert manifest["language"] == "python"
    assert manifest["create_params"] == {
        "deployment_target": "kubernetes",
        "runtime": "langgraph-server",
        "model_provider": "gemini",
        "model": "gemini-x",
        "checkpointer": "postgres",
        "registry": "registry.example.com/team",
        "cd": "helm-push",
        "auth_policy": "product-session",
        "auth_policy_implemented": False,
        "agent_guidance_filename": "CLAUDE.md",
    }
    assert manifest["environments"] == {
        "dev": {"context": "", "namespace": "my-agent-dev"},
        "staging": {"context": "", "namespace": "my-agent-staging"},
        "prod": {"context": "", "namespace": "my-agent-prod"},
    }
    assert manifest["secrets"] == {
        "keys": default_secret_keys("gemini", "langgraph-server"),
        "owner": "",
    }
    assert manifest["secrets"]["keys"][0] == "GOOGLE_API_KEY"
    assert "DATABASE_URI" in manifest["secrets"]["keys"]
    assert "product_api" not in manifest
    assert manifest["process"] == "docs/workflow.md"

    # The typed reader agrees with the file.
    cfg = read_project_config(str(project))
    assert cfg.runtime == "langgraph-server"
    assert cfg.auth_policy_implemented is False
    assert cfg.environment("dev") == ("", "my-agent-dev")
    assert cfg.process == "docs/workflow.md"

    # Guidance file and env example are rendered with the new variables.
    assert (project / "CLAUDE.md").read_text() == "# my-agent\n\nprocess: docs/workflow.md\n"
    assert not _exists(project, "GEMINI.md")
    env_example = (project / ".env.example").read_text()
    assert "MODEL_PROVIDER=gemini" in env_example
    assert "GOOGLE_API_KEY=" in env_example

    rendered = (project / "app" / "fast_api_app.py").read_text()
    assert 'PROVIDER_KEY_VAR = "GOOGLE_API_KEY"' in rendered
    assert 'DEFAULT_JUDGE_MODEL = "gemini-x"' in rendered
    assert "SECRET_KEYS = ['GOOGLE_API_KEY'" in rendered
    assert "HAS_PRODUCT_POLICY = False" in rendered
    assert "TAGS = ['langgraph']" in rendered
    assert 'RECORDED_BASE_TEMPLATE = "mini_agent"' in rendered


def test_process_defaults_to_null(run_create: CreateRunner) -> None:
    result, project = run_create()
    assert result.exit_code == 0, result.output
    manifest = read_manifest(project)
    assert "process" in manifest and manifest["process"] is None
    assert (project / "GEMINI.md").read_text() == "# my-agent\n\nprocess: none\n"


def test_product_policy_is_seeded(run_create: CreateRunner, tmp_path: pathlib.Path) -> None:
    policy = tmp_path / "policy.yaml"
    policy.write_text("product_api:\n  auth: bearer\n  allowed_methods: [GET]\n")
    result, project = run_create("--product-policy", str(policy))
    assert result.exit_code == 0, result.output
    assert (project / "product-policy.yaml").read_text() == policy.read_text()
    manifest = read_manifest(project)
    assert manifest["product_api"] == {"policy_file": "product-policy.yaml"}
    assert read_project_config(str(project)).product_policy_file == "product-policy.yaml"
    assert "HAS_PRODUCT_POLICY = True" in (project / "app" / "fast_api_app.py").read_text()
    # A bearer policy's token joins secrets.keys (Section 7 item 21, CONTRACTS section 4).
    assert manifest["secrets"]["keys"] == [
        *default_secret_keys("openai", "fastapi"),
        "PRODUCT_API_TOKEN",
    ]
    assert "PRODUCT_API_TOKEN" in read_project_config(str(project)).secret_keys


@pytest.mark.parametrize(
    ("policy_text", "expected_extra"),
    [
        ("product_api:\n  auth: bearer\n  token_env: MY_TOKEN\n", ["MY_TOKEN"]),
        ("auth: bearer\n", ["PRODUCT_API_TOKEN"]),  # unwrapped shape
        ("product_api:\n  auth: forwarded-session\n", []),
        ("product_api:\n  allowed_methods: [GET]\n", []),
    ],
)
def test_secret_keys_follow_the_policy_token_env(
    run_create: CreateRunner, tmp_path: pathlib.Path, policy_text: str, expected_extra: list[str]
) -> None:
    policy = tmp_path / "policy.yaml"
    policy.write_text(policy_text)
    result, project = run_create("--product-policy", str(policy))
    assert result.exit_code == 0, result.output
    assert read_manifest(project)["secrets"]["keys"] == [
        *default_secret_keys("openai", "fastapi"),
        *expected_extra,
    ]


def test_product_policy_must_exist(run_create: CreateRunner, tmp_path: pathlib.Path) -> None:
    result, project = run_create("--product-policy", str(tmp_path / "missing.yaml"))
    assert result.exit_code != 0
    assert not project.exists()


def test_template_shipped_policy_is_dropped_without_flag(
    run_create: CreateRunner, scaffold_root: pathlib.Path
) -> None:
    (scaffold_root / "agents" / "mini_agent" / "product-policy.yaml").write_text(
        "product_api: {}\n"
    )
    result, project = run_create()
    assert result.exit_code == 0, result.output
    assert not _exists(project, "product-policy.yaml")
    assert "product_api" not in read_manifest(project)


def test_registry_defaults_to_placeholder_without_git_remote(run_create: CreateRunner) -> None:
    result, project = run_create("--deployment-target", "kubernetes")
    assert result.exit_code == 0, result.output
    assert read_manifest(project)["create_params"]["registry"] == DEFAULT_REGISTRY_PLACEHOLDER
    assert "no git origin remote found" in result.output


def test_registry_defaults_to_git_origin_owner(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    import graph_agents_cli._runner as runner

    def fake_run_resolved(args, *, resolve_executable=True, **kwargs):
        assert args[:3] == ["git", "remote", "get-url"]
        return subprocess.CompletedProcess(
            args, 0, stdout="git@github.com:Acme-Org/repo.git\n", stderr=""
        )

    monkeypatch.setattr(runner, "run_resolved", fake_run_resolved)
    result, project = run_create("--deployment-target", "kubernetes")
    assert result.exit_code == 0, result.output
    assert read_manifest(project)["create_params"]["registry"] == "ghcr.io/acme-org"


@pytest.mark.parametrize(
    ("url", "owner"),
    [
        ("git@github.com:Acme-Org/repo.git", "acme-org"),
        ("https://github.com/acme/repo", "acme"),
        ("https://github.com/acme/repo.git", "acme"),
        ("ssh://git@github.example.com:2222/Team/repo.git", "team"),
        ("https://gitlab.com/group/subgroup/repo.git", "group"),
        ("not a url", None),
        ("https://github.com/only-owner", None),
    ],
)
def test_parse_git_remote_owner(url: str, owner: str | None) -> None:
    assert create_module.parse_git_remote_owner(url) == owner


def test_agent_directory_override(run_create: CreateRunner) -> None:
    result, project = run_create("--agent-directory", "bot")
    assert result.exit_code == 0, result.output
    assert (project / "bot" / "agent.py").is_file()
    assert not (project / "app").exists()
    assert read_manifest(project)["agent_directory"] == "bot"
    assert 'packages = ["bot"]' in (project / "pyproject.toml").read_text()


def test_invalid_agent_directory_is_refused(run_create: CreateRunner) -> None:
    result, _project = run_create("--agent-directory", "my-bot")
    assert result.exit_code != 0
    assert "hyphens" in result.output


def test_local_at_spec_renders_on_bundled_base(
    run_create: CreateRunner, tmp_path: pathlib.Path
) -> None:
    import shutil

    local = tmp_path / "local_template"
    shutil.copytree(MINI_AGENT_DIR, local)
    (local / "app" / "extra_tool.py").write_text("TOOL = 'local'\n")
    result, project = run_create("--cd", "argocd", agent=f"local@{local}")
    assert result.exit_code == 0, result.output
    assert (project / "app" / "extra_tool.py").is_file()
    assert (project / "deployment" / "argocd" / "application-dev.yaml").is_file()
    manifest = read_manifest(project)
    assert manifest["base_template"] == f"local@{local}"
    assert (project / "uv.lock").read_text().startswith("version = 1")


def test_next_steps_banner(run_create: CreateRunner) -> None:
    result, _ = run_create("--deployment-target", "kubernetes")
    assert result.exit_code == 0, result.output
    for step in (
        "graph-agents-cli install",
        "graph-agents-cli playground",
        "graph-agents-cli eval run",
        "graph-agents-cli deploy --env dev",
    ):
        assert step in result.output
    result, _ = run_create("--deployment-target", "none", name="local-only")
    assert "deploy --env dev" not in result.output
    assert "scaffold enhance --deployment-target kubernetes" in result.output
