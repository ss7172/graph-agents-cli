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

from __future__ import annotations

import click
import pytest
from click.testing import CliRunner

from graph_agents_cli._click import LazyGroup
from graph_agents_cli.dev.cmd_build import build_commands, cmd_build, image_name


def test_image_name():
    assert image_name(project_name="my-agent", registry="ghcr.io/acme/") == "ghcr.io/acme/my-agent"
    assert image_name(project_name="my-agent", registry="") == "my-agent"
    assert image_name(project_name="my-agent", registry=None) == "my-agent"
    with pytest.raises(click.ClickException):
        image_name(project_name="", registry="ghcr.io/acme")


def test_build_commands():
    assert build_commands(image="ghcr.io/acme/my-agent", tag="abc123", push=False) == [
        ["docker", "build", "-t", "ghcr.io/acme/my-agent:abc123", "-f", "Dockerfile", "."],
    ]
    assert build_commands(image="x", tag="latest", push=True)[1] == ["docker", "push", "x:latest"]


def test_build_dry_run_prints_commands(fake_project, recorded_runs):
    (fake_project.root / "Dockerfile").write_text("FROM python:3.12\n")
    result = CliRunner().invoke(
        cmd_build, ["--dry-run", "--tag", "sha1", "--push"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    assert "docker build -t ghcr.io/acme/my-agent:sha1 -f Dockerfile ." in result.output
    assert "docker push ghcr.io/acme/my-agent:sha1" in result.output
    assert recorded_runs.commands == []


def test_build_runs_docker_build_and_push(fake_project, recorded_runs):
    (fake_project.root / "Dockerfile").write_text("FROM python:3.12\n")
    result = CliRunner().invoke(
        cmd_build, ["--push", "--registry", "localhost:5000"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    assert recorded_runs.commands == [
        ["docker", "build", "-t", "localhost:5000/my-agent:latest", "-f", "Dockerfile", "."],
        ["docker", "push", "localhost:5000/my-agent:latest"],
    ]
    assert "Built image: localhost:5000/my-agent:latest" in result.output
    assert "Pushed image" in result.output


def test_build_without_registry_uses_project_name(fake_project, recorded_runs):
    (fake_project.root / "Dockerfile").write_text("FROM python:3.12\n")
    fake_project.cfg.registry = ""
    result = CliRunner().invoke(cmd_build, [], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert recorded_runs.commands[0][3] == "my-agent:latest"


def test_build_docker_failure_exits_2(fake_project, recorded_runs):
    (fake_project.root / "Dockerfile").write_text("FROM python:3.12\n")
    recorded_runs.returncodes = [1]
    result = CliRunner().invoke(cmd_build, [])
    assert result.exit_code == 2
    assert "docker build failed" in result.output


@pytest.mark.parametrize("args", [[], ["--dry-run"], ["--push"]])
def test_build_placeholder_registry_is_a_config_error(fake_project, recorded_runs, args):
    """`create` writes ghcr.io/CHANGE-ME without a git remote; docker would reject it late."""
    (fake_project.root / "Dockerfile").write_text("FROM python:3.12\n")
    fake_project.cfg.registry = "ghcr.io/CHANGE-ME"
    result = CliRunner().invoke(cmd_build, args)
    assert result.exit_code == 3, result.output
    assert result.output.startswith("Error: ")  # like every other configuration error
    assert "still the placeholder 'ghcr.io/CHANGE-ME'" in result.output
    assert "create_params.registry" in result.output and "--registry" in result.output
    assert recorded_runs.commands == []
    # --registry overrides the placeholder.
    ok = CliRunner().invoke(cmd_build, [*args, "--registry", "ghcr.io/acme"])
    assert ok.exit_code == 0, ok.output


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        (["--registry", "ghcr.io/Acme"], "must be lowercase"),
        (["--tag", "v1/2"], "the tag 'v1/2'"),
        (["--registry", "bad_host.io/acme"], "not a valid registry host"),
    ],
)
def test_build_invalid_reference_is_a_config_error(fake_project, recorded_runs, args, fragment):
    (fake_project.root / "Dockerfile").write_text("FROM python:3.12\n")
    result = CliRunner().invoke(cmd_build, ["--dry-run", *args])
    assert result.exit_code == 3, result.output
    assert fragment in result.output
    assert recorded_runs.commands == []


def test_build_missing_dockerfile_exits_3(fake_project, recorded_runs):
    result = CliRunner().invoke(cmd_build, [])
    assert result.exit_code == 3
    assert "No Dockerfile" in result.output
    assert recorded_runs.commands == []


def test_build_has_no_experiment_gate():
    from graph_agents_cli import _experiments
    from graph_agents_cli.main import main

    assert isinstance(main, LazyGroup)
    assert "build" not in main._experiment_gates
    assert "build_command" not in _experiments._REGISTRY
    with pytest.raises(ValueError):
        _experiments.resolve_experiment("build_command")
