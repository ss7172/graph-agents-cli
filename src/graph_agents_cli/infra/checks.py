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
"""Read-only prerequisite checks for ``infra check`` (D10, D12, D25).

Every check reports; nothing here creates or changes cluster or GitHub state.
Cluster checks shell out to ``kubectl`` with ``check=False`` so a missing
prerequisite is a row in the table, never an exception.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from graph_agents_cli.deploy import _kube, _modes, gitops
from graph_agents_cli.deploy._config import DeploySettings
from graph_agents_cli.deploy._kube import Target, ToolFailed
from graph_agents_cli.deploy._values import load_chart_values

MIN_KUBERNETES = (1, 28)
DISCONNECTED = "disconnected"
PROFILES = (DISCONNECTED,)

OK = "ok"
MISSING = "missing"
WARN = "warn"
SKIP = "skip"
INFO = "info"

HOSTED_REGISTRY_HOSTS = ("ghcr.io", "docker.io", "index.docker.io", "gcr.io", "quay.io")
HOSTED_REGISTRY_SUFFIXES = (".amazonaws.com", ".pkg.dev", ".azurecr.io", ".gcr.io")


@dataclass
class Check:
    name: str
    status: str
    required: bool
    detail: str = ""
    hint: str = ""

    @property
    def failed(self) -> bool:
        return self.required and self.status == MISSING

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Report:
    env: str | None
    profile: str | None
    mode: str | None
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(c.failed for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "env": self.env,
            "profile": self.profile,
            "mode": self.mode,
            "ok": self.ok,
            "checks": [c.to_dict() for c in self.checks],
        }


def _kubectl(args: list[str], target: Target | None, *, namespaced: bool = False) -> Any:
    """kubectl with a parsed JSON result: ``(returncode, data|None, stderr)``; never raises."""
    try:
        result = _kube.kubectl(
            [*args, "-o", "json"], target, namespaced=namespaced, check=False, quiet=True
        )
    except ToolFailed as e:
        return 1, None, str(e)
    data = None
    if result.returncode == 0 and result.stdout.strip():
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            data = None
    return result.returncode, data, (result.stderr or "").strip()


def _names(listing: Any) -> list[str]:
    if not isinstance(listing, dict):
        return []
    return [
        str(item.get("metadata", {}).get("name", ""))
        for item in listing.get("items", [])
        if isinstance(item, dict)
    ]


def _minor(version: str) -> int:
    digits = re.match(r"\d+", version or "")
    return int(digits.group()) if digits else 0


def _enabled(values: dict[str, Any], *path: str, default: bool = False) -> bool:
    node: Any = values
    for key in path:
        if not isinstance(node, dict):
            return default
        node = node.get(key)
    if node is None:
        return default
    return bool(node)


def _get(values: dict[str, Any], *path: str, default: Any = None) -> Any:
    node: Any = values
    for key in path:
        if not isinstance(node, dict):
            return default
        node = node.get(key)
    return default if node is None else node


# --------------------------------------------------------------------------- checks


def check_tools(settings: DeploySettings, mode: str) -> list[Check]:
    checks: list[Check] = []
    needs = {
        "kubectl": True,
        "helm": mode != _modes.ARGOCD,
        "docker": mode in _modes.DIRECT_MODES or (mode == _modes.HELM_PUSH),
        "gh": settings.cd != "skip",
    }
    hints = {
        "kubectl": "https://kubernetes.io/docs/tasks/tools/",
        "helm": "https://helm.sh/docs/intro/install/",
        "docker": "install Docker (or a docker-compatible CLI) to build images locally",
        "gh": "https://cli.github.com/",
    }
    for tool, required in needs.items():
        present = _kube.tool_available(tool)
        checks.append(
            Check(
                f"tool: {tool}",
                OK if present else MISSING,
                required,
                "found" if present else "not on PATH",
                "" if present else hints[tool],
            )
        )
    if settings.cd == _modes.ARGOCD:
        present = _kube.tool_available("argocd")
        checks.append(
            Check(
                "tool: argocd",
                OK if present else INFO,
                False,
                "found" if present else "not on PATH; `deploy --status` falls back to kubectl",
                "" if present else "https://argo-cd.readthedocs.io/en/stable/cli_installation/",
            )
        )
    return checks


def check_cluster(
    settings: DeploySettings, env: str, target: Target, values: dict[str, Any]
) -> list[Check]:
    checks: list[Check] = []
    rc, version, err = _kubectl(["version"], target)
    server = version.get("serverVersion") if isinstance(version, dict) else None
    if rc != 0 or not isinstance(server, dict):
        checks.append(
            Check(
                "cluster reachable",
                MISSING,
                True,
                err.splitlines()[0]
                if err
                else f"kubectl could not reach context {target.context or '(current)'}",
                "check the kube context recorded in environments.<env>.context and your kubeconfig",
            )
        )
        return checks
    git_version = str(server.get("gitVersion", ""))
    checks.append(
        Check(
            "cluster reachable",
            OK,
            True,
            f"context {target.context or '(current)'}, server {git_version}",
        )
    )
    major, minor = _minor(str(server.get("major", "0"))), _minor(str(server.get("minor", "0")))
    new_enough = (major, minor) >= MIN_KUBERNETES
    checks.append(
        Check(
            f"kubernetes >= {MIN_KUBERNETES[0]}.{MIN_KUBERNETES[1]}",
            OK if new_enough else MISSING,
            True,
            f"server {major}.{minor}",
            ""
            if new_enough
            else "upgrade the cluster; the chart uses gateway.networking.k8s.io/v1",
        )
    )

    gateway_on = _enabled(values, "gateway", "enabled", default=True)
    rc, _data, _ = _kubectl(["get", "crd", "httproutes.gateway.networking.k8s.io"], target)
    have_gateway_crd = rc == 0
    checks.append(
        Check(
            "gateway api crds",
            OK if have_gateway_crd else MISSING,
            gateway_on,
            "httproutes.gateway.networking.k8s.io present"
            if have_gateway_crd
            else "HTTPRoute CRD absent",
            ""
            if have_gateway_crd
            else "install the Gateway API CRDs (https://gateway-api.sigs.k8s.io/guides/) or set ingress.enabled=true",
        )
    )
    class_names: list[str] = []
    if have_gateway_crd:
        rc, listing, _ = _kubectl(["get", "gatewayclass"], target)
        class_names = _names(listing) if rc == 0 else []
    wanted = str(_get(values, "gateway", "className", default="") or "")
    if wanted:
        found = wanted in class_names
        checks.append(
            Check(
                "gateway class",
                OK if found else MISSING,
                gateway_on,
                f"{wanted} {'found' if found else 'not found'}; available: {', '.join(class_names) or 'none'}",
                ""
                if found
                else "set gateway.className to one of the GatewayClasses in the cluster",
            )
        )
    else:
        checks.append(
            Check(
                "gateway classes",
                OK if class_names else MISSING,
                gateway_on,
                ", ".join(class_names) if class_names else "no GatewayClass in the cluster",
                ""
                if class_names
                else "install a Gateway API implementation (Envoy Gateway, Cilium, Istio, Traefik, NGINX Gateway Fabric, Kong) and set gateway.className",
            )
        )
    # The chart's HTTPRoute marks gateway.parentRef.name as `required`, and `deploy`
    # refuses (exit 3) before any tool runs when it is blank; report it here so the
    # operator sees it before the first deploy (CONTRACTS section 8).
    parent_name = str(_get(values, "gateway", "parentRef", "name", default="") or "").strip()
    checks.append(
        Check(
            "gateway parentRef",
            OK if parent_name else MISSING,
            gateway_on,
            f"gateway.parentRef.name = {parent_name}"
            if parent_name
            else "gateway.parentRef.name is blank",
            ""
            if parent_name
            else "set gateway.parentRef.name to the Gateway to attach to in values-<env>.yaml (or set gateway.enabled: false / ingress.enabled: true)",
        )
    )

    ingress_on = _enabled(values, "ingress", "enabled")
    rc, listing, _ = _kubectl(["get", "ingressclass"], target)
    ingress_classes = _names(listing) if rc == 0 else []
    ingress_wanted = str(_get(values, "ingress", "className", default="") or "")
    detail = ", ".join(ingress_classes) if ingress_classes else "no IngressClass in the cluster"
    status = OK if ingress_classes else (MISSING if ingress_on else INFO)
    if ingress_wanted and ingress_on:
        status = OK if ingress_wanted in ingress_classes else MISSING
        detail = f"{ingress_wanted} {'found' if status == OK else 'not found'}; available: {', '.join(ingress_classes) or 'none'}"
    checks.append(
        Check(
            "ingress classes",
            status,
            ingress_on,
            detail,
            ""
            if status == OK
            else "select a maintained ingress controller and set ingress.className",
        )
    )

    if _enabled(values, "tls", "certManager", "enabled"):
        rc, _data, _ = _kubectl(["get", "crd", "certificates.cert-manager.io"], target)
        checks.append(
            Check(
                "cert-manager",
                OK if rc == 0 else MISSING,
                True,
                "certificates.cert-manager.io present" if rc == 0 else "CRD absent",
                ""
                if rc == 0
                else "install cert-manager (https://cert-manager.io/docs/installation/) or set tls.certManager.enabled=false",
            )
        )
    else:
        checks.append(
            Check("cert-manager", SKIP, False, "not needed: tls.certManager.enabled is false")
        )

    if _enabled(values, "hpa", "enabled"):
        rc, api, _ = _kubectl(["get", "apiservice", "v1beta1.metrics.k8s.io"], target)
        available = False
        if rc == 0 and isinstance(api, dict):
            for cond in api.get("status", {}).get("conditions", []):
                if cond.get("type") == "Available" and str(cond.get("status")).lower() == "true":
                    available = True
        checks.append(
            Check(
                "metrics-server",
                OK if available else MISSING,
                True,
                "metrics API available" if available else "v1beta1.metrics.k8s.io unavailable",
                ""
                if available
                else "install metrics-server (https://github.com/kubernetes-sigs/metrics-server) or set hpa.enabled=false",
            )
        )
    else:
        checks.append(Check("metrics-server", SKIP, False, "not needed: hpa.enabled is false"))

    if settings.cd == _modes.ARGOCD:
        rc_ns, _ns, _ = _kubectl(["get", "namespace", "argocd"], target)
        rc_crd, _crd, _ = _kubectl(["get", "crd", "applications.argoproj.io"], target)
        ok = rc_ns == 0 and rc_crd == 0
        checks.append(
            Check(
                "argo cd",
                OK if ok else MISSING,
                True,
                "namespace argocd and applications.argoproj.io present"
                if ok
                else f"namespace argocd {'ok' if rc_ns == 0 else 'missing'}, Application CRD {'ok' if rc_crd == 0 else 'missing'}",
                ""
                if ok
                else "install Argo CD (https://argo-cd.readthedocs.io/en/stable/getting_started/)",
            )
        )
    else:
        checks.append(Check("argo cd", SKIP, False, f"not needed: cd is {settings.cd}"))

    rc, _ns, _ = _kubectl(["get", "namespace", target.namespace], target)
    checks.append(
        Check(
            f"namespace {target.namespace}",
            OK if rc == 0 else WARN,
            False,
            "exists"
            if rc == 0
            else "absent (helm --create-namespace creates it; `secrets apply` needs it first in CD modes)",
        )
    )

    pull_secrets: list[str] = []
    for item in _get(values, "imagePullSecrets", default=[]) or []:
        if isinstance(item, dict) and item.get("name"):
            pull_secrets.append(str(item["name"]))
        elif isinstance(item, str):
            pull_secrets.append(item)
    if pull_secrets:
        for name in pull_secrets:
            rc, _s, _ = _kubectl(["get", "secret", name], target, namespaced=True)
            checks.append(
                Check(
                    f"image pull secret {name}",
                    OK if rc == 0 else MISSING,
                    True,
                    f"present in {target.namespace}"
                    if rc == 0
                    else f"absent from {target.namespace}",
                    ""
                    if rc == 0
                    else f"kubectl create secret docker-registry {name} -n {target.namespace} ... (operator prerequisite)",
                )
            )
    else:
        checks.append(Check("image pull secrets", SKIP, False, "none referenced in values"))

    rc, _s, _ = _kubectl(["get", "secret", settings.secret_name], target, namespaced=True)
    cd_mode = settings.cd != "skip"
    checks.append(
        Check(
            f"app secret {settings.secret_name}",
            OK if rc == 0 else (MISSING if cd_mode else WARN),
            cd_mode,
            "present" if rc == 0 else "absent",
            "" if rc == 0 else f"graph-agents-cli secrets apply --env {env} --env-file .env.{env}",
        )
    )
    return checks


def _github_token_available() -> bool:
    if gitops.github_token():
        return True
    if not _kube.tool_available("gh"):
        return False
    try:
        result = _kube.run_cmd(["gh", "auth", "status"], check=False, quiet=True)
    except ToolFailed:
        return False
    return result.returncode == 0


def _gh_api(path: str) -> tuple[int, Any]:
    try:
        result = _kube.run_cmd(["gh", "api", path], check=False, quiet=True)
    except ToolFailed:
        return 1, None
    if result.returncode != 0:
        return result.returncode, None
    try:
        return 0, json.loads(result.stdout or "null")
    except json.JSONDecodeError:
        return 0, None


def check_github(settings: DeploySettings) -> list[Check]:
    """Report (never enforce) the D12 environment protection and branch protection settings."""
    if settings.cd == "skip":
        return [Check("github protection", SKIP, False, "not needed: cd is skip")]
    if not _github_token_available():
        return [
            Check(
                "github protection",
                INFO,
                False,
                "skipped: no GITHUB_TOKEN and gh is not authenticated",
            )
        ]
    if not _kube.tool_available("gh"):
        return [Check("github protection", INFO, False, "skipped: gh CLI not installed")]
    try:
        remote = gitops.parse_remote(gitops.origin_url())
    except Exception as e:
        return [Check("github protection", INFO, False, f"skipped: {e}")]
    checks: list[Check] = []
    repo = f"repos/{remote.owner}/{remote.repo}"

    rc, prod = _gh_api(f"{repo}/environments/production")
    if rc != 0 or not isinstance(prod, dict):
        checks.append(
            Check(
                "github env: production",
                WARN,
                False,
                "environment not found",
                "create the `production` environment with required reviewers, prevent self-review, branches restricted to main",
            )
        )
    else:
        rules = prod.get("protection_rules") or []
        reviewers = next((r for r in rules if r.get("type") == "required_reviewers"), None)
        has_reviewers = bool(reviewers and reviewers.get("reviewers"))
        prevent_self = bool(reviewers and reviewers.get("prevent_self_review"))
        policy = prod.get("deployment_branch_policy") or {}
        restricted = bool(policy.get("protected_branches") or policy.get("custom_branch_policies"))
        detail = (
            f"reviewers: {'yes' if has_reviewers else 'no'}, prevent self-review: {'yes' if prevent_self else 'no'}, "
            f"branches restricted: {'yes' if restricted else 'no'}"
        )
        ok = has_reviewers and prevent_self and restricted
        checks.append(
            Check(
                "github env: production",
                OK if ok else WARN,
                False,
                detail,
                ""
                if ok
                else "D12: at least one required reviewer, prevent self-review, deployment branches restricted to main",
            )
        )

    rc, staging = _gh_api(f"{repo}/environments/staging")
    if rc != 0 or not isinstance(staging, dict):
        checks.append(
            Check(
                "github env: staging",
                WARN,
                False,
                "environment not found",
                "create the `staging` environment with deployment branches restricted to main",
            )
        )
    else:
        policy = staging.get("deployment_branch_policy") or {}
        restricted = bool(policy.get("protected_branches") or policy.get("custom_branch_policies"))
        checks.append(
            Check(
                "github env: staging",
                OK if restricted else WARN,
                False,
                f"branches restricted: {'yes' if restricted else 'no'}",
                "" if restricted else "restrict deployment branches to main",
            )
        )

    rc, prot = _gh_api(f"{repo}/branches/main/protection")
    if rc != 0 or not isinstance(prot, dict):
        checks.append(
            Check(
                "github branch protection: main",
                WARN,
                False,
                "no branch protection on main",
                "D12: require pull requests, code-owner review, dismiss stale approvals, pr_checks as a required status check",
            )
        )
    else:
        reviews = prot.get("required_pull_request_reviews") or {}
        contexts = (prot.get("required_status_checks") or {}).get("contexts") or []
        checks_list = [
            c.get("context")
            for c in (prot.get("required_status_checks") or {}).get("checks") or []
            if isinstance(c, dict)
        ]
        has_pr_checks = any("pr_checks" in str(c) for c in [*contexts, *checks_list])
        code_owners = bool(reviews.get("require_code_owner_reviews"))
        dismiss = bool(reviews.get("dismiss_stale_reviews"))
        ok = bool(reviews) and code_owners and dismiss and has_pr_checks
        detail = (
            f"pull requests: {'yes' if reviews else 'no'}, code-owner review: {'yes' if code_owners else 'no'}, "
            f"dismiss stale: {'yes' if dismiss else 'no'}, pr_checks required: {'yes' if has_pr_checks else 'no'}"
        )
        checks.append(
            Check(
                "github branch protection: main",
                OK if ok else WARN,
                False,
                detail,
                "" if ok else "D12 branch protection settings",
            )
        )
    return checks


def _hosted_registry(registry: str) -> bool:
    host = registry.split("/")[0].lower()
    return host in HOSTED_REGISTRY_HOSTS or host.endswith(HOSTED_REGISTRY_SUFFIXES)


def check_disconnected(settings: DeploySettings, values: dict[str, Any]) -> list[Check]:
    """D25 disconnected profile: fail on every hosted dependency."""
    checks: list[Check] = []
    provider_ok = settings.model_provider == "openai-compatible"
    checks.append(
        Check(
            "disconnected: model provider",
            OK if provider_ok else MISSING,
            True,
            settings.model_provider,
            ""
            if provider_ok
            else "use MODEL_PROVIDER=openai-compatible with OPENAI_BASE_URL pointing at an on-network server",
        )
    )
    base_url = str(_get(values, "env", "OPENAI_BASE_URL", default="") or "")
    checks.append(
        Check(
            "disconnected: OPENAI_BASE_URL",
            OK if base_url else MISSING,
            True,
            base_url or "not set in chart env",
            ""
            if base_url
            else "set env.OPENAI_BASE_URL in values-<env>.yaml to the in-cluster model server",
        )
    )
    runtime_ok = settings.runtime == "fastapi"
    checks.append(
        Check(
            "disconnected: runtime",
            OK if runtime_ok else MISSING,
            True,
            settings.runtime,
            ""
            if runtime_ok
            else "langgraph-server needs a LangSmith license check (ASSUMPTIONS item 1); use runtime fastapi",
        )
    )
    hosted = bool(settings.registry) and _hosted_registry(settings.registry)
    checks.append(
        Check(
            "disconnected: registry",
            MISSING if hosted else OK,
            True,
            settings.registry or "(none)",
            ""
            if not hosted
            else "mirror images into an on-network registry (Harbor, registry:2) and set create_params.registry",
        )
    )
    tracing_on = _enabled(values, "tracing", "enabled")
    otlp = str(_get(values, "tracing", "otlpEndpoint", default="") or "")
    langsmith = tracing_on and not otlp
    checks.append(
        Check(
            "disconnected: tracing",
            MISSING if langsmith else OK,
            True,
            "LangSmith (hosted)" if langsmith else ("OTLP " + otlp if tracing_on else "off"),
            ""
            if not langsmith
            else "set tracing.otlpEndpoint to an in-cluster collector or disable tracing",
        )
    )
    if settings.cd == "skip":
        checks.append(
            Check(
                "disconnected: ci/cd",
                OK,
                True,
                "cd is skip: lifecycle covered by direct-mode deploy",
            )
        )
    elif gitops.enterprise_hosts():
        checks.append(
            Check(
                "disconnected: ci/cd",
                WARN,
                False,
                f"cd is {settings.cd} on GitHub Enterprise Server {', '.join(sorted(gitops.enterprise_hosts()))} (ASSUMPTIONS item 24)",
            )
        )
    else:
        checks.append(
            Check(
                "disconnected: ci/cd",
                MISSING,
                True,
                f"cd is {settings.cd} on GitHub-hosted CI",
                "GitHub-hosted Actions are outside the profile; use cd: skip or an on-network GitHub Enterprise Server (set GH_HOST)",
            )
        )
    update_check = os.environ.get("GRAPH_AGENTS_CLI_NO_UPDATE_CHECK") == "1"
    checks.append(
        Check(
            "disconnected: update check",
            OK if update_check else WARN,
            False,
            "GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1"
            if update_check
            else "GRAPH_AGENTS_CLI_NO_UPDATE_CHECK is not set",
            "" if update_check else "export GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1",
        )
    )
    return checks


def run_checks(
    settings: DeploySettings,
    env: str | None,
    profile: str | None = None,
    *,
    context_resolver: Callable[[DeploySettings, str], str | None] = _modes.resolve_context,
) -> Report:
    target: Target | None = None
    mode: str | None = None
    values: dict[str, Any] = load_chart_values(settings.chart_dir, env)
    if env:
        base = settings.target(env)
        target = Target(context=context_resolver(settings, env), namespace=base.namespace)
        mode = _modes.derive_mode(settings.cd, target.context)
    else:
        mode = _modes.derive_mode(settings.cd, None)
    report = Report(env=env, profile=profile, mode=mode)
    report.checks += check_tools(settings, mode)
    if target is not None:
        report.checks += check_cluster(settings, env or "", target, values)
    else:
        report.checks.append(Check("cluster", SKIP, False, "pass --env to check the cluster"))
    report.checks += check_github(settings)
    if profile == DISCONNECTED:
        report.checks += check_disconnected(settings, values)
    return report
