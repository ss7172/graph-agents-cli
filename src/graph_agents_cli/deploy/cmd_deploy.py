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
"""graph-agents-cli deploy command — deploy the agent to Kubernetes.

Every check that needs only the project (chart and values, image reference,
env file) runs before anything touches a cluster, so a configuration error
never leaves a half-done deploy behind. Then the kube context is printed (and
confirmed outside ``dev``), and only then are images built and loaded or
pushed, the Secret applied, its required keys verified, and helm run.

A failed rollout leaves the environment as it was: the release is rolled
back to its last good revision, and the app Secret this run applied is put
back to its previous values (only while nobody else changed it since).
``--status`` and ``--restart`` wait for the rollout for a bounded time and
explain a rollout that does not finish.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from graph_agents_cli._output import Console
from graph_agents_cli.deploy import _image, _kube, _modes, _preflight, gitops, local_load
from graph_agents_cli.deploy._config import DeploySettings, load_settings
from graph_agents_cli.deploy._kube import ConfigError, Refused, Target
from graph_agents_cli.deploy._modes import ResolvedContext
from graph_agents_cli.deploy._values import load_chart_values, split_image_ref
from graph_agents_cli.secrets import _apply as secrets_apply
from graph_agents_cli.secrets import _required

PROTECTED_ENVS = _modes.PROTECTED_ENVS
DEFAULT_TIMEOUT = "5m"
# `--status` only reads: it reports within a minute unless told otherwise.
DEFAULT_STATUS_TIMEOUT = "60s"
# Warning events older than the start of this run (less this clock-skew
# allowance) belong to earlier rollouts and are not printed.
EVENT_CLOCK_SKEW_S = 30
_DURATION = re.compile(r"^(?=\d)(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")
DIAGNOSTIC_PODS = 3
DIAGNOSTIC_LOG_LINES = 40
DIAGNOSTIC_EVENTS = 15


def _timeout_option(_ctx: click.Context, _param: click.Parameter, value: str | None) -> str | None:
    """A helm duration: ``300`` (seconds), ``300s``, ``10m`` or ``1h30m``; never zero."""
    if value is None:
        return None
    raw = value.strip().lower()
    if raw.isdigit():
        raw += "s"
    match = _DURATION.match(raw)
    if not match or not any(match.groups()) or not any(int(g or 0) for g in match.groups()):
        raise click.BadParameter(
            f"{value!r} is not a duration; use for example 300s, 10m or 1h30m."
        )
    return raw


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
    help="Env file for the Secret; defaults to .env.<env> (dev also falls back to .env).",
)
@click.option(
    "--context",
    "context",
    default=None,
    help="Kube context to use instead of environments.<env>.context.",
)
@click.option(
    "--yes",
    "-y",
    "yes",
    is_flag=True,
    help="Accept the kubeconfig's current context outside dev without prompting.",
)
@click.option(
    "--status",
    "status",
    is_flag=True,
    help="Report the rollout, pods and warning events instead of deploying (exit 1 when not "
    "ready within --timeout, default 60s).",
)
@click.option(
    "--restart",
    "restart",
    is_flag=True,
    help="Rollout-restart the Deployment (after a Secret rotation) and wait for the new pods.",
)
@click.option(
    "--force-direct",
    "force_direct",
    is_flag=True,
    help="helm-push mode: allow a deploy to staging/prod from outside CI.",
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
    help="Image tag for a local build (default: short git sha, plus -dirty-<time> for "
    "uncommitted changes; a timestamp outside git).",
)
@click.option(
    "--timeout",
    "timeout",
    default=None,
    callback=_timeout_option,
    help=f"How long to wait for the rollout (e.g. 300s, 10m; default {DEFAULT_TIMEOUT}, "
    f"{DEFAULT_STATUS_TIMEOUT} for --status).",
)
@click.option(
    "--atomic/--no-atomic",
    "atomic",
    default=True,
    show_default=True,
    help="Roll back a failed rollout (after printing pod diagnostics).",
)
@click.option(
    "--rotate-api-key",
    "rotate_api_key",
    is_flag=True,
    help="Replace the live API_KEY with the one in the env file (otherwise the live key wins).",
)
def cmd_deploy(
    env: str,
    image: str | None,
    env_file: str | None,
    context: str | None,
    yes: bool,
    status: bool,
    restart: bool,
    force_direct: bool,
    dry_run: bool,
    tag: str | None,
    timeout: str | None,
    atomic: bool,
    rotate_api_key: bool,
) -> None:
    """Deploy the agent to Kubernetes (mode depends on the project's CD setting).

    \b
    Modes (from create_params.cd in the manifest):
      skip       direct: build, local-load or push, apply the Secret, helm upgrade
      helm-push  CI builds and pushes; deploy --image runs helm only
      argocd     never runs helm: writes image.tag into values-<env>.yaml and opens a PR
    \b
    Outside dev the kube context must be recorded in the manifest
    (environments.<env>.context) or passed with --context; the kubeconfig's
    current context is used only after a confirmation (or --yes).
    """
    console = Console()
    settings = load_settings()
    namespace = settings.target(env).namespace
    _modes.derive_mode(settings.cd, None)  # an unknown cd value is a configuration error
    resolved = _modes.resolve(settings, env, context)
    target = Target(context=resolved.name, namespace=namespace)

    if status:
        _modes.announce(env, resolved, console=console)
        _modes.require_known_context(env, resolved, dry_run=dry_run, console=console)
        _show_status(
            settings,
            env,
            target,
            timeout=timeout or DEFAULT_STATUS_TIMEOUT,
            dry_run=dry_run,
            console=console,
        )
        return
    timeout = timeout or DEFAULT_TIMEOUT

    if env in PROTECTED_ENVS and not settings.auth_policy_implemented:
        raise Refused(
            f"Refusing to deploy to {env}: the manifest records auth_policy_implemented: false.\n"
            f"  Implement the {settings.auth_policy} auth policy (the custom stub lives in\n"
            "  <agent_directory>/policies/custom.py), then set auth_policy_implemented: true\n"
            "  in graph-agents-cli-manifest.yaml."
        )

    if restart:
        _modes.announce(env, resolved, console=console)
        _modes.confirm(env, resolved, yes=yes, dry_run=dry_run, console=console, action="restart")
        _restart(settings, env, target, timeout=timeout, dry_run=dry_run, console=console)
        return

    console.print(f"Environment: {env}  namespace: {namespace}")
    options = _Options(
        image=image,
        env_file=env_file,
        tag=tag,
        yes=yes,
        force_direct=force_direct,
        dry_run=dry_run,
        timeout=timeout,
        atomic=atomic,
        rotate_api_key=rotate_api_key,
    )
    if settings.cd == _modes.ARGOCD:
        _deploy_argocd(settings, env, resolved, options, console=console)
    elif settings.cd == _modes.HELM_PUSH:
        _deploy_helm_push(settings, env, resolved, target, options, console=console)
    else:
        _deploy_direct(settings, env, resolved, target, options, console=console)


@dataclass(frozen=True)
class _Options:
    image: str | None
    env_file: str | None
    tag: str | None
    yes: bool
    force_direct: bool
    dry_run: bool
    timeout: str
    atomic: bool
    rotate_api_key: bool


@dataclass(frozen=True)
class _ImagePlan:
    repository: str
    tag: str
    build: bool

    @property
    def ref(self) -> str:
        return f"{self.repository}:{self.tag}"


# --------------------------------------------------------------------------- modes


def _deploy_direct(
    settings: DeploySettings,
    env: str,
    resolved: ResolvedContext,
    target: Target,
    opts: _Options,
    *,
    console: Console,
) -> None:
    # Project-only checks first: nothing below them may touch a cluster.
    _require_chart(settings, env)
    plan = _image_plan(settings, opts, console=console)
    path = secrets_apply.resolve_env_file(env, opts.env_file)
    chart_values = load_chart_values(settings.chart_dir, env)
    _check_chart_env(settings, env, chart_values, console=console)
    _check_jwt(settings, env, chart_values, None, dry_run=opts.dry_run, console=console, warn=False)
    if path is None and not _modes.is_dev_env(env):
        raise secrets_apply.missing_env_file_error(
            env, _required.for_environment(settings, chart_values).secret_keys
        )
    values: dict[str, str] = {}
    if path is not None:
        values = secrets_apply.read_env_file(path)
    # The allow-list for this environment (AUTH_JWT_SECRET joins it for HS* JWTs).
    settings = _required.for_environment(settings, chart_values, values)
    if path is not None:
        secrets_apply.check_file_values(
            values, settings.secret_keys, rotate_api_key=opts.rotate_api_key, source=path
        )
    elif opts.rotate_api_key:
        raise ConfigError("--rotate-api-key needs an env file that sets API_KEY.")

    _modes.announce(env, resolved, console=console)
    _modes.confirm(
        env, resolved, yes=opts.yes, dry_run=opts.dry_run, console=console, action="deploy to"
    )

    cluster: local_load.LocalCluster | None = None
    if plan.build:
        cluster, why = local_load.detect(target.context)
        mode = _modes.LOCAL_LOAD if cluster else _modes.REGISTRY
        detail = f": {cluster.describe()}" if cluster else f" ({why})" if why else ""
        console.print(f"Mode: {_modes.describe(mode)}{detail}", markup=False)
    else:
        console.print("Mode: direct, image given (build, load and push skipped)")
        console.print(f"Using image {plan.ref}.")

    # Plan the Secret and check its required keys (read-only) before anything is
    # built, pushed or changed: an incomplete Secret stops the deploy up front.
    # --dry-run makes the same reads, so it refuses what the real run would.
    secret_plan: secrets_apply.SecretPlan | None = None
    live: secrets_apply.LiveSecret | None = None
    if path is None:
        keys = _verify_secret(
            settings, env, target, fix_hint=None, dry_run=opts.dry_run, console=console
        )
    else:
        secret_plan, live = secrets_apply.prepare(
            name=settings.secret_name,
            env=env,
            target=target,
            allowed=settings.secret_keys,
            path=path,
            values=values,
            rotate_api_key=opts.rotate_api_key,
            dry_run=opts.dry_run,
            mint_api_key=_preflight.effective_auth_policy(settings, chart_values)
            == "shared-bearer",
            metrics_name=settings.metrics_secret_name,
            read_live_on_dry_run=True,
        )
        keys = _verify_secret(
            settings,
            env,
            target,
            keys=set(secret_plan.data),
            uncertain=bool(secret_plan.live_unread),
            fix_hint=f"Add them to {path} and re-run deploy",
            dry_run=opts.dry_run,
            console=console,
        )
        _warn_dsn_without_tls(settings, env, chart_values, secret_plan.data, console=console)
    _check_jwt(settings, env, chart_values, keys, dry_run=opts.dry_run, console=console)
    _check_release_idle(settings, target, dry_run=opts.dry_run, console=console)
    before = _live_workload(settings, target)
    _announce_same_image(settings, env, plan, before, console=console)

    if plan.build:
        _build(settings, plan, dry_run=opts.dry_run, console=console)
        if cluster is not None:
            _load(cluster, plan.ref, dry_run=opts.dry_run, console=console)
        else:
            _kube.run_cmd(
                ["docker", "push", plan.ref], capture=False, dry_run=opts.dry_run, console=console
            )

    snaps: list[secrets_apply.Snapshot] = []
    if secret_plan is None:
        console.print(
            f"  No env file found (.env.{env} or .env); the Secret {settings.secret_name} is "
            "left as is.",
            style="yellow",
        )
    else:
        console.print(
            f"Applying Secret {settings.secret_name} from {path} (allow-listed keys only)."
        )
        _required.print_unreached_hs_settings(
            settings, env, chart_values, values, source=path, console=console
        )
        if not opts.dry_run:
            # What the Secret(s) held before, to put back if the rollout fails.
            snaps = secrets_apply.snapshot(secret_plan, live, managed=settings.secret_keys)
        if opts.dry_run:
            console.print(
                "  [dry-run] if the rollout fails and the release is rolled back, the Secret is "
                "put back to its current values.",
                style="cyan",
                markup=False,
            )
    try:
        if secret_plan is not None:
            secrets_apply.apply_plan(secret_plan, dry_run=opts.dry_run, console=console, live=live)
        _helm_upgrade(settings, env, target, plan, opts, console=console)
    except _kube.DeployError as e:
        # A failure while applying (the metrics Secret, say) leaves the release untouched too.
        lines = _secret_outcome(snaps, env, e, console=console)
        if not lines:
            raise
        raise type(e)("\n  ".join([str(e.message), *lines])) from None
    except KeyboardInterrupt:
        # Interrupted mid-rollout: the release's state is unknown, so nothing is undone.
        for line in secrets_apply.describe_unrestored(snaps, env, "the deploy was interrupted"):
            console.print(f"  {line}", style="yellow", markup=False)
        raise
    _report_rollout(settings, env, target, before, snaps, dry_run=opts.dry_run, console=console)
    _print_done(settings, env, plan.ref, dry_run=opts.dry_run, console=console)


def _deploy_helm_push(
    settings: DeploySettings,
    env: str,
    resolved: ResolvedContext,
    target: Target,
    opts: _Options,
    *,
    console: Console,
) -> None:
    _refuse_secrets(settings, env, opts, mode=_modes.HELM_PUSH, console=console)
    _require_chart(settings, env)
    chart_values = load_chart_values(settings.chart_dir, env)
    _check_chart_env(settings, env, chart_values, console=console)
    _check_jwt(settings, env, chart_values, None, dry_run=opts.dry_run, console=console, warn=False)
    if env in PROTECTED_ENVS and not opts.force_direct and not _kube.in_ci():
        raise Refused(
            f"Refusing to deploy {env} from outside CI in helm-push mode (with or without "
            "--image).\n"
            f"  The staging and promote-to-prod workflows run `deploy --env {env} --image <ref>` "
            "on the self-hosted runner (GITHUB_ACTIONS=true); pass --force-direct to deploy "
            "from here anyway."
        )
    plan = _image_plan(settings, opts, console=console)

    _modes.announce(env, resolved, console=console)
    _modes.confirm(
        env, resolved, yes=opts.yes, dry_run=opts.dry_run, console=console, action="deploy to"
    )
    console.print(f"Mode: {_modes.describe(_modes.HELM_PUSH)}")
    keys = _verify_secret(
        settings,
        env,
        target,
        fix_hint=f"The Secret owner provisions them with `graph-agents-cli secrets apply --env {env}`",
        dry_run=opts.dry_run,
        console=console,
    )
    _check_jwt(settings, env, chart_values, keys, dry_run=opts.dry_run, console=console)
    _check_release_idle(settings, target, dry_run=opts.dry_run, console=console)
    before = _live_workload(settings, target)
    _announce_same_image(settings, env, plan, before, console=console)
    if plan.build:
        _build(settings, plan, dry_run=opts.dry_run, console=console)
        _kube.run_cmd(
            ["docker", "push", plan.ref], capture=False, dry_run=opts.dry_run, console=console
        )
    _helm_upgrade(settings, env, target, plan, opts, console=console)
    _report_rollout(settings, env, target, before, [], dry_run=opts.dry_run, console=console)
    _print_done(settings, env, plan.ref, dry_run=opts.dry_run, console=console)


def _deploy_argocd(
    settings: DeploySettings,
    env: str,
    resolved: ResolvedContext,
    opts: _Options,
    *,
    console: Console,
) -> None:
    console.print(f"Mode: {_modes.describe(_modes.ARGOCD)}")
    console.print(
        f"  No cluster is contacted (kube context {resolved.name or '(none)'} is not used): "
        "Argo CD applies the change after the pull request merges.",
        style="dim",
        markup=False,
    )
    _refuse_secrets(settings, env, opts, mode=_modes.ARGOCD, console=console)
    values_path = settings.values_file(env)
    if not values_path.is_file():
        raise ConfigError(f"Values file not found: {values_path}")
    chart_values = load_chart_values(settings.chart_dir, env)
    # Argo CD renders these values as they are: check what its pods would get.
    _check_chart_env(settings, env, chart_values, console=console)
    _check_jwt(settings, env, chart_values, None, dry_run=opts.dry_run, console=console)
    chart_repository = str((chart_values.get("image") or {}).get("repository") or "")
    if _image.has_placeholder(chart_repository):
        raise ConfigError(
            f"image.repository in the chart values is still the placeholder {chart_repository!r}; "
            "Argo CD would pull it. Set image.repository in "
            f"{settings.chart_dir / 'values.yaml'} (and create_params.registry in the manifest)."
        )
    if opts.image:
        repository, image_tag = split_image_ref(opts.image)
        problem = _image.reference_problem(repository, image_tag)
        if problem:
            raise ConfigError(f"--image: {problem}")
        if chart_repository and repository != chart_repository:
            console.print(
                f"  argocd mode writes only image.tag: Argo CD pulls {chart_repository}:"
                f"{image_tag}, not {repository}:{image_tag}. Change image.repository in the "
                "chart values if the repository moved.",
                style="yellow",
                markup=False,
            )
    else:
        repository = (settings.registry and settings.image_repository) or None
        image_tag = opts.tag or gitops.short_sha() or _timestamp()
        problem = _image.tag_problem(image_tag)
        if problem:
            raise ConfigError(f"--tag: {problem}.")
        console.print(
            f"  No --image given; writing tag {image_tag!r}. The image must already be pushed "
            "by CI for Argo CD to roll it out.",
            style="yellow",
        )
        if opts.tag is None and gitops.worktree_dirty():
            console.print(
                "  The working tree has uncommitted changes; they are not in the image CI "
                f"built for {image_tag}.",
                style="yellow",
            )
    result = gitops.write_desired_state(
        env=env,
        values_path=values_path,
        image_repository=repository,
        tag=image_tag,
        project_name=settings.project_name,
        dry_run=opts.dry_run,
        console=console,
    )
    if not result.changed:
        return
    what = "Would open" if opts.dry_run else ("Opened" if result.created else "Updated")
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


def _timestamp() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y%m%d%H%M%S")


def _workstation_tag(console: Console) -> str:
    """The short commit SHA; ``<sha>-dirty-<time>`` with uncommitted changes; a timestamp outside git.

    Rebuilding at the same commit under the same tag renders an identical pod
    spec (and ``IfNotPresent`` nodes keep the old image), so helm reports
    success while nothing rolls out. A dirty tree therefore always gets a new tag.
    """
    sha = gitops.short_sha()
    if not sha:
        return _timestamp()
    if gitops.worktree_dirty():
        tag = f"{sha}-dirty-{_timestamp()}"
        console.print(
            f"  The working tree has uncommitted changes: tagging the image {tag} so the "
            "rollout picks them up. Commit first for a reproducible deploy.",
            style="yellow",
            markup=False,
        )
        return tag
    return sha


def _image_plan(settings: DeploySettings, opts: _Options, *, console: Console) -> _ImagePlan:
    """The image to deploy, validated before docker, kubectl or helm run (exit 3 when invalid)."""
    if opts.image:
        repository, image_tag = split_image_ref(opts.image)
        problem = _image.reference_problem(repository, image_tag)
        if problem:
            raise ConfigError(f"--image: {problem}")
        return _ImagePlan(repository, image_tag, build=False)
    if _image.has_placeholder(settings.registry):
        raise ConfigError(_image.placeholder_message(settings.registry))
    repository = settings.image_repository
    image_tag = opts.tag or _workstation_tag(console)
    problem = _image.reference_problem(repository, image_tag, registry=settings.registry)
    if problem:
        raise ConfigError(problem)
    return _ImagePlan(repository, image_tag, build=True)


def _build(settings: DeploySettings, plan: _ImagePlan, *, dry_run: bool, console: Console) -> None:
    dockerfile = _dockerfile(settings)
    _kube.run_cmd(
        ["docker", "build", "-t", plan.ref, "-f", dockerfile, "."],
        capture=False,
        dry_run=dry_run,
        console=console,
    )


def _load(cluster: local_load.LocalCluster, image: str, *, dry_run: bool, console: Console) -> None:
    commands = local_load.local_load_commands(cluster, image)
    if not commands:
        console.print(f"  {cluster.describe()}: no image load needed.", markup=False)
    for cmd in commands:
        _kube.run_cmd(cmd, capture=False, dry_run=dry_run, console=console)


def _refuse_secrets(
    settings: DeploySettings, env: str, opts: _Options, *, mode: str, console: Console
) -> None:
    procedure = secrets_apply.provisioning_procedure(
        project=settings.project_name, env=env, owner=settings.secrets_owner, mode=mode
    )
    for flag, given in (("--env-file", opts.env_file), ("--rotate-api-key", opts.rotate_api_key)):
        if given:
            raise Refused(f"{flag} is not accepted in {mode} mode.\n  {procedure}")
    console.print(f"  {procedure}", style="dim", markup=False)


def _first_line(error: object) -> str:
    return next((line.strip() for line in str(error).splitlines() if line.strip()), "")


def _verify_secret(
    settings: DeploySettings,
    env: str,
    target: Target,
    *,
    keys: set[str] | None = None,
    uncertain: bool = False,
    fix_hint: str | None,
    dry_run: bool,
    console: Console,
) -> set[str] | None:
    """Refuse (exit 1) before anything changes when the Secret lacks a key the pods need.

    ``keys`` are the keys the Secret will hold after this deploy applies it; when
    ``None`` the live Secret is read (read-only, so ``--dry-run`` reads it too and
    refuses what the real run would). ``uncertain``: under ``--dry-run`` the live
    Secret could not be read, so a key missing from ``keys`` may still be there.
    Nothing has been built, pushed or applied when this refuses. Returns the keys
    the Secret holds or will hold (``None`` when unknown).
    """
    required = _required.required_keys(settings, load_chart_values(settings.chart_dir, env))
    name = settings.secret_name
    absent = False
    if keys is None:
        try:
            found = secrets_apply.secret_keys_present(name, target, console=console)
        except _kube.ToolFailed as e:
            if not dry_run:
                raise
            console.print(
                f"  [dry-run] could not read Secret {name} ({_first_line(e)}); the real run "
                "refuses unless it holds: " + (", ".join(required) or "(no required key)"),
                style="yellow",
                markup=False,
            )
            return None
        absent = found is None
        present = found or set()
    else:
        present = keys
    if not required:
        return present
    missing = [k for k in required if k not in present]
    if not missing:
        verb = "would hold" if dry_run else ("will hold" if keys is not None else "holds")
        console.print(
            f"  Secret {name} {verb} the required key(s): {', '.join(required)}.", style="dim"
        )
        return present
    if uncertain:
        console.print(
            f"  [dry-run] Secret {name} would lack {', '.join(missing)} unless the live Secret "
            "(not readable here) holds them: the real run refuses otherwise.",
            style="yellow",
            markup=False,
        )
        return present
    fix = fix_hint or (
        f"Put them in .env.{env} and re-run deploy, or provision them with "
        f"`graph-agents-cli secrets apply --env {env}`"
    )
    why_jwt = (
        f"\n  {_required.JWT_SECRET_KEY} is needed because {settings.values_file(env)} (or "
        "values.yaml) lists an HS* algorithm in AUTH_JWT_ALGORITHMS."
        if _required.JWT_SECRET_KEY in missing
        else ""
    )
    dry = "\n  (--dry-run: the real deploy stops here the same way.)" if dry_run else ""
    raise Refused(
        f"Secret {name} in {target.namespace} {'would be' if dry_run else 'is'} missing required "
        f"key(s): {', '.join(missing)}{' (the Secret does not exist)' if absent else ''}.\n"
        "  Without them the pods crash or answer every request with 503; nothing was built, "
        "applied or deployed.\n"
        f"  {fix}, or remove a key from secrets.keys in graph-agents-cli-manifest.yaml if "
        f"{env} does not need it.{why_jwt}{dry}"
    )


def _check_chart_env(
    settings: DeploySettings, env: str, values: dict[str, Any], *, console: Console
) -> None:
    """Refuse (exit 3) a scaffold placeholder in the chart env outside dev; warn in dev.

    ``api add`` writes ``<API>_BASE_URL: http://CHANGE-ME`` and an
    ``openai-compatible`` project starts with ``OPENAI_BASE_URL`` at CHANGE-ME:
    the pods would call that address, and every tool (or the model) would fail.
    """
    keys = _preflight.env_placeholders(values)
    if not keys:
        return
    names = ", ".join(f"env.{k}" for k in keys)
    where = f"{settings.values_file(env)} (or {settings.chart_dir / 'values.yaml'})"
    if _modes.is_dev_env(env):
        console.print(
            f"  Warning: {names} still hold(s) the placeholder CHANGE-ME: the {env} pods would "
            f"call it, so those calls fail. Set the real value(s) in {where}.",
            style="yellow",
            markup=False,
        )
        return
    raise ConfigError(
        f"{names} still hold(s) the placeholder CHANGE-ME in the chart values for {env}: the "
        f"pods would call it. Set the real value(s) in {where}."
    )


def _check_jwt(
    settings: DeploySettings,
    env: str,
    values: dict[str, Any],
    keys: set[str] | None,
    *,
    dry_run: bool,
    console: Console,
    warn: bool = True,
) -> None:
    """The ``jwt`` policy's verification settings: exit 3 outside dev, a warning in dev."""
    findings = _preflight.jwt_findings(settings, env, values, keys)
    error = findings.error()
    if error:
        raise ConfigError(
            error + ("\n  (--dry-run: the real deploy stops here the same way.)" if dry_run else "")
        )
    warning = findings.warning()
    if warning and warn:
        console.print(f"  Warning: {warning}", style="yellow", markup=False)


def _warn_dsn_without_tls(
    settings: DeploySettings,
    env: str,
    values: dict[str, Any],
    data: dict[str, str],
    *,
    console: Console,
) -> None:
    """Outside dev, warn when the external database's connection string does not require TLS.

    The value is inspected in memory and never printed.
    """
    for key in _preflight.dsn_keys(settings, values):
        dsn = data.get(key) or ""
        if _modes.is_dev_env(env) or not dsn or dsn == secrets_apply.PENDING_PLACEHOLDER:
            continue
        if _preflight.dsn_without_tls(values, dsn):
            console.print(
                f"  Warning: {_preflight.dsn_tls_warning(key)}", style="yellow", markup=False
            )


@dataclass(frozen=True)
class _Workload:
    """The live Deployment as far as a deploy needs it (read-only)."""

    image: str
    generation: int


def _live_workload(settings: DeploySettings, target: Target) -> _Workload | None:
    """The release's Deployment (its agent image and generation); ``None`` when absent or unreadable."""
    try:
        result = _kube.kubectl(
            ["get", "deployment", settings.release, "-o", "json"],
            target,
            check=False,
            quiet=True,
        )
        body = json.loads(result.stdout or "null") if result.returncode == 0 else None
    except (_kube.ToolFailed, json.JSONDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    containers = ((body.get("spec") or {}).get("template") or {}).get("spec", {}).get(
        "containers"
    ) or []
    agent = next((c for c in containers if c.get("name") == "agent"), None) or (
        containers[0] if containers else {}
    )
    try:
        generation = int((body.get("metadata") or {}).get("generation") or 0)
    except (TypeError, ValueError):
        generation = 0
    return _Workload(image=str(agent.get("image") or ""), generation=generation)


def _announce_same_image(
    settings: DeploySettings,
    env: str,
    plan: _ImagePlan,
    live: _Workload | None,
    *,
    console: Console,
) -> None:
    """Say up front when the release already runs this exact image reference."""
    if live is None or live.image != plan.ref:
        return
    rebuilt = " The image is rebuilt and loaded under the same tag." if plan.build else ""
    console.print(
        f"  {settings.release} already runs {plan.ref}: the image is unchanged.{rebuilt} helm "
        "records a new revision, but the pods are replaced only if the chart values change, "
        f"and a changed Secret reaches them only with `graph-agents-cli deploy --env {env} "
        "--restart` (reported after the rollout).",
        style="yellow",
        markup=False,
    )


def _secret_outcome(
    snaps: list[secrets_apply.Snapshot], env: str, error: Exception, *, console: Console
) -> list[str]:
    """After a failed helm step: restore the Secret(s) when the release is as it was before."""
    if not any(snap.touched for snap in snaps):
        return []
    if getattr(error, "release_unchanged", True):
        return secrets_apply.restore(snaps, console=console)
    return secrets_apply.describe_unrestored(
        snaps, env, "the release was not put back to its previous revision"
    )


def _report_rollout(
    settings: DeploySettings,
    env: str,
    target: Target,
    before: _Workload | None,
    snaps: list[secrets_apply.Snapshot],
    *,
    dry_run: bool,
    console: Console,
) -> None:
    """After a successful upgrade: say when no pod was replaced, and what that leaves stale."""
    if dry_run or before is None:
        return
    after = _live_workload(settings, target)
    if after is None or after.generation != before.generation:
        return
    console.print(
        "  No pods were replaced: the pod template (image and chart values) is unchanged.",
        style="yellow",
    )
    changed = sorted({k for snap in snaps for k in snap.touched})
    if changed:
        console.print(
            f"  The Secret changed ({', '.join(changed)}), but running pods read it only when "
            f"they start: run `graph-agents-cli deploy --env {env} --restart`.",
            style="yellow",
            markup=False,
        )


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
    plan: _ImagePlan,
    opts: _Options,
    *,
    console: Console,
) -> None:
    """``helm upgrade --install --wait --timeout``; on failure print diagnostics, then roll back.

    The rollback (``--atomic``, the default) is done here rather than with
    helm's own ``--atomic``: helm rolls back before it returns, which deletes
    the failed pods and their logs, the very output that explains the failure.
    It follows helm's rule: back to the newest deployed or superseded revision,
    or uninstall a first install that never succeeded.

    It only ever undoes the revision this run created. The release's newest
    revision is recorded just before the upgrade; after a failure the CLI acts
    only when exactly one newer revision exists and it is ``failed``. When helm
    reports another operation in progress, or another deploy changed the
    release meanwhile, the release is left alone: rolling back would undo (or
    uninstall) someone else's rollout.
    """
    common = _helm_args(settings, env, plan.repository, plan.tag)
    upgrade = [
        "upgrade",
        "--install",
        settings.release,
        *common,
        "--create-namespace",
        "--wait",
        "--timeout",
        opts.timeout,
    ]
    _helm_dependency_build(settings, dry_run=opts.dry_run, console=console)
    if opts.dry_run:
        _kube.echo_cmd(_kube.helm_args(upgrade, target), dry_run=True, console=console)
        if opts.atomic:
            console.print(
                "  [dry-run] a failed rollout prints pod diagnostics and is rolled back "
                "(--no-atomic keeps it).",
                style="cyan",
                markup=False,
            )
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
    before = _release_history(settings, target, console=console)
    _refuse_if_busy(settings, target, before, changed="the release was not touched")
    console.print(f"  helm waits up to {opts.timeout} for the rollout.", style="dim")
    started = _now()
    # Captured (helm prints nothing until the rollout ends under --wait) so that
    # helm's own "another operation is in progress" refusal can be recognised.
    result = _kube.helm(upgrade, target, check=False, console=console)
    for stream in (result.stdout, result.stderr):
        if (stream or "").strip():
            console.print(stream.rstrip(), highlight=False, markup=False)
    if result.returncode == 0:
        return
    after = _release_history(settings, target, console=console)
    if _HELM_BUSY in f"{result.stderr or ''}\n{result.stdout or ''}":
        raise RolloutFailed(
            _busy_message(settings, target, after, changed="the release was not touched"),
            release_unchanged=True,
        )
    outcome, unchanged = _failure_outcome(
        settings, target, before, after, opts, since=started, console=console
    )
    raise RolloutFailed(
        f"helm upgrade failed (exit code {result.returncode}) for {settings.release} in "
        f"{target.namespace}; {outcome}.",
        release_unchanged=unchanged,
    )


class RolloutFailed(_kube.ToolFailed):
    """helm failed (exit 2); ``release_unchanged``: the release is back as it was before the run.

    Only then is the Secret this run applied put back: a release left on the
    failed (or another deploy's) revision keeps the values it was rolled out with.
    """

    def __init__(self, message: str, *, release_unchanged: bool = False) -> None:
        super().__init__(message)
        self.release_unchanged = release_unchanged


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


def _failure_outcome(
    settings: DeploySettings,
    target: Target,
    before: _History,
    after: _History,
    opts: _Options,
    *,
    since: _dt.datetime | None = None,
    console: Console,
) -> tuple[str, bool]:
    """Undo the failed revision this run created, if any; describe what happened to the release.

    Returns the description and whether the release is back as it was before the
    run (no new revision, rolled back, or a failed first install uninstalled).
    """
    check = f"check `helm history {settings.release} -n {target.namespace}`"
    if not (before.readable and after.readable):
        return (
            "the release history could not be read, so the failure cannot be tied to a "
            f"revision of this run and nothing was rolled back; {check}"
        ), False
    newer = after.newer_than(before.latest)
    if not newer:
        return (
            "helm recorded no new revision (it failed before the rollout), so the release is "
            "unchanged and there is nothing to roll back"
        ), True
    newest = newer[-1]
    number, status = int(newest["revision"]), _status(newest)
    if status.startswith(_PENDING):
        if len(newer) > 1:
            return (
                f"another helm operation started after this one failed (revision {number} is "
                f"{status}), so nothing was rolled back; {check}"
            ), False
        # Either this run's helm stopped before finishing its revision (killed,
        # crashed, lost the connection) or another deploy holds the release.
        return (
            f"revision {number} is still {status} (helm stopped before finishing it, or "
            "another deploy is working on the release), so nothing was rolled back; if no "
            f"other deploy is running, {_clear_hint(settings, target, after)} clears it"
        ), False
    if len(newer) > 1:
        return (
            f"another deploy changed the release while this one ran (revisions "
            f"{int(newer[0]['revision'])} to {number} are new), so nothing was rolled back; "
            f"{check}"
        ), False
    if status != "failed":
        return (
            f"its new revision {number} is {status}, so nothing was rolled back; {check}",
            False,
        )
    # Exactly one new revision and it failed: the one this run created.
    # Read the diagnostics before the rollback: it removes the failed pods and their logs.
    _print_rollout_diagnostics(settings, target, since=since, console=console)
    if not opts.atomic:
        return (
            f"the failed revision {number} was left in place (--no-atomic); roll back with "
            f"`helm rollback {settings.release} -n {target.namespace}`"
        ), False
    return _roll_back(
        settings,
        target,
        after,
        number,
        existed=before.installed,
        timeout=opts.timeout,
        console=console,
    )


def _diag(console: Console, args: list[str], target: Target, *, tail: int | None = None) -> str:
    """Run a read-only kubectl for diagnostics and print it; never raises. Returns stdout."""
    cmd = _kube.kubectl_args(args, target)
    try:
        result = _kube.run_cmd(cmd, check=False, quiet=True)
    except _kube.ToolFailed as e:
        console.print(f"  $ {_kube.format_cmd(cmd)}\n    {e}", markup=False, highlight=False)
        return ""
    out = (result.stdout or "").rstrip() or (result.stderr or "").rstrip() or "(no output)"
    lines = out.splitlines()
    if tail is not None and len(lines) > tail + 1:
        lines = lines[:1] + lines[-tail:]  # keep the header row
    console.print(f"  $ {_kube.format_cmd(cmd)}", style="dim", markup=False, highlight=False)
    for line in lines:
        console.print(f"    {line}", markup=False, highlight=False)
    return result.stdout or ""


def _pod_ready(pod: dict[str, Any]) -> bool:
    status = pod.get("status") or {}
    if status.get("phase") == "Succeeded":
        return True
    return any(
        c.get("type") == "Ready" and str(c.get("status")) == "True"
        for c in status.get("conditions") or []
    )


def _terminating(pod: dict[str, Any]) -> bool:
    return bool((pod.get("metadata") or {}).get("deletionTimestamp"))


def _container_notes(pod: dict[str, Any]) -> list[str]:
    status = pod.get("status") or {}
    notes: list[str] = []
    for c in (status.get("initContainerStatuses") or []) + (status.get("containerStatuses") or []):
        name = c.get("name", "?")
        state = c.get("state") or {}
        last = (c.get("lastState") or {}).get("terminated") or {}
        if "waiting" in state:
            w = state["waiting"] or {}
            notes.append(f"{name}: waiting {w.get('reason', '')} {w.get('message', '')}".rstrip())
        elif "terminated" in state:
            t = state["terminated"] or {}
            notes.append(f"{name}: terminated {t.get('reason', '')} exit {t.get('exitCode')}")
        if last:
            notes.append(
                f"{name}: last run {last.get('reason', '')} exit {last.get('exitCode')} "
                f"(restarts {c.get('restartCount', 0)})"
            )
    return notes


def _release_pods(settings: DeploySettings, target: Target) -> list[dict[str, Any]]:
    """The release's pods (``-l app.kubernetes.io/instance=<release>``); ``[]`` when unreadable."""
    selector = f"app.kubernetes.io/instance={settings.release}"
    try:
        listing = _kube.run_cmd(
            _kube.kubectl_args(["get", "pods", "-l", selector, "-o", "json"], target),
            check=False,
            quiet=True,
        )
        pods = json.loads(listing.stdout or "{}").get("items") or []
    except (_kube.ToolFailed, json.JSONDecodeError, AttributeError):
        return []
    return [p for p in pods if isinstance(p, dict)]


def _release_objects(settings: DeploySettings, target: Target) -> set[tuple[str, str]]:
    """``(kind, name)`` of the release's objects that warning events are about.

    The Deployment, its ReplicaSets and pods, and the bundled database's
    StatefulSet, pods and volume claims: everything labelled with the release.
    """
    selector = f"app.kubernetes.io/instance={settings.release}"
    objects = {("Deployment", settings.release)}
    try:
        listing = _kube.run_cmd(
            _kube.kubectl_args(
                [
                    "get",
                    "pods,replicasets,deployments,statefulsets,persistentvolumeclaims",
                    "-l",
                    selector,
                    "-o",
                    "json",
                ],
                target,
            ),
            check=False,
            quiet=True,
        )
        items = json.loads(listing.stdout or "{}").get("items") or []
    except (_kube.ToolFailed, json.JSONDecodeError, AttributeError):
        items = []
    for item in items:
        if isinstance(item, dict):
            name = (item.get("metadata") or {}).get("name")
            if item.get("kind") and name:
                objects.add((str(item["kind"]), str(name)))
    return objects


def _parse_time(value: object) -> _dt.datetime | None:
    if not value:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_dt.UTC)


