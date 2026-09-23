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
"""Deployment mode derivation (direct, argocd, helm-push) and kube context resolution.

The kube context decides which cluster every kubectl and helm call reaches.
Taking it silently from the kubeconfig's current context means a developer
whose terminal points at production can deploy there by accident (and a prod
rollout can land on a laptop cluster). So only ``dev`` may use the current
context without asking: every other environment needs the context recorded in
the manifest (``environments.<env>.context``) or passed with ``--context``, or
an explicit confirmation (a prompt at a terminal, ``--yes`` otherwise). The
resolved context and its API server are printed before anything happens.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

import click

from graph_agents_cli._output import Console
from graph_agents_cli.deploy import _kube
from graph_agents_cli.deploy._config import DeploySettings
from graph_agents_cli.deploy._kube import ConfigError, Refused

LOCAL_LOAD = "local-load"
REGISTRY = "registry"
HELM_PUSH = "helm-push"
ARGOCD = "argocd"

DIRECT_MODES = (LOCAL_LOAD, REGISTRY)

# Environments with policy gates (auth policy implemented, helm-push CI-only deploys).
PROTECTED_ENVS = ("staging", "prod")
# The one environment that may fall back to `.env` and the current kube context.
DEV_ENV = "dev"

# Kube context names that suggest a single-node dev cluster (kind, k3s, minikube, ...).
DEV_CONTEXT_PREFIXES = ("kind-", "k3d-", "minikube", "k3s")
DEV_CONTEXT_NAMES = ("minikube", "docker-desktop", "rancher-desktop", "orbstack", "k3s")

FLAG = "flag"
MANIFEST = "manifest"
CURRENT = "current"
NONE = "none"


def is_dev_cluster(context: str | None) -> bool:
    """Whether a context *name* looks like a dev cluster (a hint; see ``local_load.detect``)."""
    if not context:
        return False
    name = context.strip()
    return name in DEV_CONTEXT_NAMES or name.startswith(DEV_CONTEXT_PREFIXES)


def is_dev_env(env: str) -> bool:
    return env == DEV_ENV


@dataclass(frozen=True)
class ResolvedContext:
    """The kube context for one command and where it came from."""

    name: str | None
    source: str  # FLAG, MANIFEST, CURRENT or NONE

    @property
    def explicit(self) -> bool:
        return self.source in (FLAG, MANIFEST)

    def describe(self, env: str) -> str:
        return {
            FLAG: "from --context",
            MANIFEST: f"from environments.{env}.context",
            CURRENT: "the kubeconfig's current context",
            NONE: "no context recorded and no current context",
        }[self.source]


def resolve(settings: DeploySettings, env: str, override: str | None = None) -> ResolvedContext:
    """``--context``, else the manifest's ``environments.<env>.context``, else the current one."""
    if override and override.strip():
        return ResolvedContext(override.strip(), FLAG)
    recorded = settings.environment(env).get("context")
    if recorded:
        return ResolvedContext(recorded, MANIFEST)
    current = _kube.current_context()
    if current:
        return ResolvedContext(current, CURRENT)
    return ResolvedContext(None, NONE)


def resolve_context(settings: DeploySettings, env: str) -> str | None:
    """The environment's recorded context, else the kubeconfig's current context."""
    return resolve(settings, env).name


def _interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def announce(
    env: str, resolved: ResolvedContext, *, console: Console, show_server: bool = True
) -> None:
    """Print the context (and its API server) every cluster-touching command will use."""
    server = _kube.server_url(resolved.name) if show_server and resolved.name else None
    where = f"; server {server}" if server else ""
    console.print(
        f"Kube context: {resolved.name or '(none)'} ({resolved.describe(env)}{where})",
        highlight=False,
        markup=False,
    )


def confirm(
    env: str,
    resolved: ResolvedContext,
    *,
    yes: bool,
    dry_run: bool,
    console: Console,
    action: str,
) -> None:
    """Refuse or confirm an implicit context for any environment other than ``dev``.

    ``dev`` and an explicit context (``--context`` or the manifest) pass, once
    the kubeconfig is known to hold that context (checked here, so a typo stops
    the command before anything is built). For another environment an implicit
    current context needs ``--yes`` or a ``y`` at the prompt; with no context at
    all it is a configuration error. ``--dry-run`` never prompts (nothing is
    changed) and only reports.
    """
    if resolved.explicit and resolved.name:
        names = _kube.context_names()
        if names and resolved.name not in names:
            message = (
                f"Kube context {resolved.name!r} ({resolved.describe(env)}) is not in the "
                f"kubeconfig. Known contexts: {', '.join(sorted(names)[:10])}."
            )
            if not dry_run:
                raise ConfigError(message)
            console.print(f"  [dry-run] {message}", style="yellow", markup=False)
    if is_dev_env(env) or resolved.explicit:
        return
    how = (
        f"Record it as environments.{env}.context in graph-agents-cli-manifest.yaml or pass "
        "--context <name>"
    )
    if resolved.name is None:
        if dry_run:
            console.print(
                f"  [dry-run] no kube context for {env}; the real run stops here. {how}.",
                style="yellow",
                markup=False,
            )
            return
        raise ConfigError(f"No kube context for {env}. {how}.")
    if dry_run:
        console.print(
            f"  [dry-run] {env} has no recorded context; the real run asks to confirm "
            f"{resolved.name!r} (or needs --yes).",
            style="yellow",
            markup=False,
        )
        return
    if yes:
        console.print(
            f"  Using the current context {resolved.name!r} for {env} (--yes).",
            style="yellow",
            markup=False,
        )
        return
    if not _interactive():
        raise Refused(
            f"Refusing to {action} {env} on the kubeconfig's current context {resolved.name!r} "
            f"without confirmation.\n  {how}, or pass --yes to accept the current context."
        )
    if not click.confirm(
        f"{action.capitalize()} {env} on context {resolved.name!r}?", default=False
    ):
        raise Refused(f"Aborted: {env} was not changed. {how}.")


def derive_mode(cd: str, context: str | None, *, local: bool | None = None) -> str:
    """The deploy mode for ``cd``; ``local`` (when known) overrides the context-name guess."""
    if cd == "skip":
        is_local = is_dev_cluster(context) if local is None else local
        return LOCAL_LOAD if is_local else REGISTRY
    if cd == HELM_PUSH:
        return HELM_PUSH
    if cd == ARGOCD:
        return ARGOCD
    raise ConfigError(
        f"Unknown cd value {cd!r} in the manifest (expected argocd, helm-push or skip)."
    )


def describe(mode: str) -> str:
    return {
        LOCAL_LOAD: "direct, local-load (build and load the image into the dev cluster)",
        REGISTRY: "direct, registry (build, push, and helm upgrade)",
        HELM_PUSH: "helm-push (CI builds and pushes; deploy runs helm)",
        ARGOCD: "argocd (deploy writes desired state; Argo CD reconciles from main)",
    }.get(mode, mode)
