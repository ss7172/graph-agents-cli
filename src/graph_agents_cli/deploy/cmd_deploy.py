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
"""graph-agents-cli deploy command — deploy the agent to Kubernetes."""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import click

from graph_agents_cli._output import Console
from graph_agents_cli.deploy import _kube, _modes, gitops, local_load
from graph_agents_cli.deploy._config import DeploySettings, load_settings
from graph_agents_cli.deploy._kube import ConfigError, Refused, Target
from graph_agents_cli.deploy._values import load_chart_values, split_image_ref
from graph_agents_cli.secrets import _apply as secrets_apply

PROTECTED_ENVS = ("staging", "prod")


@click.command("deploy")
@click.option("--env", "env", required=True, help="Target environment (dev, staging, prod).")
@click.option(
    "--image",
    "image",
    default=None,
    help="Image reference to deploy instead of building one (CI mode).",
)
@click.option(
    "--env-file",
    "env_file",
    default=None,
    help="Env file for the Secret; defaults to .env.<env> then .env.",
)
@click.option("--status", "status", is_flag=True, help="Show rollout status instead of deploying.")
@click.option(
    "--restart",
    "restart",
    is_flag=True,
    help="Rollout-restart the Deployment (after a Secret rotation).",
)
@click.option(
    "--force-direct",
    "force_direct",
    is_flag=True,
    help="helm-push mode: allow a direct workstation deploy to staging/prod.",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    help="Print every command; run `helm template` instead of upgrade.",
)
@click.option(
    "--tag",
    "tag",
    default=None,
    help="Image tag for a local build (default: short git sha, else a timestamp).",
)
def cmd_deploy(
    env: str,
    image: str | None,
    env_file: str | None,
    status: bool,
    restart: bool,
    force_direct: bool,
    dry_run: bool,
    tag: str | None,
) -> None:
    """Deploy the agent to Kubernetes (mode depends on the project's CD setting).

    \b
    Modes (from create_params.cd in the manifest):
      skip       direct: build, local-load or push, apply the Secret, helm upgrade
      helm-push  CI builds and pushes; deploy --image runs helm only
      argocd     never runs helm: writes image.tag into values-<env>.yaml and opens a PR
    """
    console = Console()
    settings = load_settings()
    target = settings.target(env)
    context = _modes.resolve_context(settings, env)
    target = Target(context=context, namespace=target.namespace)
    mode = _modes.derive_mode(settings.cd, context)

    if status:
        _show_status(settings, env, target, mode, dry_run=dry_run, console=console)
        return

    if env in PROTECTED_ENVS and not settings.auth_policy_implemented:
        raise Refused(
            f"Refusing to deploy to {env}: the manifest records auth_policy_implemented: false.\n"
            f"  Implement the {settings.auth_policy} auth policy (the custom stub lives in\n"
            "  <agent_directory>/policies/custom.py), then set auth_policy_implemented: true\n"
            "  in graph-agents-cli-manifest.yaml."
        )

    if restart:
        _restart(settings, target, mode, dry_run=dry_run, console=console)
        return

    console.print(
        f"Environment: {env}  namespace: {target.namespace}  context: {context or '(current)'}"
    )
    console.print(f"Mode: {_modes.describe(mode)}")

    if mode == _modes.ARGOCD:
        _deploy_argocd(
            settings, env, image=image, env_file=env_file, tag=tag, dry_run=dry_run, console=console
        )
        return

    if mode == _modes.HELM_PUSH:
        _deploy_helm_push(
            settings,
            env,
            target,
            image=image,
            env_file=env_file,
            tag=tag,
            force_direct=force_direct,
            dry_run=dry_run,
            console=console,
        )
        return

    _deploy_direct(
        settings,
        env,
        target,
        mode,
        image=image,
        env_file=env_file,
        tag=tag,
        dry_run=dry_run,
        console=console,
    )


# --------------------------------------------------------------------------- modes