def _event_time(event: dict[str, Any]) -> _dt.datetime | None:
    """When a (possibly repeated) event was last seen."""
    series = event.get("series") or {}
    for value in (
        event.get("lastTimestamp"),
        series.get("lastObservedTime") if isinstance(series, dict) else None,
        event.get("eventTime"),
        event.get("firstTimestamp"),
        (event.get("metadata") or {}).get("creationTimestamp"),
    ):
        parsed = _parse_time(value)
        if parsed is not None:
            return parsed
    return None


def _age(when: _dt.datetime | None) -> str:
    if when is None:
        return "?"
    seconds = max(0, int((_now() - when).total_seconds()))
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 7200:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def _print_warning_events(
    settings: DeploySettings,
    target: Target,
    *,
    since: _dt.datetime | None,
    console: Console,
) -> None:
    """Warning events about this release's objects, newer than ``since`` (less clock skew).

    The namespace may hold other workloads and events from earlier rollouts
    (events live for an hour): only this release's objects are shown, and with
    ``since`` only what happened during this run.
    """
    cmd = _kube.kubectl_args(
        ["get", "events", "--field-selector", "type=Warning", "-o", "json"], target
    )
    console.print(f"  $ {_kube.format_cmd(cmd)}", style="dim", markup=False, highlight=False)
    try:
        result = _kube.run_cmd(cmd, check=False, quiet=True)
        events = json.loads(result.stdout or "{}").get("items") or []
    except (_kube.ToolFailed, json.JSONDecodeError, AttributeError) as e:
        console.print(f"    (could not list events: {_first_line(e) or 'no output'})", markup=False)
        return
    objects = _release_objects(settings, target)
    cutoff = since - _dt.timedelta(seconds=EVENT_CLOCK_SKEW_S) if since else None
    mine: list[tuple[_dt.datetime | None, dict[str, Any]]] = []
    older = 0
    for event in events:
        if not isinstance(event, dict):
            continue
        involved = event.get("involvedObject") or event.get("regarding") or {}
        if (str(involved.get("kind")), str(involved.get("name"))) not in objects:
            continue
        when = _event_time(event)
        if cutoff is not None and (when is None or when < cutoff):
            older += 1
            continue
        mine.append((when, event))
    mine.sort(key=lambda pair: pair[0] or _dt.datetime.min.replace(tzinfo=_dt.UTC))
    if not mine:
        console.print("    (no warning events for this release)", markup=False)
    for when, event in mine[-DIAGNOSTIC_EVENTS:]:
        involved = event.get("involvedObject") or event.get("regarding") or {}
        count = event.get("count") or ((event.get("series") or {}).get("count"))
        times = f" (x{count})" if isinstance(count, int) and count > 1 else ""
        message = " ".join(str(event.get("message") or event.get("note") or "").split())
        console.print(
            f"    {_age(when):>4} ago  {involved.get('kind')}/{involved.get('name')}  "
            f"{event.get('reason', '')}: {message}{times}",
            markup=False,
            highlight=False,
        )
    if older:
        console.print(
            f"    ({older} older warning event(s) for this release, from before this run, not "
            "shown)",
            style="dim",
            markup=False,
        )


