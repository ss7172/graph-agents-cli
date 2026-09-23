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

"""ProjectConfig parsing and defaults."""

from __future__ import annotations

import pathlib

import click
import pytest
import yaml

from graph_agents_cli._defaults import default_secret_keys
from graph_agents_cli._project import (
    MANIFEST_FILENAME,
    ProjectConfig,
    find_project_config,
    find_project_root,
    read_project_config,
    require_deployment_target,
)

FULL_MANIFEST = {
    "name": "my-agent",
    "cli_version": "0.1.0",
    "agent_directory": "app",
    "base_template": "langgraph",
    "generated_at": "2026-09-22T00:00:00+00:00",
    "language": "python",
    "create_params": {
        "deployment_target": "kubernetes",
        "runtime": "langgraph-server",
        "model_provider": "anthropic",
        "model": "claude-sonnet-5",
        "checkpointer": "postgres",
        "registry": "ghcr.io/my-org",
        "cd": "argocd",
        "auth_policy": "custom",
        "auth_policy_implemented": False,
        "agent_guidance_filename": "CLAUDE.md",
    },
    "environments": {
        "dev": {"context": "kind-dev", "namespace": "my-agent-dev"},
        "staging": {"context": "", "namespace": "my-agent-staging"},
        "prod": {"context": "prod-cluster", "namespace": "agents-prod"},
    },
    "secrets": {
        "keys": ["ANTHROPIC_API_KEY", "JUDGE_API_KEY", "DATABASE_URI", "REDIS_URI", "API_KEY"],
        "owner": "platform-team",
    },
    "api_policy": {"policy_file": "api-policy.yaml"},
    "process": "agentic-template/workflow.md",
}


def test_full_manifest_is_exposed_as_attributes() -> None:
    cfg = ProjectConfig.from_dict(FULL_MANIFEST)

    assert cfg.name == "my-agent"
    assert cfg.project_name == "my-agent"
    assert cfg.cli_version == "0.1.0"
    assert cfg.agent_directory == "app"
    assert cfg.base_template == "langgraph"
    assert cfg.generated_at == "2026-09-22T00:00:00+00:00"
    assert cfg.language == "python"
    assert cfg.create_params == FULL_MANIFEST["create_params"]
    assert cfg.deployment_target == "kubernetes"
    assert cfg.runtime == "langgraph-server"
    assert cfg.model_provider == "anthropic"
    assert cfg.model == "claude-sonnet-5"
    assert cfg.checkpointer == "postgres"
    assert cfg.registry == "ghcr.io/my-org"
    assert cfg.cd == "argocd"
    assert cfg.auth_policy == "custom"
    assert cfg.auth_policy_implemented is False
    assert cfg.agent_guidance_filename == "CLAUDE.md"
    assert cfg.environments["prod"] == {"context": "prod-cluster", "namespace": "agents-prod"}
    assert cfg.secrets.keys == FULL_MANIFEST["secrets"]["keys"]
    assert cfg.secret_keys == FULL_MANIFEST["secrets"]["keys"]
    assert cfg.secrets.owner == "platform-team"
    assert cfg.api_policy.policy_file == "api-policy.yaml"
    assert cfg.api_policy_file == "api-policy.yaml"
    assert cfg.has_legacy_product_api is False
    assert cfg.process == "agentic-template/workflow.md"
    assert cfg.provider_key_var() == "ANTHROPIC_API_KEY"
    assert cfg.environment("dev") == ("kind-dev", "my-agent-dev")
    assert cfg.environment("prod") == ("prod-cluster", "agents-prod")


def test_retired_names_are_read_with_a_warning(capsys) -> None:
    from graph_agents_cli import _defaults

    _defaults._warned_aliases.clear()
    cfg = ProjectConfig.from_dict(
        {
            "name": "old",
            "create_params": {"auth_policy": "product-session"},
            "product_api": {"policy_file": "product-policy.yaml"},
        }
    )
    assert cfg.auth_policy == "custom"
    assert cfg.auth_policy == "custom"
    # Unrecorded implemented flag: a stub policy is not implemented.
    assert cfg.auth_policy_implemented is False
    assert cfg.has_legacy_product_api is True
    assert cfg.api_policy_file is None
    assert capsys.readouterr().err.count("deprecated") == 1


@pytest.mark.parametrize(("policy", "implemented"), [("jwt", True), ("custom", False)])
def test_auth_policy_implemented_defaults(policy: str, implemented: bool) -> None:
    cfg = ProjectConfig.from_dict({"name": "x", "create_params": {"auth_policy": policy}})
    assert cfg.auth_policy == policy
    assert cfg.auth_policy_implemented is implemented


def test_minimal_manifest_defaults() -> None:
    cfg = ProjectConfig.from_dict({"name": "tiny", "cli_version": "0.1.0"})

    assert cfg.deployment_target == "none"
    assert cfg.runtime == "fastapi"
    assert cfg.model_provider == "openai"
    assert cfg.model == "gpt-5-mini"
    assert cfg.checkpointer == "memory"  # none target defaults to memory
    assert cfg.registry == ""
    assert cfg.cd == "skip"
    assert cfg.auth_policy == "shared-bearer"
    assert cfg.auth_policy_implemented is True
    assert cfg.agent_guidance_filename == "AGENTS.md"
    assert cfg.environments == {}
    assert cfg.api_policy_file is None
    assert cfg.process is None
    assert cfg.provider_key_var() == "OPENAI_API_KEY"
    # The default allow-list for openai + fastapi
    assert cfg.secret_keys == [
        "OPENAI_API_KEY",
        "JUDGE_API_KEY",
        "POSTGRES_DSN",
        "API_KEY",
        "LANGSMITH_API_KEY",
    ]


