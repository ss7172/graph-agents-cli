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

"""Manifest -> create flags round trip (used by upgrade and enhance)."""

from __future__ import annotations

from graph_agents_cli._project import ProjectConfig, read_project_config
from graph_agents_cli.scaffold.utils.generation_metadata import metadata_to_cli_args

from .conftest import CreateRunner, read_manifest


def _pairs(args: list[str]) -> dict[str, str]:
    return {args[i]: args[i + 1] for i in range(0, len(args), 2)}


def test_metadata_to_cli_args_maps_every_create_param() -> None:
    cfg = ProjectConfig.from_dict(
        {
            "name": "svc",
            "agent_directory": "bot",
            "base_template": "langgraph",
            "create_params": {
                "deployment_target": "kubernetes",
                "runtime": "langgraph-server",
                "model_provider": "anthropic",
                "model": "claude-sonnet-5",
                "checkpointer": "postgres",
                "registry": "ghcr.io/org",
                "cd": "argocd",
                "auth_policy": "product-session",
                "agent_guidance_filename": "AGENTS.md",
            },
            "process": "docs/process.md",
        }
    )
    args = metadata_to_cli_args(cfg)
    assert _pairs(args) == {
        "--agent": "langgraph",
        "--agent-directory": "bot",
        "--agent-guidance-filename": "AGENTS.md",
        "--deployment-target": "kubernetes",
        "--runtime": "langgraph-server",
        "--model-provider": "anthropic",
        "--model": "claude-sonnet-5",
        "--checkpointer": "postgres",
        "--registry": "ghcr.io/org",
        "--cd": "argocd",
        "--auth-policy": "product-session",
        "--process": "docs/process.md",
    }

    enhance_args = metadata_to_cli_args(cfg, for_enhance=True)
    assert "--agent" not in enhance_args
    assert _pairs(enhance_args)["--base-template"] == "langgraph"


def test_metadata_to_cli_args_none_target_has_no_registry() -> None:
    cfg = ProjectConfig.from_dict({"name": "svc", "create_params": {"deployment_target": "none"}})
    pairs = _pairs(metadata_to_cli_args(cfg))
    assert pairs["--deployment-target"] == "none"
    assert pairs["--checkpointer"] == "memory"
    assert pairs["--cd"] == "skip"
    assert "--registry" not in pairs
    assert "--process" not in pairs
    assert "--agent-directory" not in pairs


def test_remote_spec_goes_positional_for_enhance() -> None:
    cfg = ProjectConfig.from_dict({"name": "svc", "base_template": "org/repo/path@v1"})
    assert metadata_to_cli_args(cfg, for_enhance=True)[0] == "org/repo/path@v1"
    assert metadata_to_cli_args(cfg)[:2] == ["--agent", "org/repo/path@v1"]


def test_round_trip_create_manifest_create(run_create: CreateRunner) -> None:
    result, first = run_create(
        "--runtime",
        "langgraph-server",
        "--model-provider",
        "anthropic",
        "--checkpointer",
        "postgres",
        "--registry",
        "ghcr.io/org",
        "--cd",
        "argocd",
        "--auth-policy",
        "product-session",
        "--process",
        "docs/process.md",
        "--agent-directory",
        "bot",
    )
    assert result.exit_code == 0, result.output

    cfg = read_project_config(str(first))
    args = metadata_to_cli_args(cfg)
    assert args[:2] == ["--agent", "mini_agent"]

    # Re-create from the recorded flags: the manifest must come out identical
    # except for the timestamp.
    result, second = run_create(*args[2:], name="round-trip")
    assert result.exit_code == 0, result.output
    a = read_manifest(first)
    b = read_manifest(second)
    for manifest in (a, b):
        manifest.pop("generated_at")
        manifest.pop("name")
        manifest.pop("environments", None)
    assert a == b
    assert sorted((first / "bot").iterdir(), key=str) != []
    assert (second / "bot" / "agent.py").is_file()