def _print_rollout_diagnostics(
    settings: DeploySettings,
    target: Target,
    *,
    since: _dt.datetime | None = None,
    headline: str | None = None,
    console: Console,
) -> None:
    """Pods, container states, this release's warning events and recent logs (best effort)."""
    selector = f"app.kubernetes.io/instance={settings.release}"
    console.print(
        headline or f"Rollout of {settings.release} in {target.namespace} failed; diagnostics:",
        style="yellow",
    )
    _diag(console, ["get", "pods", "-l", selector, "-o", "wide"], target)
    pods = _release_pods(settings, target)
    # A terminating pod belongs to the revision being replaced: not what failed.
    failing = [p for p in pods if not _pod_ready(p) and not _terminating(p)]
    for pod in failing[:DIAGNOSTIC_PODS]:
        name = (pod.get("metadata") or {}).get("name", "?")
        for note in _container_notes(pod):
            console.print(f"  {name}: {note}", markup=False, highlight=False)
    _print_warning_events(settings, target, since=since, console=console)
    for pod in failing[:DIAGNOSTIC_PODS]:
        name = (pod.get("metadata") or {}).get("name", "?")
        logs = _diag(
            console,
            ["logs", name, "--all-containers", f"--tail={DIAGNOSTIC_LOG_LINES}"],
            target,
        )
        restarted = any(
            int(c.get("restartCount") or 0) > 0
            for c in (pod.get("status") or {}).get("containerStatuses") or []
        )
        if restarted and not logs.strip():
            _diag(
                console,
                ["logs", name, "--all-containers", "--previous", f"--tail={DIAGNOSTIC_LOG_LINES}"],
                target,
            )


