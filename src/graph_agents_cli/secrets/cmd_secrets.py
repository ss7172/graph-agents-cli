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
"""graph-agents-cli secrets commands — provision and inspect the app Secret (D14)."""

from __future__ import annotations

import click

from graph_agents_cli._click import LazyGroup
from graph_agents_cli._output import Console
from graph_agents_cli.deploy import _modes
from graph_agents_cli.deploy._config import load_settings
from graph_agents_cli.deploy._kube import ConfigError, Target
from graph_agents_cli.secrets import _apply


@click.group("secrets", cls=LazyGroup)
def secrets_group() -> None:
    """Provision and inspect the application Secret per environment.

    \b
    Subcommands:
      apply   Create or update <name>-app from the allow-listed keys of an env file
      status  List which allow-listed keys are present (values are never printed)
    """


def _target(settings, env: str) -> Target:
    base = settings.target(env)
    return Target(context=_modes.resolve_context(settings, env), namespace=base.namespace)


@secrets_group.command("apply")
@click.option("--env", "env", required=True, help="Target environment (dev, staging, prod).")
@click.option(
    "--env-file", "env_file", default=None, help="Env file; defaults to .env.<env> then .env."
)
@click.option(
    "--dry-run", "dry_run", is_flag=True, help="Print the kubectl pipeline and a redacted manifest."
)
def cmd_secrets_apply(env: str, env_file: str | None, dry_run: bool) -> None:
    """Create or update the app Secret from the allow-listed keys of an env file."""
    console = Console()
    settings = load_settings()
    target = _target(settings, env)
    path = _apply.resolve_env_file(env, env_file)
    if path is None:
        raise ConfigError(
            f"No env file found: pass --env-file or create .env.{env} (or .env) with the allow-listed keys: "
            + ", ".join(settings.secret_keys)
        )
    console.print(
        f"Secret {settings.secret_name} in {target.namespace} (context {target.context or 'current'}) from {path}"
    )
    values = _apply.read_env_file(path)
    existing = _apply.existing_values_for_plan(
        settings.secret_name, target, settings.secret_keys, values, dry_run=dry_run
    )
    plan = _apply.build_plan(
        name=settings.secret_name,
        target=target,
        allowed=settings.secret_keys,
        values=values,
        existing=existing,
        dry_run=dry_run,
    )
    _apply.apply_plan(plan, dry_run=dry_run, console=console)


@secrets_group.command("status")
@click.option("--env", "env", required=True, help="Target environment (dev, staging, prod).")
@click.option(
    "--dry-run", "dry_run", is_flag=True, help="Print the kubectl command without running it."
)
def cmd_secrets_status(env: str, dry_run: bool) -> None:
    """List which allow-listed keys are present in the app Secret (never values)."""
    console = Console()
    settings = load_settings()
    target = _target(settings, env)
    present = _apply.secret_keys_present(
        settings.secret_name, target, dry_run=dry_run, console=console
    )
    if dry_run:
        return
    if present is None:
        console.print(
            f"Secret {settings.secret_name} not found in namespace {target.namespace}.", style="red"
        )
        console.print("  Missing: " + ", ".join(settings.secret_keys))
        raise SystemExit(1)
    missing = [k for k in settings.secret_keys if k not in present]
    found = [k for k in settings.secret_keys if k in present]
    extra = sorted(present - set(settings.secret_keys))
    console.print(f"Secret {settings.secret_name} in {target.namespace}:")
    console.print("  present: " + (", ".join(found) or "(none)"), style="green")
    console.print(
        "  missing: " + (", ".join(missing) or "(none)"), style="yellow" if missing else "green"
    )
    if extra:
        console.print("  not in the allow-list: " + ", ".join(extra), style="dim")
    if missing:
        raise SystemExit(1)
