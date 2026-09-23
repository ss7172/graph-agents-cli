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

"""D6 combination validation and --prototype / --cd semantics of ``create``."""

from __future__ import annotations

import pytest

from graph_agents_cli.scaffold.utils import template
from graph_agents_cli.scaffold.utils.template import D6_COMBINATIONS, validate_combination

from .conftest import CreateRunner, read_manifest

ALL_COMBINATIONS = sorted(D6_COMBINATIONS)


def test_d6_table_covers_every_combination() -> None:
    expected = {
        (r, c, t)
        for r in ("fastapi", "langgraph-server")
        for c in ("memory", "postgres")
        for t in ("none", "kubernetes")
    }
    assert set(D6_COMBINATIONS) == expected
    invalid = {k for k, (valid, _) in D6_COMBINATIONS.items() if not valid}
    assert invalid == {
        ("fastapi", "memory", "kubernetes"),
        ("langgraph-server", "memory", "kubernetes"),
    }


@pytest.mark.parametrize(("runtime", "checkpointer", "target"), ALL_COMBINATIONS)
def test_validate_combination_matches_table(runtime: str, checkpointer: str, target: str) -> None:
    valid, note = D6_COMBINATIONS[(runtime, checkpointer, target)]
    if valid:
        validate_combination(runtime, checkpointer, target)
    else:
        with pytest.raises(ValueError, match=note.split(";")[0]):
            validate_combination(runtime, checkpointer, target)


def test_validate_combination_cd_requires_kubernetes() -> None:
    validate_combination("fastapi", "postgres", "kubernetes", "argocd")
    validate_combination("fastapi", "memory", "none", "skip")
    with pytest.raises(ValueError, match="requires --deployment-target kubernetes"):
        validate_combination("fastapi", "memory", "none", "helm-push")


@pytest.mark.parametrize(("runtime", "checkpointer", "target"), ALL_COMBINATIONS)
def test_create_enforces_every_combination(
    run_create: CreateRunner, runtime: str, checkpointer: str, target: str
) -> None:
    valid, note = D6_COMBINATIONS[(runtime, checkpointer, target)]
    result, project = run_create(
        "--runtime", runtime, "--checkpointer", checkpointer, "--deployment-target", target
    )
    if valid:
        assert result.exit_code == 0, result.output
        assert project.is_dir()
        params = read_manifest(project)["create_params"]
        assert (params["runtime"], params["checkpointer"], params["deployment_target"]) == (
            runtime,
            checkpointer,
            target,
        )
    else:
        assert result.exit_code != 0
        assert not project.exists()
        assert "Invalid combination" in result.output
        assert note.split(";")[0] in result.output


def test_default_checkpointer_follows_target(run_create: CreateRunner) -> None:
    result, project = run_create("--deployment-target", "kubernetes")
    assert result.exit_code == 0, result.output
    assert read_manifest(project)["create_params"]["checkpointer"] == "postgres"

    result, project = run_create("--deployment-target", "none", name="local-agent")
    assert result.exit_code == 0, result.output
    assert read_manifest(project)["create_params"]["checkpointer"] == "memory"
    assert template.default_checkpointer("kubernetes") == "postgres"
    assert template.default_checkpointer("none") == "memory"


def test_deployment_target_defaults_to_kubernetes(run_create: CreateRunner) -> None:
    result, project = run_create()
    assert result.exit_code == 0, result.output
    params = read_manifest(project)["create_params"]
    assert params["deployment_target"] == "kubernetes"
    assert params["runtime"] == "fastapi"
    assert params["model_provider"] == "openai"
    assert params["checkpointer"] == "postgres"
    assert params["cd"] == "skip"
    assert params["auth_policy"] == "shared-bearer"


def test_cd_other_than_skip_requires_kubernetes(run_create: CreateRunner) -> None:
    result, project = run_create("--deployment-target", "none", "--cd", "argocd")
    assert result.exit_code != 0
    assert "requires --deployment-target kubernetes" in result.output
    assert not project.exists()


def test_prototype_defaults_target_to_none_and_forces_cd_skip(run_create: CreateRunner) -> None:
    result, project = run_create("--prototype", "--cd", "argocd")
    assert result.exit_code == 0, result.output
    params = read_manifest(project)["create_params"]
    assert params["deployment_target"] == "none"
    assert params["cd"] == "skip"
    assert params["checkpointer"] == "memory"
    assert params["registry"] == ""
    assert not (project / "deployment").exists()
    assert "ignored due to --prototype" in result.output


def test_prototype_keeps_explicit_target(run_create: CreateRunner) -> None:
    result, project = run_create(
        "--prototype", "--deployment-target", "kubernetes", "--cd", "helm-push"
    )
    assert result.exit_code == 0, result.output
    params = read_manifest(project)["create_params"]
    assert params["deployment_target"] == "kubernetes"
    assert params["cd"] == "skip"  # forced by --prototype (C34)
    assert (project / "deployment" / "helm" / "mini" / "Chart.yaml").is_file()
    assert not (project / ".github" / "workflows" / "staging.yaml").exists()


def test_existing_project_directory_is_refused(run_create: CreateRunner) -> None:
    result, _project = run_create()
    assert result.exit_code == 0, result.output
    result, _ = run_create()
    assert result.exit_code != 0
    assert "already exists" in result.output