_HEALTHY = ("deployed", "superseded")
_PENDING = "pending-"
# helm's refusal while the release's newest revision is pending-install/-upgrade/-rollback.
_HELM_BUSY = "another operation (install/upgrade/rollback) is in progress"


def _status(revision: dict[str, Any]) -> str:
    return str(revision.get("status", "")).strip().lower()


@dataclass(frozen=True)
class _History:
    """``helm history`` of the release; ``readable`` is False when helm could not read it."""

    revisions: tuple[dict[str, Any], ...] = ()
    readable: bool = True

    @property
    def latest(self) -> int:
        """The newest revision number (0 when the release has none)."""
        return max((int(r["revision"]) for r in self.revisions), default=0)

    def newer_than(self, revision: int) -> list[dict[str, Any]]:
        """Revisions after ``revision``, oldest first."""
        newer = [r for r in self.revisions if int(r["revision"]) > revision]
        return sorted(newer, key=lambda r: int(r["revision"]))

    @property
    def pending(self) -> dict[str, Any] | None:
        """The newest revision when helm is (or was, if interrupted) still working on it."""
        newest = max(self.revisions, key=lambda r: int(r["revision"]), default=None)
        return newest if newest is not None and _status(newest).startswith(_PENDING) else None

    @property
    def installed(self) -> bool:
        """Whether the release exists (``uninstall --keep-history`` leaves uninstalled ones)."""
        return any(_status(r) != "uninstalled" for r in self.revisions)