def _deploy_direct(
    settings: DeploySettings,
    env: str,
    target: Target,
    mode: str,
    *,
    image: str | None,
    env_file: str | None,
    tag: str | None,
    dry_run: bool,
    console: Console,
) -> None:
    _require_chart(settings, env)
    if image:
        repository, image_tag = split_image_ref(image)
        console.print(f"Using image {image} (build and push skipped).")
    else:
        repository = settings.image_repository
        image_tag = tag or _default_tag()
        _build(settings, repository, image_tag, dry_run=dry_run, console=console)
        if mode == _modes.LOCAL_LOAD:
            _load(
                target.context or "", f"{repository}:{image_tag}", dry_run=dry_run, console=console
            )
        else:
            _kube.run_cmd(
                ["docker", "push", f"{repository}:{image_tag}"],
                capture=False,
                dry_run=dry_run,
                console=console,
            )

    _apply_secret(settings, env, target, env_file=env_file, dry_run=dry_run, console=console)
    _helm_upgrade(settings, env, target, repository, image_tag, dry_run=dry_run, console=console)
    _print_done(settings, env, f"{repository}:{image_tag}", dry_run=dry_run, console=console)


def _deploy_helm_push(
    settings: DeploySettings,
    env: str,
    target: Target,
    *,
    image: str | None,
    env_file: str | None,
    tag: str | None,
    force_direct: bool,
    dry_run: bool,
    console: Console,
) -> None:
    _refuse_secrets(settings, env, env_file, mode=_modes.HELM_PUSH, console=console)
    _require_chart(settings, env)
    if image:
        repository, image_tag = split_image_ref(image)
    else:
        if env in PROTECTED_ENVS and not force_direct:
            raise Refused(
                f"Refusing a direct workstation deploy to {env} in helm-push mode.\n"
                "  CI on the self-hosted runner runs `deploy --image <ref> --env "
                f"{env}`; pass --force-direct to override."
            )
        repository = settings.image_repository
        image_tag = tag or _default_tag()
        _build(settings, repository, image_tag, dry_run=dry_run, console=console)
        _kube.run_cmd(
            ["docker", "push", f"{repository}:{image_tag}"],
            capture=False,
            dry_run=dry_run,
            console=console,
        )
    _helm_upgrade(settings, env, target, repository, image_tag, dry_run=dry_run, console=console)
    _print_done(settings, env, f"{repository}:{image_tag}", dry_run=dry_run, console=console)


def _deploy_argocd(
    settings: DeploySettings,
    env: str,
    *,
    image: str | None,
    env_file: str | None,
    tag: str | None,
    dry_run: bool,
    console: Console,
) -> None:
    _refuse_secrets(settings, env, env_file, mode=_modes.ARGOCD, console=console)
    values_path = settings.values_file(env)
    if not values_path.is_file():
        raise ConfigError(f"Values file not found: {values_path}")
    if image:
        repository, image_tag = split_image_ref(image)
    else:
        repository = (settings.registry and settings.image_repository) or None
        image_tag = tag or _default_tag()
        console.print(
            f"  No --image given; writing tag {image_tag!r}. The image must already be pushed "
            "by CI for Argo CD to roll it out.",
            style="yellow",
        )
    result = gitops.write_desired_state(
        env=env,
        values_path=values_path,
        image_repository=repository,
        tag=image_tag,
        project_name=settings.project_name,
        dry_run=dry_run,
        console=console,
    )
    if not result.changed:
        return
    what = "Would open" if dry_run else ("Opened" if result.created else "Updated")
    where = f" {result.url}" if result.url else ""
    console.print(
        f"{what} pull request on branch {result.branch}.{where}", style="green", markup=False
    )
    if env == "prod":
        console.print(
            "  The production change lands only when the PR merges after code-owner review."
        )


# --------------------------------------------------------------------------- steps


def _require_chart(settings: DeploySettings, env: str) -> None:
    chart = settings.chart_dir
    if not (chart / "Chart.yaml").is_file():
        raise ConfigError(f"Helm chart not found at {chart} (expected Chart.yaml).")
    if not settings.values_file(env).is_file():
        raise ConfigError(f"Values file not found: {settings.values_file(env)}")
    _require_gateway_values(settings, env)


def _truthy(value: object) -> bool:
    return value is True or (
        isinstance(value, str) and value.strip().lower() in ("true", "yes", "1")
    )