@pytest.mark.parametrize(
    ("provider", "runtime", "expected"),
    [
        (
            "openai",
            "fastapi",
            ["OPENAI_API_KEY", "JUDGE_API_KEY", "POSTGRES_DSN", "API_KEY", "LANGSMITH_API_KEY"],
        ),
        (
            "gemini",
            "langgraph-server",
            [
                "GOOGLE_API_KEY",
                "JUDGE_API_KEY",
                "DATABASE_URI",
                "REDIS_URI",
                "API_KEY",
                "LANGSMITH_API_KEY",
            ],
        ),
        (
            "openai-compatible",
            "fastapi",
            ["MODEL_API_KEY", "JUDGE_API_KEY", "POSTGRES_DSN", "API_KEY", "LANGSMITH_API_KEY"],
        ),
    ],
)
def test_secret_keys_default_follows_provider_and_runtime(
    provider: str, runtime: str, expected: list[str]
) -> None:
    cfg = ProjectConfig.from_dict(
        {
            "name": "x",
            "create_params": {"model_provider": provider, "runtime": runtime},
            "secrets": {"owner": "me"},
        }
    )
    assert cfg.secret_keys == expected == default_secret_keys(provider, runtime)
    assert cfg.secrets.owner == "me"


def test_kubernetes_target_defaults_checkpointer_to_postgres() -> None:
    cfg = ProjectConfig.from_dict(
        {"name": "x", "create_params": {"deployment_target": "kubernetes"}}
    )
    assert cfg.checkpointer == "postgres"


def test_auth_policy_implemented_defaults_from_policy() -> None:
    stub = ProjectConfig.from_dict({"name": "x", "create_params": {"auth_policy": "custom"}})
    assert stub.auth_policy_implemented is False
    flipped = ProjectConfig.from_dict(
        {
            "name": "x",
            "create_params": {"auth_policy": "custom", "auth_policy_implemented": True},
        }
    )
    assert flipped.auth_policy_implemented is True


def test_environment_fallback_and_unknown() -> None:
    cfg = ProjectConfig.from_dict(
        {"name": "svc", "create_params": {"deployment_target": "kubernetes"}}
    )
    assert cfg.environment("staging") == ("", "svc-staging")
    with pytest.raises(click.UsageError):
        cfg.environment("qa")


def test_environment_namespace_defaults_when_missing() -> None:
    cfg = ProjectConfig.from_dict(
        {"name": "svc", "environments": {"dev": {"context": "kind-kind"}, "qa": None}}
    )
    assert cfg.environment("dev") == ("kind-kind", "svc-dev")
    assert cfg.environment("qa") == ("", "svc-qa")


def test_malformed_manifest_raises() -> None:
    with pytest.raises(click.ClickException):
        ProjectConfig.from_dict(["not", "a", "mapping"])
    with pytest.raises(click.ClickException):
        ProjectConfig.from_dict({"name": "x", "create_params": "nope"})
    with pytest.raises(click.ClickException):
        ProjectConfig.from_dict({"name": "x", "environments": ["dev"]})


def test_require_deployment_target_is_usage_error() -> None:
    cfg = ProjectConfig.from_dict({"name": "x"})
    with pytest.raises(click.UsageError):
        require_deployment_target(cfg)
    require_deployment_target(
        ProjectConfig.from_dict({"name": "x", "create_params": {"deployment_target": "kubernetes"}})
    )


def test_find_project_root_walks_up_and_has_no_legacy_fallback(tmp_path: pathlib.Path) -> None:
    project = tmp_path / "proj"
    (project / "app" / "tools").mkdir(parents=True)
    (project / MANIFEST_FILENAME).write_text(yaml.safe_dump(FULL_MANIFEST))
    # A legacy [tool.graph-agents-cli] pyproject elsewhere must not count as a project.
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "pyproject.toml").write_text('[tool.graph-agents-cli]\nname = "old"\n')

    assert find_project_root(project / "app" / "tools") == project
    assert find_project_root(legacy) is None
    assert find_project_config(legacy) is None
    cfg = find_project_config(project / "app")
    assert cfg is not None and cfg.name == "my-agent"


def test_read_project_config_without_manifest_is_default(tmp_path: pathlib.Path) -> None:
    cfg = read_project_config(str(tmp_path))
    assert cfg.project_name == ""
    assert cfg.deployment_target == "none"


@pytest.mark.parametrize(
    "policy_file", ["policies/api.yaml", "other-policy.yaml", "../api-policy.yaml"]
)
def test_a_policy_file_the_agent_would_not_load_is_a_config_error(policy_file: str) -> None:
    """The agent and the Dockerfiles use only api-policy.yaml: lint must check that file."""
    with pytest.raises(click.ClickException) as exc:
        ProjectConfig.from_dict({"name": "x", "api_policy": {"policy_file": policy_file}})
    assert exc.value.exit_code == 3
    assert repr(policy_file) in exc.value.message
    assert "Rename the file to api-policy.yaml" in exc.value.message


def test_a_policy_file_spelled_with_a_leading_dot_slash_is_accepted() -> None:
    cfg = ProjectConfig.from_dict({"name": "x", "api_policy": {"policy_file": "./api-policy.yaml"}})
    assert cfg.api_policy_file == "api-policy.yaml"