def _release_history(settings: DeploySettings, target: Target, *, console: Console) -> _History:
    """``helm history`` of the release: empty when it does not exist, unreadable on other errors."""
    try:
        history = _kube.helm(
            ["history", settings.release, "-o", "json"],
            target,
            check=False,
            quiet=True,
            console=console,
        )
    except _kube.ToolFailed:
        return _History(readable=False)
    if history.returncode != 0:
        # `Error: release: not found` means no release yet; anything else is unknown.
        return _History(readable="release: not found" in (history.stderr or ""))
    try:
        revisions = json.loads(history.stdout or "[]")
    except json.JSONDecodeError:
        return _History(readable=False)
    if not isinstance(revisions, list):
        return _History(readable=False)
    return _History(
        tuple(r for r in revisions if isinstance(r, dict) and str(r.get("revision", "")).isdigit())
    )


def _clear_hint(settings: DeploySettings, target: Target, history: _History) -> str:
    """The command that clears a release left pending by an interrupted helm."""
    release, ns = settings.release, target.namespace
    pending = history.pending
    below = int(pending["revision"]) if pending is not None else history.latest + 1
    good = [
        int(r["revision"])
        for r in history.revisions
        if _status(r) in _HEALTHY and int(r["revision"]) < below
    ]
    if good:
        return f"`helm rollback {release} {max(good)} -n {ns}`"
    if pending is not None and _status(pending) == "pending-install":
        return f"`helm uninstall {release} -n {ns}` (the first install never finished)"
    return f"`helm rollback {release} <last good revision> -n {ns}`"