def _require_gateway_values(settings: DeploySettings, env: str) -> None:
    """Mirror the chart's ``required`` on ``gateway.parentRef.name`` before any tool runs.

    The scaffolded staging/prod values ship ``gateway.enabled: true`` with a blank
    parentRef (the operator names the Gateway); helm would only refuse after the
    image was built and pushed and the Secret applied, as a tool failure (exit
    2). This is configuration, so it is reported as such (exit 3) up front.
    """
    values = load_chart_values(settings.chart_dir, env)
    gateway = values.get("gateway") if isinstance(values.get("gateway"), dict) else {}
    if not _truthy(gateway.get("enabled")):
        return
    parent = gateway.get("parentRef") if isinstance(gateway.get("parentRef"), dict) else {}
    if str(parent.get("name") or "").strip():
        return
    raise ConfigError(
        f"gateway.parentRef.name is blank in {settings.values_file(env)} but gateway.enabled is "
        "true: set it to the Gateway to attach to (or set gateway.enabled: false / "
        "ingress.enabled: true)."
    )


def _dockerfile(settings: DeploySettings) -> str:
    for candidate in ("Dockerfile", f"Dockerfile.{settings.runtime}"):
        if Path(candidate).is_file():
            return candidate
    raise ConfigError("No Dockerfile found in the project root.")


def _default_tag() -> str:
    return gitops.short_sha() or _dt.datetime.now(_dt.UTC).strftime("%Y%m%d%H%M%S")


def _build(
    settings: DeploySettings, repository: str, tag: str, *, dry_run: bool, console: Console
) -> None:
    dockerfile = _dockerfile(settings)
    _kube.run_cmd(
        ["docker", "build", "-t", f"{repository}:{tag}", "-f", dockerfile, "."],
        capture=False,
        dry_run=dry_run,
        console=console,
    )


def _load(context: str, image: str, *, dry_run: bool, console: Console) -> None:
    commands = local_load.local_load_commands(context, image)
    if not commands:
        console.print(f"  {context}: the cluster shares the docker daemon; no image load needed.")
    for cmd in commands:
        _kube.run_cmd(cmd, capture=False, dry_run=dry_run, console=console)


def _apply_secret(
    settings: DeploySettings,
    env: str,
    target: Target,
    *,
    env_file: str | None,
    dry_run: bool,
    console: Console,
) -> None:
    path = secrets_apply.resolve_env_file(env, env_file)
    if path is None:
        console.print(
            f"  No env file found (.env.{env} or .env); the Secret {settings.secret_name} is left as is.",
            style="yellow",
        )
        return
    console.print(f"Applying Secret {settings.secret_name} from {path} (allow-listed keys only).")
    values = secrets_apply.read_env_file(path)
    existing = secrets_apply.existing_values_for_plan(
        settings.secret_name, target, settings.secret_keys, values, dry_run=dry_run
    )
    plan = secrets_apply.build_plan(
        name=settings.secret_name,
        target=target,
        allowed=settings.secret_keys,
        values=values,
        existing=existing,
        dry_run=dry_run,
    )
    secrets_apply.apply_plan(plan, dry_run=dry_run, console=console)


def _refuse_secrets(
    settings: DeploySettings, env: str, env_file: str | None, *, mode: str, console: Console
) -> None:
    procedure = secrets_apply.provisioning_procedure(
        project=settings.project_name, env=env, owner=settings.secrets_owner, mode=mode
    )
    if env_file:
        raise Refused(f"--env-file is not accepted in {mode} mode.\n  {procedure}")
    console.print(f"  {procedure}", style="dim", markup=False)


def _helm_args(settings: DeploySettings, env: str, repository: str, tag: str) -> list[str]:
    chart = settings.chart_dir
    return [
        str(chart),
        "-f",
        str(chart / "values.yaml"),
        "-f",
        str(settings.values_file(env)),
        "--set",
        f"image.repository={repository}",
        "--set",
        f"image.tag={tag}",
        "--set",
        f"existingSecret={settings.secret_name}",
    ]


