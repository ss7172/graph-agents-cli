# Copyright 2026 Google LLC
# Modifications Copyright 2026 graph-agents-cli contributors
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

"""graph-agents-cli build command: ``docker build`` on the project's Dockerfile.

One runtime-specific Dockerfile per project, selected at scaffold time;
``build``, CI, and local-load all run ``docker build``. There is no experiment
gate. Exit codes: 0 ok, 2 when docker fails, 3 for a configuration error.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import click

from graph_agents_cli import _tools
from graph_agents_cli._project import chdir_project_root, read_project_config
from graph_agents_cli._runner import run
from graph_agents_cli.deploy._image import reference_problem as image_reference_problem

DEFAULT_TAG = "latest"
DOCKERFILE = "Dockerfile"
EXIT_TOOL_FAILURE = 2
EXIT_CONFIG_ERROR = 3


def image_name(*, project_name: str, registry: str | None) -> str:
    """``<registry>/<project>`` when a registry is set, else ``<project>``."""
    name = (project_name or "").strip()
    if not name:
        raise click.ClickException(
            "The manifest has no project name; cannot derive an image name.\n"
            "  Set 'name' in graph-agents-cli-manifest.yaml."
        )
    reg = (registry or "").strip().rstrip("/")
    return f"{reg}/{name}" if reg else name


def build_commands(
    *, image: str, tag: str, push: bool, dockerfile: str = DOCKERFILE
) -> list[list[str]]:
    ref = f"{image}:{tag}"
    commands = [["docker", "build", "-t", ref, "-f", dockerfile, "."]]
    if push:
        commands.append(["docker", "push", ref])
    return commands


@click.command("build")
@click.option("--tag", default=DEFAULT_TAG, show_default=True, help="Image tag.")
@click.option("--registry", default=None, help="Override the manifest registry (e.g. ghcr.io/org).")
@click.option("--push", is_flag=True, default=False, help="Push the image after building.")
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Print the docker commands without running them.",
)
@click.pass_context
def cmd_build(
    ctx: click.Context, tag: str, registry: str | None, push: bool, dry_run: bool
) -> None:
    """Build the agent container image.

    Runs `docker build -t <registry>/<name>:<tag> .` with the project's
    runtime-specific Dockerfile. The registry defaults to the manifest's
    `create_params.registry`; without one the image is named after the project.
    """
    chdir_project_root()
    cfg = read_project_config()
    project_root = Path.cwd()

    if not (project_root / DOCKERFILE).is_file():
        click.secho(
            f"Error: No {DOCKERFILE} at the project root ({project_root}).\n"
            "  Add deployment support with: graph-agents-cli scaffold enhance",
            fg="red",
            err=True,
        )
        ctx.exit(EXIT_CONFIG_ERROR)

    project_name = getattr(cfg, "project_name", "") or getattr(cfg, "name", "")
    effective_registry = registry if registry is not None else getattr(cfg, "registry", "")
    image = image_name(project_name=project_name, registry=effective_registry)
    # Checked before docker runs (and under --dry-run): the scaffold's placeholder
    # registry, or any malformed reference, would otherwise surface as a docker
    # parser error that does not name the setting to change.
    problem = image_reference_problem(image, tag, registry=effective_registry)
    if problem:
        # "Error:" like every other command's configuration error.
        click.secho(f"Error: {problem}", fg="red", err=True)
        ctx.exit(EXIT_CONFIG_ERROR)
    commands = build_commands(image=image, tag=tag, push=push)

    if dry_run:
        click.echo("Dry run; would execute:")
        for cmd in commands:
            click.echo(f"  {shlex.join(cmd)}")
        return

    _tools.require_tool(
        "docker",
        "Install Docker (https://docs.docker.com/get-docker/) and ensure it is in your PATH.",
    )
    for cmd in commands:
        result = run(cmd, check=False)
        if result.returncode != 0:
            click.secho(
                f"Error: {shlex.join(cmd[:2])} failed (exit code {result.returncode}).",
                fg="red",
                err=True,
            )
            ctx.exit(EXIT_TOOL_FAILURE)
    click.echo(f"Built image: {image}:{tag}")
    if push:
        click.echo(f"Pushed image: {image}:{tag}")