def _busy_message(
    settings: DeploySettings, target: Target, history: _History, *, changed: str
) -> str:
    release, ns = settings.release, target.namespace
    pending = history.pending
    what = f" (revision {pending['revision']} is {_status(pending)})" if pending else ""
    return (
        f"Another helm operation (install/upgrade/rollback) is in progress on {release} in "
        f"{ns}{what}; {changed}, and nothing was rolled back.\n"
        f"  Wait for it to finish (`helm history {release} -n {ns}`) and deploy again. If no "
        "other deploy is running, an interrupted helm left the release pending: "
        f"{_clear_hint(settings, target, history)} clears it."
    )


def _refuse_if_busy(
    settings: DeploySettings, target: Target, history: _History, *, changed: str
) -> None:
    """Exit 2 when another helm operation holds the release (helm itself would refuse)."""
    if history.pending is not None:
        raise _kube.ToolFailed(_busy_message(settings, target, history, changed=changed))


def _check_release_idle(
    settings: DeploySettings, target: Target, *, dry_run: bool, console: Console
) -> None:
    """Stop before anything is built or applied while another deploy is rolling out."""
    if dry_run:
        return
    history = _release_history(settings, target, console=console)
    _refuse_if_busy(settings, target, history, changed="nothing was built, applied or deployed")


