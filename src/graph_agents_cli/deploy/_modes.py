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
"""Deployment mode derivation (DECISIONS.md D9) and dev-cluster detection."""

from __future__ import annotations

from graph_agents_cli.deploy import _kube
from graph_agents_cli.deploy._config import DeploySettings
from graph_agents_cli.deploy._kube import ConfigError

LOCAL_LOAD = "local-load"
REGISTRY = "registry"
HELM_PUSH = "helm-push"
ARGOCD = "argocd"

DIRECT_MODES = (LOCAL_LOAD, REGISTRY)

# Kube context names that identify a single-node dev cluster (ASSUMPTIONS item 10).
DEV_CONTEXT_PREFIXES = ("kind-", "k3d-", "minikube", "k3s")
DEV_CONTEXT_NAMES = ("minikube", "docker-desktop", "rancher-desktop", "orbstack", "k3s")


def is_dev_cluster(context: str | None) -> bool:
    if not context:
        return False
    name = context.strip()
    return name in DEV_CONTEXT_NAMES or name.startswith(DEV_CONTEXT_PREFIXES)


def resolve_context(settings: DeploySettings, env: str) -> str | None:
    """The environment's recorded context, else the kubeconfig's current context."""
    return settings.environment(env).get("context") or _kube.current_context()


def derive_mode(cd: str, context: str | None) -> str:
    if cd == "skip":
        return LOCAL_LOAD if is_dev_cluster(context) else REGISTRY
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
