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
"""graph-agents-cli secrets commands — provision and inspect the app Secret."""

from __future__ import annotations

import click

from graph_agents_cli._click import LazyGroup
from graph_agents_cli._output import Console
from graph_agents_cli.deploy import _modes, _preflight
from graph_agents_cli.deploy._config import DeploySettings, load_settings
from graph_agents_cli.deploy._kube import Target
from graph_agents_cli.deploy._values import load_chart_values
from graph_agents_cli.secrets import _apply, _required


@click.group("secrets", cls=LazyGroup)
def secrets_group() -> None:
    """Provision and inspect the application Secret per environment.

    \b
    Subcommands:
      apply   Create or update <name>-app from the allow-listed keys of an env file
      status  List which allow-listed keys are present (values are never printed)
    """


def _resolve(
    settings: DeploySettings, env: str, context: str | None
) -> tuple[_modes.ResolvedContext, Target]:
    namespace = settings.target(env).namespace
    resolved = _modes.resolve(settings, env, context)
    return resolved, Target(context=resolved.name, namespace=namespace)


_CONTEXT_HELP = "Kube context to use instead of environments.<env>.context."


def _default_env_file_values(env: str) -> dict[str, str]:
    """The env file ``secrets apply --env <env>`` reads by default (empty when absent or unreadable)."""
    path = _apply.resolve_env_file(env, None)
    if path is None:
        return {}
    try:
        return _apply.read_env_file(path)
    except (OSError, UnicodeDecodeError):
        return {}


@secrets_group.command("apply")
@click.option("--env", "env", required=True, help="Target environment (dev, staging, prod).")
@click.option(
    "--env-file",
    "env_file",
    default=None,
    help="Env file; defaults to .env.<env> (dev also falls back to .env).",
)
@click.option("--context", "context", default=None, help=_CONTEXT_HELP)
@click.option(
    "--yes",
    "-y",
    "yes",
    is_flag=True,
    help="Accept the kubeconfig's current context outside dev without prompting.",
)
@click.option(
    "--rotate-api-key",
    "rotate_api_key",
    is_flag=True,
    help="Replace the live API_KEY with the one in the env file (otherwise the live key wins).",
)
@click.option(
    "--dry-run", "dry_run", is_flag=True, help="Print the kubectl pipeline and a redacted manifest."
)
def cmd_secrets_apply(
    env: str,
    env_file: str | None,
    context: str | None,
    yes: bool,
    rotate_api_key: bool,
    dry_run: bool,
) -> None:
    """Create or update the app Secret from the allow-listed keys of an env file."""
    console = Console()
    settings = load_settings()
    resolved, target = _resolve(settings, env, context)
    path = _apply.resolve_env_file(env, env_file)
    chart_values = load_chart_values(settings.chart_dir, env)
    if path is None:
        raise _apply.missing_env_file_error(
            env, _required.for_environment(settings, chart_values).secret_keys
        )
    values = _apply.read_env_file(path)
    # The allow-list for this environment (AUTH_JWT_SECRET joins it for HS* JWTs).
    settings = _required.for_environment(settings, chart_values, values)
    _apply.check_file_values(
        values, settings.secret_keys, rotate_api_key=rotate_api_key, source=path
    )
    console.print(f"Secret {settings.secret_name} in {target.namespace} from {path}")
    _modes.announce(env, resolved, console=console)
    _modes.confirm(
        env, resolved, yes=yes, dry_run=dry_run, console=console, action="apply the Secret for"
    )
    _required.print_unreached_hs_settings(
        settings, env, chart_values, values, source=path, console=console
    )
    plan = _apply.provision(
        name=settings.secret_name,
        env=env,
        target=target,
        allowed=settings.secret_keys,
        path=path,
        values=values,
        rotate_api_key=rotate_api_key,
        dry_run=dry_run,
        console=console,
        # Only the shared-bearer policy reads API_KEY: never mint one it would not use.
        mint_api_key=_preflight.effective_auth_policy(settings, chart_values) == "shared-bearer",
        metrics_name=settings.metrics_secret_name,
    )
    if not _modes.is_dev_env(env):
        for key in _preflight.dsn_keys(settings, chart_values):
            dsn = plan.data.get(key) or ""
            if (
                dsn
                and dsn != _apply.PENDING_PLACEHOLDER
                and _preflight.dsn_without_tls(chart_values, dsn)
            ):
                console.print(
                    f"  Warning: {_preflight.dsn_tls_warning(key)}", style="yellow", markup=False
                )


@secrets_group.command("status")
@click.option("--env", "env", required=True, help="Target environment (dev, staging, prod).")
@click.option("--context", "context", default=None, help=_CONTEXT_HELP)
@click.option(
    "--strict",
    "strict",
    is_flag=True,
    help="Also exit 1 when an optional allow-listed key is missing.",
)
@click.option(
    "--dry-run", "dry_run", is_flag=True, help="Print the kubectl command without running it."
)
def cmd_secrets_status(env: str, context: str | None, strict: bool, dry_run: bool) -> None:
    """List which allow-listed keys are present in the app Secret (never values).

    \b
    Exit codes (usable as a CI or pre-deploy gate):
      0  the Secret holds every required key (optional ones may be missing)
      1  the Secret is missing, or a required key is (any key with --strict)
      2  kubectl failed (unreachable cluster, credentials, RBAC)
      3  configuration error (unknown environment or kube context, no manifest)
    Required keys follow the environment's chart values: the model provider's key
    (not for openai-compatible), API_KEY under shared-bearer, AUTH_JWT_SECRET under
    jwt with an HS* algorithm, and POSTGRES_DSN or DATABASE_URI/REDIS_URI unless the
    bundled subchart provides them.
    """
    console = Console()
    settings = load_settings()
    resolved, target = _resolve(settings, env, context)
    _modes.announce(env, resolved, console=console)
    _modes.require_known_context(env, resolved, dry_run=dry_run, console=console)
    values = load_chart_values(settings.chart_dir, env)
    # The env file `secrets apply` would read (never printed) tells whether the
    # environment uses HS* JWTs, which adds AUTH_JWT_SECRET to the allow-list.
    settings = _required.for_environment(settings, values, _default_env_file_values(env))
    required = _required.required_keys(settings, values)
    optional = [k for k in settings.secret_keys if k not in required]
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
    missing_required = [k for k in required if k not in present]
    missing_optional = [k for k in optional if k not in present]
    found = [k for k in settings.secret_keys if k in present]
    extra = sorted(present - set(settings.secret_keys))
    console.print(f"Secret {settings.secret_name} in {target.namespace}:")
    console.print("  present: " + (", ".join(found) or "(none)"), style="green")
    console.print(
        "  missing required: " + (", ".join(missing_required) or "(none)"),
        style="red" if missing_required else "green",
    )
    console.print(
        "  missing optional: " + (", ".join(missing_optional) or "(none)"),
        style="yellow" if missing_optional else "green",
    )
    if extra:
        console.print("  not in the allow-list: " + ", ".join(extra), style="dim")
    if missing_required or (strict and missing_optional):
        raise SystemExit(1)