def _roll_back(
    settings: DeploySettings,
    target: Target,
    history: _History,
    failed: int,
    *,
    existed: bool,
    timeout: str,
    console: Console,
) -> tuple[str, bool]:
    """Undo this run's failed revision ``failed`` the way helm's ``--atomic`` would; describe it.

    Back to the newest deployed or superseded revision before it; with none, a
    release this run installed (``existed`` False) is uninstalled, and one that
    existed before is left in place (helm's ``--atomic`` does the same).
    """
    release = settings.release
    good = [r for r in history.revisions if _status(r) in _HEALTHY and int(r["revision"]) < failed]
    if good:
        revision = str(max(int(r["revision"]) for r in good))
        console.print(f"Rolling back {release} to revision {revision} (--atomic).", style="yellow")
        result = _kube.helm(
            ["rollback", release, revision, "--wait", "--timeout", timeout],
            target,
            capture=False,
            check=False,
            console=console,
        )
        if result.returncode == 0:
            return f"rolled back to revision {revision}", True
        return (
            f"the rollback to revision {revision} also failed (exit code {result.returncode}); "
            f"check `helm history {release} -n {target.namespace}`"
        ), False
    if existed:
        return (
            f"there is no earlier successful revision to roll back to, so the failed revision "
            f"{failed} was left in place; fix the cause and deploy again, or remove it with "
            f"`helm uninstall {release} -n {target.namespace}`"
        ), False
    console.print(
        f"Uninstalling {release}: the first install never succeeded (--atomic).", style="yellow"
    )
    result = _kube.helm(
        ["uninstall", release, "--wait", "--timeout", timeout],
        target,
        capture=False,
        check=False,
        console=console,
    )
    if result.returncode == 0:
        return "the failed first install was uninstalled (there was no earlier revision)", True
    return (
        f"uninstalling the failed first install also failed (exit code {result.returncode}); "
        f"check `helm status {release} -n {target.namespace}`"
    ), False


def _print_done(
    settings: DeploySettings, env: str, image: str, *, dry_run: bool, console: Console
) -> None:
    verb = "Would deploy" if dry_run else "Deployed"
    console.print(f"{verb} {settings.release} ({image}) to {env}.", style="green")


# --------------------------------------------------------------------------- status / restart


class NotReady(_kube.DeployError):
    """``deploy --status``: the rollout is not complete within ``--timeout`` (exit 1)."""

    exit_code = 1


_ROLLOUT_OK = "ok"
_ROLLOUT_NOT_READY = "not-ready"
_ROLLOUT_ABSENT = "absent"