def _chart_dependencies(chart: Path) -> list[str]:
    """Names of the subcharts ``Chart.yaml`` declares (``[]`` when none or unreadable)."""
    import yaml

    try:
        with open(chart / "Chart.yaml", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return []
    deps = data.get("dependencies") if isinstance(data, dict) else None
    if not isinstance(deps, list):
        return []
    return [str(d.get("name")) for d in deps if isinstance(d, dict) and d.get("name")]


def _missing_dependencies(chart: Path, names: list[str]) -> list[str]:
    """Declared subcharts with no archive or directory under ``charts/``."""
    charts_dir = chart / "charts"
    missing: list[str] = []
    for name in names:
        archives = list(charts_dir.glob(f"{name}-*.tgz")) if charts_dir.is_dir() else []
        if not archives and not (charts_dir / name).is_dir():
            missing.append(name)
    return missing


def _helm_dependency_build(settings: DeploySettings, *, dry_run: bool, console: Console) -> None:
    """Fetch the subcharts ``Chart.yaml`` declares before ``helm upgrade`` or ``helm template``.

    The scaffolded chart declares the bitnami ``postgresql`` and ``redis`` charts as
    conditional dependencies; helm refuses to render or install until they sit in
    ``charts/``. The build is skipped when every subchart is present and
    ``Chart.lock`` exists. Under ``--dry-run`` the command still runs: it only
    writes into the chart directory (no cluster access) and the dry-run render
    (``helm template``) cannot succeed without it.
    """
    chart = settings.chart_dir
    names = _chart_dependencies(chart)
    if not names:
        return
    missing = _missing_dependencies(chart, names)
    if not missing and (chart / "Chart.lock").is_file():
        return
    cmd = ["helm", "dependency", "build", str(chart)]
    _kube.echo_cmd(cmd, dry_run=dry_run, console=console)
    if dry_run:
        console.print(
            f"  [dry-run] fetching subchart(s) {', '.join(missing or names)} into "
            f"{chart / 'charts'} so the render below can run (local only).",
            style="cyan",
            markup=False,
        )
    result = _kube.run_cmd(cmd, check=False, quiet=True, console=console)
    if result.returncode != 0:
        raise _kube.ToolFailed(
            f"helm dependency build failed (exit code {result.returncode}); the chart declares "
            f"subchart(s) {', '.join(names)} that must be fetched before helm can render it. "
            "Check network access to the chart repository or vendor the charts under "
            f"{chart / 'charts'}.\n{(result.stderr or result.stdout).strip()}"
        )


def _helm_upgrade(
    settings: DeploySettings,
    env: str,
    target: Target,
    repository: str,
    tag: str,
    *,
    dry_run: bool,
    console: Console,
) -> None:
    common = _helm_args(settings, env, repository, tag)
    upgrade = ["upgrade", "--install", settings.release, *common, "--create-namespace", "--wait"]
    _helm_dependency_build(settings, dry_run=dry_run, console=console)
    if dry_run:
        _kube.echo_cmd(_kube.helm_args(upgrade, target), dry_run=True, console=console)
        console.print(
            "  [dry-run] rendering with `helm template` instead:", style="cyan", markup=False
        )
        result = _kube.helm(
            ["template", settings.release, *common], target, check=False, console=console
        )
        if result.returncode != 0:
            raise _kube.ToolFailed(
                f"helm template failed (exit code {result.returncode}):\n{(result.stderr or result.stdout).strip()}"
            )
        if result.stdout:
            console.print(result.stdout, highlight=False, markup=False)
        return
    _kube.helm(upgrade, target, capture=False, console=console)


def _print_done(
    settings: DeploySettings, env: str, image: str, *, dry_run: bool, console: Console
) -> None:
    verb = "Would deploy" if dry_run else "Deployed"
    console.print(f"{verb} {settings.release} ({image}) to {env}.", style="green")


# --------------------------------------------------------------------------- status / restart


def _show_status(
    settings: DeploySettings,
    env: str,
    target: Target,
    mode: str,
    *,
    dry_run: bool,
    console: Console,
) -> None:
    if mode == _modes.ARGOCD and _kube.tool_available("argocd"):
        _kube.run_cmd(
            ["argocd", "app", "get", f"{settings.project_name}-{env}"],
            capture=False,
            dry_run=dry_run,
            console=console,
        )
        return
    _kube.kubectl(
        ["rollout", "status", f"deployment/{settings.release}"],
        target,
        capture=False,
        dry_run=dry_run,
        console=console,
    )


def _restart(
    settings: DeploySettings, target: Target, mode: str, *, dry_run: bool, console: Console
) -> None:
    if mode == _modes.ARGOCD:
        console.print(
            "  Warning: this environment is reconciled by Argo CD with self-heal; the restart "
            "annotation may be reverted. Prefer an Argo resource action (restart) on the Deployment.",
            style="yellow",
        )
    _kube.kubectl(
        ["rollout", "restart", f"deployment/{settings.release}"],
        target,
        capture=False,
        dry_run=dry_run,
        console=console,
    )
    console.print(
        f"{'Would restart' if dry_run else 'Restarted'} deployment/{settings.release} in {target.namespace}."
    )
