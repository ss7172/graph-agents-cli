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

"""Show project configuration, paths, and CLI version."""

from __future__ import annotations

import platform
from pathlib import Path
from typing import Any

import click

import graph_agents_cli as _cli_pkg
from graph_agents_cli.__init__ import __version__
from graph_agents_cli._output import emit
from graph_agents_cli._project import (
    ProjectConfig,
    check_cli_version,
    find_project_root,
    read_project_config,
)
from graph_agents_cli._skills_check import get_installed_skills
from graph_agents_cli.extension._loader import ExtensionSet, load_extension_set
from graph_agents_cli.extension._paths import user_config_root

_CLI_INSTALL_PATH = str(Path(_cli_pkg.__file__).parent)


def _print_extensions(extension_set: ExtensionSet) -> None:
    rows = extension_set.command_rows()
    conflicts = extension_set.conflict_rows()
    incompatible = extension_set.incompatible_rows()
    if not rows and not conflicts and not incompatible:
        return
    click.echo()
    click.echo("Extensions (experimental):")
    for r in rows:
        line = (
            f"  {r['command']}  ({r['kind']}, {r['scope']}) "
            f"<- {r['extension']}: {' '.join(r['run'])}"
        )
        if r.get("requires"):
            line += f"  [requires graph-agents-cli {r['requires']}]"
        if r.get("blocked"):
            line += "  [BLOCKED: out of range, this command will not run]"
        click.echo(line)
    for c in conflicts:
        click.echo(f"  ! conflict: {c} (claimed by multiple same-scope extensions)")
    for inc in incompatible:
        click.echo(
            f"  ! incompatible: {inc['extension']} requires graph-agents-cli "
            f"{inc['requires']} (running {inc['running']})"
        )


def _print_installed_skills(skills: list[dict] | None) -> None:
    """Print installed skills summary."""
    if skills is None:
        click.echo("Installed skills:   (could not query)")
        return
    if not skills:
        click.echo("Installed skills:   none")
        return
    # Group by scope
    by_scope: dict[str, list[str]] = {}
    for s in skills:
        scope = s.get("scope", "unknown")
        by_scope.setdefault(scope, []).append(s["name"])
    for scope, names in sorted(by_scope.items()):
        click.echo(f"Installed skills:   {len(names)} ({scope})")
        for name in sorted(names):
            click.echo(f"  - {name}")


def project_info(project_root: Path, cfg: ProjectConfig) -> dict[str, Any]:
    """The project block of ``info``: what the manifest records."""
    return {
        "project_root": str(project_root),
        "project_name": cfg.project_name,
        "cli_version": cfg.cli_version,
        "language": cfg.language,
        "base_template": cfg.base_template,
        "agent_directory": cfg.agent_directory,
        "runtime": cfg.runtime,
        "model_provider": cfg.model_provider,
        "model": cfg.model,
        "checkpointer": cfg.checkpointer,
        "deployment_target": cfg.deployment_target,
        "registry": cfg.registry,
        "cd": cfg.cd,
        "auth_policy": cfg.auth_policy,
        "auth_policy_implemented": cfg.auth_policy_implemented,
        "api_policy_file": cfg.api_policy_file,
        "process": cfg.process,
        "environments": cfg.environments,
        "secret_keys": cfg.secret_keys,
        "secrets_owner": cfg.secrets.owner,
    }


def _print_project(project_root: Path, cfg: ProjectConfig) -> None:
    click.echo()
    click.echo(f"Project root:       {project_root}")
    click.echo(f"Project name:       {cfg.project_name or '(not set)'}")
    click.echo(f"Scaffolded with:    {cfg.cli_version or '(unknown)'}")
    click.echo(f"Base template:      {cfg.base_template}")
    click.echo(f"Agent directory:    {cfg.agent_directory}")
    click.echo(f"Runtime:            {cfg.runtime}")
    click.echo(f"Model provider:     {cfg.model_provider}")
    click.echo(f"Model:              {cfg.model}")
    click.echo(f"Checkpointer:       {cfg.checkpointer}")
    click.echo(f"Deployment target:  {cfg.deployment_target}")
    click.echo(f"Registry:           {cfg.registry or '(none)'}")
    click.echo(f"CD:                 {cfg.cd}")
    implemented = "" if cfg.auth_policy_implemented else "  (stub, not yet implemented)"
    click.echo(f"Auth policy:        {cfg.auth_policy}{implemented}")
    click.echo(f"API policy:         {cfg.api_policy_file or 'none'}")
    click.echo(f"Process:            {cfg.process or 'none'}")
    if cfg.environments:
        click.echo("Environments:")
        for env_name, env in cfg.environments.items():
            context = env.get("context") or "(current context)"
            click.echo(f"  - {env_name}: namespace={env.get('namespace')} context={context}")
    else:
        click.echo("Environments:       none")


@click.command()
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON.")
def cmd_info(as_json: bool) -> None:
    """Show project configuration, paths, and CLI version."""
    installed_skills = get_installed_skills()
    project_root = find_project_root()
    os_info = platform.platform()
    extension_set = load_extension_set(project_root, user_config_root())

    base: dict[str, Any] = {
        "cli_version": __version__,
        "cli_install_path": _CLI_INSTALL_PATH,
        "os_info": os_info,
        "installed_skills": installed_skills,
        "extensions": extension_set.command_rows(),
        "extension_conflicts": extension_set.conflict_rows(),
        "extension_incompatible": extension_set.incompatible_rows(),
    }

    if project_root is None:
        if as_json:
            emit({**base, "project": None})
        else:
            click.echo(f"CLI version:        {__version__}")
            click.echo(f"CLI install path:   {_CLI_INSTALL_PATH}")
            click.echo(f"OS info:            {os_info}")
            _print_installed_skills(installed_skills)
            _print_extensions(extension_set)
            click.echo()
            click.echo("No agent project found in the current directory or any parent.")
            click.echo("  Run this command from within a project, or create one:")
            click.echo("    graph-agents-cli create my-agent")
        return

    cfg = read_project_config(str(project_root))
    check_cli_version(cfg)

    if as_json:
        emit({**base, "project": project_info(project_root, cfg)})
        return

    click.echo(f"CLI version:        {__version__}")
    click.echo(f"CLI install path:   {_CLI_INSTALL_PATH}")
    click.echo(f"OS info:            {os_info}")
    _print_installed_skills(installed_skills)
    _print_project(project_root, cfg)
    _print_extensions(extension_set)