def _rollout_status_cmd(settings: DeploySettings, target: Target, timeout: str) -> list[str]:
    return _kube.kubectl_args(
        ["rollout", "status", f"deployment/{settings.release}", f"--timeout={timeout}"], target
    )


def _wait_for_rollout(
    settings: DeploySettings, target: Target, *, timeout: str, console: Console
) -> tuple[str, str]:
    """``kubectl rollout status --timeout``: ``(state, kubectl's last line)``, never unbounded.

    A timeout or an exceeded progress deadline is ``not-ready`` and a missing
    Deployment ``absent``; any other kubectl failure (cluster unreachable,
    credentials) is a tool failure (exit 2).
    """
    cmd = _rollout_status_cmd(settings, target, timeout)
    _kube.echo_cmd(cmd, console=console)
    console.print(
        f"  Waiting up to {timeout} for deployment/{settings.release} to roll out.", style="dim"
    )
    result = _kube.run_cmd(cmd, check=False, quiet=True)
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    last = next((line for line in reversed((err or out).splitlines()) if line.strip()), "")
    if result.returncode == 0:
        return _ROLLOUT_OK, last
    if "(NotFound)" in err or "not found" in err.lower():
        return _ROLLOUT_ABSENT, last
    if "timed out" in err or "progress deadline" in err:
        return _ROLLOUT_NOT_READY, last
    raise _kube.ToolFailed(
        f"Command failed (exit code {result.returncode}): {_kube.format_cmd(cmd)}"
        + (f"\n{err or out}" if err or out else "")
    )


def _print_workload_summary(settings: DeploySettings, target: Target, *, console: Console) -> None:
    """The Deployment's replicas and image, the helm revision, and every pod's readiness."""
    release = settings.release
    try:
        result = _kube.kubectl(
            ["get", "deployment", release, "-o", "json"], target, check=False, quiet=True
        )
        deployment = json.loads(result.stdout or "null") if result.returncode == 0 else None
    except (_kube.ToolFailed, json.JSONDecodeError):
        deployment = None
    if isinstance(deployment, dict):
        spec = deployment.get("spec") or {}
        status = deployment.get("status") or {}
        containers = ((spec.get("template") or {}).get("spec") or {}).get("containers") or []
        agent = next((c for c in containers if c.get("name") == "agent"), None) or (
            containers[0] if containers else {}
        )
        wanted = spec.get("replicas", 1)
        console.print(
            f"deployment/{release}: {status.get('readyReplicas') or 0}/{wanted} ready, "
            f"{status.get('updatedReplicas') or 0} up to date, image {agent.get('image', '?')}",
            markup=False,
            highlight=False,
        )
        for condition in status.get("conditions") or []:
            if str(condition.get("status")) != "True":
                console.print(
                    f"  {condition.get('type')}: {condition.get('reason', '')} "
                    f"{condition.get('message', '')}".rstrip(),
                    markup=False,
                    highlight=False,
                )
    if settings.cd != _modes.ARGOCD:
        history = _release_history(settings, target, console=console)
        if history.readable and history.revisions:
            newest = max(history.revisions, key=lambda r: int(r["revision"]))
            console.print(
                f"helm release {release}: revision {newest['revision']} ({_status(newest)})",
                markup=False,
            )
    for pod in _release_pods(settings, target):
        name = (pod.get("metadata") or {}).get("name", "?")
        statuses = (pod.get("status") or {}).get("containerStatuses") or []
        restarts = sum(int(c.get("restartCount") or 0) for c in statuses)
        last = next(
            (
                (c.get("lastState") or {}).get("terminated")
                for c in statuses
                if (c.get("lastState") or {}).get("terminated")
            ),
            None,
        )
        why = f" (last exit: {last.get('reason', '')} {last.get('exitCode')})" if last else ""
        if _terminating(pod):
            state = "terminating"
        else:
            state = "ready" if _pod_ready(pod) else "NOT ready"
        console.print(
            f"  pod {name}: {state}, restarts {restarts}{why}", markup=False, highlight=False
        )


def _show_status(
    settings: DeploySettings,
    env: str,
    target: Target,
    *,
    timeout: str,
    dry_run: bool,
    console: Console,
) -> None:
    """Report the rollout within ``timeout``: exit 1 (with diagnostics) when it is not complete."""
    if settings.cd == _modes.ARGOCD and _kube.tool_available("argocd"):
        _kube.run_cmd(
            ["argocd", "app", "get", f"{settings.project_name}-{env}"],
            capture=False,
            dry_run=dry_run,
            console=console,
        )
        return
    if dry_run:
        _kube.echo_cmd(
            _rollout_status_cmd(settings, target, timeout), dry_run=True, console=console
        )
        return
    state, detail = _wait_for_rollout(settings, target, timeout=timeout, console=console)
    where = f"deployment/{settings.release} in {target.namespace}"
    if state == _ROLLOUT_ABSENT:
        raise NotReady(f"{where} does not exist (not deployed yet?): {detail}")
    _print_workload_summary(settings, target, console=console)
    if state == _ROLLOUT_OK:
        console.print(f"{where}: rollout complete.", style="green", markup=False)
        return
    _print_rollout_diagnostics(
        settings,
        target,
        headline=f"{where} is not ready after {timeout}; diagnostics:",
        console=console,
    )
    raise NotReady(
        f"The rollout of {where} did not complete within {timeout} ({detail}). Pass a longer "
        "--timeout to wait more; the diagnostics above show why the pods are not ready."
    )


def _restart(
    settings: DeploySettings,
    env: str,
    target: Target,
    *,
    timeout: str,
    dry_run: bool,
    console: Console,
) -> None:
    """``kubectl rollout restart``, then wait (bounded) until the new pods are ready."""
    if settings.cd == _modes.ARGOCD:
        console.print(
            "  Warning: this environment is reconciled by Argo CD with self-heal; the restart "
            "annotation may be reverted. Prefer an Argo resource action (restart) on the Deployment.",
            style="yellow",
        )
    where = f"deployment/{settings.release} in {target.namespace}"
    _kube.kubectl(
        ["rollout", "restart", f"deployment/{settings.release}"],
        target,
        capture=False,
        dry_run=dry_run,
        console=console,
    )
    if dry_run:
        _kube.echo_cmd(
            _rollout_status_cmd(settings, target, timeout), dry_run=True, console=console
        )
        console.print(f"Would restart {where} and wait up to {timeout} for the new pods.")
        return
    started = _now()
    state, detail = _wait_for_rollout(settings, target, timeout=timeout, console=console)
    _print_workload_summary(settings, target, console=console)
    if state == _ROLLOUT_OK:
        console.print(f"Restarted {where}: the new pods are ready.", style="green", markup=False)
        return
    _print_rollout_diagnostics(
        settings,
        target,
        since=started,
        headline=f"The restart of {where} did not finish within {timeout}; diagnostics:",
        console=console,
    )
    raise _kube.ToolFailed(
        f"The restarted pods of {where} did not become ready within {timeout} ({detail}).\n"
        "  The pods that were running keep serving until new ones are ready. Fix the cause "
        f"(often a Secret value: `graph-agents-cli secrets status --env {env}`) and restart "
        f"again, or go back to the previous pods with `kubectl rollout undo "
        f"deployment/{settings.release} -n {target.namespace}`."
    )
