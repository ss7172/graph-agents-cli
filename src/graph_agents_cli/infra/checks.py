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
"""Read-only prerequisite checks for ``infra check``: cluster, GitHub gates, disconnected profile.

Every check reports; nothing here creates or changes cluster or GitHub state.
Cluster checks shell out to ``kubectl`` with ``check=False`` so a missing
prerequisite is a row in the table, never an exception.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from graph_agents_cli._defaults import REGISTRY_FIX_COMMAND, REGISTRY_FIX_EFFECT
from graph_agents_cli.deploy import _image, _kube, _modes, _preflight, gitops, local_load
from graph_agents_cli.deploy._config import DeploySettings
from graph_agents_cli.deploy._kube import Target, ToolFailed
from graph_agents_cli.deploy._values import load_chart_values
from graph_agents_cli.secrets import _apply as secrets_apply
from graph_agents_cli.secrets import _required

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

    if _enabled(values, "gateway", "enabled", default=True):
        checks += _gateway_checks(target, values)
    else:
        # Like cert-manager and metrics-server below: nothing to install, no hint.
        checks.append(Check("gateway api", SKIP, False, "not needed: gateway.enabled is false"))

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
            else "absent (`deploy` and `secrets apply` create it; Argo CD with CreateNamespace)",
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

    rc, secret, _ = _kubectl(["get", "secret", settings.secret_name], target, namespaced=True)
    cd_mode = settings.cd != "skip"
    required = _required.required_keys(settings, values)
    present: set[str] = set()
    if isinstance(secret, dict):
        present = set(secret.get("data") or {}) | set(secret.get("stringData") or {})
    missing = [k for k in required if k not in present]
    provision = secrets_apply.provision_hint(env)
    if rc != 0:
        status, detail, hint = MISSING if cd_mode else WARN, "absent", provision
    elif missing and isinstance(secret, dict):
        status = MISSING if cd_mode else WARN
        detail = f"present, missing required key(s): {', '.join(missing)}"
        hint = provision
    else:
        status, detail, hint = (
            OK,
            "present" + (f" with the required key(s) {', '.join(required)}" if required else ""),
            "",
        )
    # In cd skip, `deploy` applies the Secret from the env file itself, then
    # refuses before helm when a required key is still missing.
    checks.append(Check(f"app secret {settings.secret_name}", status, cd_mode, detail, hint))
    checks += _jwt_checks(settings, env, values, present if rc == 0 else set())
    checks += _database_tls_checks(settings, env, values, secret if rc == 0 else None)
    checks += _metrics_token_checks(settings, env, target, values)
    return checks


def _database_tls_checks(
    settings: DeploySettings, env: str, values: dict[str, Any], secret: Any
) -> list[Check]:
    """Outside dev: whether the external database's DSN requires TLS (read, never printed)."""
    if _modes.is_dev_env(env) or not isinstance(secret, dict):
        return []
    checks: list[Check] = []
    data = secret.get("data") or {}
    for key in _preflight.dsn_keys(settings, values):
        raw = data.get(key)
        if not raw:
            continue
        try:
            dsn = base64.b64decode(str(raw)).decode("utf-8", errors="replace")
        except (binascii.Error, ValueError):
            continue
        insecure = _preflight.dsn_without_tls(values, dsn)
        checks.append(
            Check(
                f"database tls ({key})",
                WARN if insecure else OK,
                False,
                "no sslmode=require/verify-ca/verify-full"
                if insecure
                else f"sslmode={_preflight.sslmode(dsn) or 'from PGSSLMODE'}",
                "append ?sslmode=verify-full&sslrootcert=<CA file mounted into the pod> (or "
                "sslrootcert=system) to the DSN, and connect as a least-privileged role"
                if insecure
                else "",
            )
        )
    return checks


def _gateway_checks(target: Target, values: dict[str, Any]) -> list[Check]:
    """Gateway API CRDs, a GatewayClass and the parentRef (only called while the gateway is on)."""
    checks: list[Check] = []
    gateway_on = True
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
    # operator sees it before the first deploy.
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
    return checks


def _jwt_checks(
    settings: DeploySettings, env: str, values: dict[str, Any], secret_keys: set[str]
) -> list[Check]:
    """The ``jwt`` policy's verification settings (a key source; issuer and audience outside dev)."""
    if _preflight.effective_auth_policy(settings, values) != "jwt":
        return []
    findings = _preflight.jwt_findings(settings, env, values, secret_keys)
    name = "auth: jwt settings"
    if not findings:
        return [Check(name, OK, findings.strict, "verification key (and issuer and audience) set")]
    hint = f"set them in {findings.where}"
    if findings.strict:
        detail = "; ".join(findings.problems) + " (the pods refuse to start)"
        return [Check(name, MISSING, True, detail, hint)]
    detail = "; ".join(findings.problems) + " (the pods answer every request with 503)"
    return [Check(name, WARN, False, detail, hint)]


def _metrics_token_checks(
    settings: DeploySettings, env: str, target: Target, values: dict[str, Any]
) -> list[Check]:
    """The ServiceMonitor's token Secret, when the ServiceMonitor sends one."""
    monitor = _get(values, "metrics", "serviceMonitor", default={}) or {}
    bearer = monitor.get("bearerToken") if isinstance(monitor, dict) else None
    if not (
        _enabled(values, "metrics", "serviceMonitor", "enabled")
        and isinstance(bearer, dict)
        and bearer.get("enabled")
    ):
        return []
    name = str(bearer.get("secretName") or settings.metrics_secret_name)
    key = str(bearer.get("key") or secrets_apply.METRICS_TOKEN_KEY)
    rc, secret, _ = _kubectl(["get", "secret", name], target, namespaced=True)
    present = set((secret or {}).get("data") or {}) if isinstance(secret, dict) else set()
    ok = rc == 0 and key in present
    hint = ""
    if not ok:
        hint = (
            f"add {secrets_apply.METRICS_TOKEN_KEY} to secrets.keys and the env file, then "
            f"{secrets_apply.provision_hint(env)} (it writes {settings.metrics_secret_name})"
            if name == settings.metrics_secret_name
            else f"create Secret {name} with the key {key} in {target.namespace}"
        )
    return [
        Check(
            f"metrics token secret {name}",
            OK if ok else WARN,
            False,
            f"present with {key}"
            if ok
            else (f"absent from {target.namespace}" if rc != 0 else f"has no key {key}")
            + ": the ServiceMonitor's scrapes get 401",
            hint,
        )
    ]


def _codeowners_placeholders() -> list[str]:
    path = Path(".github") / "CODEOWNERS"
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return [
        line.strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#") and _image.has_placeholder(line)
    ]


def _argocd_placeholders() -> list[str]:
    """``deployment/argocd/*.yaml`` files whose ``repoURL`` still holds the placeholder."""
    import yaml

    found: list[str] = []
    for path in sorted((Path("deployment") / "argocd").glob("*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        spec = doc.get("spec") if isinstance(doc, dict) else None
        if not isinstance(spec, dict):
            continue
        sources = [spec.get("source"), *(spec.get("sources") or [])]
        urls = [str(s.get("repoURL") or "") for s in sources if isinstance(s, dict)]
        if any(_image.has_placeholder(url) for url in urls):
            found.append(str(path))
    return found


def check_placeholders(
    settings: DeploySettings, values: dict[str, Any], env: str | None = None
) -> list[Check]:
    """Scaffold placeholders (``CHANGE-ME``) that break a build, a rollout or the production gate.

    A placeholder in the chart env is what ``deploy`` refuses outside dev and
    warns about in dev (the pods would call it); ``env`` selects which.
    """
    checks: list[Check] = []
    registry = settings.registry
    problem = (
        _image.reference_problem(
            f"{registry.rstrip('/')}/{settings.project_name}", None, registry=registry
        )
        if registry
        else "create_params.registry is empty"
    )
    checks.append(
        Check(
            "placeholder: registry",
            MISSING if problem else OK,
            True,
            registry if not problem else problem.splitlines()[0],
            ""
            if not problem
            else f"run `{REGISTRY_FIX_COMMAND}` ({REGISTRY_FIX_EFFECT}), or set "
            "create_params.registry in graph-agents-cli-manifest.yaml (e.g. ghcr.io/<org>)",
        )
    )
    repository = str(_get(values, "image", "repository", default="") or "")
    argocd = settings.cd == _modes.ARGOCD
    chart_placeholder = _image.has_placeholder(repository)
    checks.append(
        Check(
            "placeholder: chart image.repository",
            (MISSING if argocd else WARN) if chart_placeholder else OK,
            argocd,
            repository or "(unset)",
            ""
            if not chart_placeholder
            else f"run `{REGISTRY_FIX_COMMAND}`, or set image.repository in the chart's "
            "values.yaml (Argo CD renders it as is; `deploy` overrides it with --set)",
        )
    )
    env_keys = _preflight.env_placeholders(values)
    env_required = not (env and _modes.is_dev_env(env))
    checks.append(
        Check(
            "placeholder: chart env",
            (MISSING if env_required else WARN) if env_keys else OK,
            env_required,
            f"CHANGE-ME in env.{', env.'.join(env_keys)}" if env_keys else "none",
            ""
            if not env_keys
            else "replace the placeholder URLs in values.yaml / values-<env>.yaml",
        )
    )
    owners = _codeowners_placeholders()
    gated = settings.cd != "skip"
    checks.append(
        Check(
            "placeholder: CODEOWNERS",
            (MISSING if gated else WARN) if owners else OK,
            gated,
            f"{len(owners)} rule(s) name a CHANGE-ME owner" if owners else "no placeholder owner",
            ""
            if not owners
            else "name the team or users who approve production changes in .github/CODEOWNERS "
            "(GitHub ignores unknown owners, so the code-owner review gate would require nobody)",
        )
    )
    if argocd:
        apps = _argocd_placeholders()
        checks.append(
            Check(
                "placeholder: argocd repoURL",
                MISSING if apps else OK,
                True,
                f"CHANGE-ME repoURL in {', '.join(apps)}" if apps else "set",
                "" if not apps else "set spec.source.repoURL to this repository's git URL",
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
    rc, data, _err = _gh_api_detail(path)
    return rc, data


def _gh_api_detail(path: str) -> tuple[int, Any, str]:
    """``gh api <path>`` (GET, read-only): ``(returncode, parsed JSON or None, stderr)``."""
    try:
        result = _kube.run_cmd(["gh", "api", path], check=False, quiet=True)
    except ToolFailed as e:
        return 1, None, str(e)
    if result.returncode != 0:
        return result.returncode, None, (result.stderr or "").strip()
    try:
        return 0, json.loads(result.stdout or "null"), ""
    except json.JSONDecodeError:
        return 0, None, ""


# `gh api` reports an HTTP error as e.g. "gh: Not Found (HTTP 404)".
_HTTP_STATUS = re.compile(r"\(HTTP (\d{3})\)")

FOUND = "found"
ABSENT = "absent"
DENIED = "denied"
UNKNOWN = "unknown"


def _gh_lookup(path: str) -> tuple[str, Any, str]:
    """Classify a read: FOUND, ABSENT (HTTP 404), DENIED (401/403) or UNKNOWN (network, ...)."""
    rc, data, err = _gh_api_detail(path)
    if rc == 0:
        return FOUND, data, ""
    match = _HTTP_STATUS.search(err)
    code = int(match.group(1)) if match else 0
    if code == 404:
        return ABSENT, None, err
    if code in (401, 403):
        return DENIED, None, err
    return UNKNOWN, None, err


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


# GitHub environments the helm-push workflows run in (staging.yaml, promote-to-prod.yaml)
# and the environment secret holding the cluster credentials there.
HELM_PUSH_ENVIRONMENTS = ("staging", "production")
KUBECONFIG_SECRET = "DEPLOY_KUBECONFIG"
REPO_KUBECONFIG_NAMES = ("KUBECONFIG", "DEPLOY_KUBECONFIG")


def check_kubeconfig_secrets(repo: str) -> list[Check]:
    """helm-push: the kubeconfig must be an environment secret, never a repository one.

    Only jobs that pass the environment's protection rules (required reviewers
    for production, main only) can read an environment secret. A repository or
    organization secret with the same name is readable by every workflow job of
    the repository, and ``secrets.DEPLOY_KUBECONFIG`` falls back to it whenever
    the environment secret is missing, which bypasses the production gate.
    Only secret names are read (GitHub never returns values).
    """
    checks: list[Check] = []
    for environment in HELM_PUSH_ENVIRONMENTS:
        name = f"github secret: {KUBECONFIG_SECRET} ({environment})"
        state, _data, err = _gh_lookup(
            f"{repo}/environments/{environment}/secrets/{KUBECONFIG_SECRET}"
        )
        if state == FOUND:
            checks.append(Check(name, OK, False, f"set in the {environment} environment"))
        elif state == ABSENT:
            checks.append(
                Check(
                    name,
                    WARN,
                    False,
                    f"not set in the {environment} environment (or the environment does not "
                    "exist); the helm-push deploy job then stops at `Configure kubeconfig`, "
                    "or uses a repository or organization secret of that name",
                    f"gh secret set {KUBECONFIG_SECRET} --env {environment} < <kubeconfig of the "
                    f"{environment} cluster> (an environment secret, never a repository secret)",
                )
            )
        elif state == DENIED:
            checks.append(
                Check(
                    name,
                    INFO,
                    False,
                    "not checked: listing environment secrets needs admin access (or the "
                    "secrets read permission) on the repository",
                )
            )
        else:
            checks.append(
                Check(name, INFO, False, f"not checked: {_first_line(err) or 'gh api failed'}")
            )

    found: list[str] = []
    unknown: list[str] = []
    for secret in REPO_KUBECONFIG_NAMES:
        state, _data, err = _gh_lookup(f"{repo}/actions/secrets/{secret}")
        if state == FOUND:
            found.append(f"repository secret {secret}")
        elif state == DENIED:
            unknown.append("reading repository secrets needs admin access on the repository")
        elif state != ABSENT:
            unknown.append(_first_line(err) or "gh api failed")
    # Organization secrets shared with the repository resolve the same way (best effort).
    state, listing, _err = _gh_lookup(f"{repo}/actions/organization-secrets?per_page=100")
    org_checked = state == FOUND and isinstance(listing, dict)
    if org_checked:
        for item in listing.get("secrets") or []:
            if isinstance(item, dict) and item.get("name") in REPO_KUBECONFIG_NAMES:
                found.append(f"organization secret {item['name']}")
    name = "github secret: repository-level kubeconfig"
    if found:
        checks.append(
            Check(
                name,
                WARN,
                False,
                f"{', '.join(found)} exist(s): every workflow job of the repository can read "
                f"it, and secrets.{KUBECONFIG_SECRET} falls back to it when an environment "
                "secret is missing (bypassing the production gate)",
                "delete it (`gh secret delete <name>`, or remove the organization secret's "
                f"access to this repository) and keep the kubeconfig only as the "
                f"{KUBECONFIG_SECRET} secret of the staging and production environments",
            )
        )
    elif unknown:
        checks.append(Check(name, INFO, False, f"not checked: {unknown[0]}"))
    else:
        scope = "repository or organization" if org_checked else "repository"
        checks.append(
            Check(
                name,
                OK,
                False,
                f"no {scope} secret named {' or '.join(REPO_KUBECONFIG_NAMES)}",
            )
        )
    return checks


def check_github(settings: DeploySettings) -> list[Check]:
    """Report (never enforce) the production environment and branch protection settings."""
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

    rc, prod, err = _gh_api_detail(f"{repo}/environments/production")
    if rc != 0 and not _HTTP_STATUS.search(err):
        # No HTTP answer at all (offline, DNS, proxy): nothing below can be checked.
        return [
            Check(
                "github protection",
                INFO,
                False,
                f"skipped: the GitHub API did not answer ({_first_line(err) or 'gh api failed'})",
            )
        ]
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
                else "at least one required reviewer, prevent self-review, deployment branches restricted to main",
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
                "require pull requests, code-owner review, dismiss stale approvals, pr_checks as a required status check",
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
                "" if ok else "the branch protection settings the production gate relies on",
            )
        )
    if settings.cd == _modes.HELM_PUSH:
        checks += check_kubeconfig_secrets(repo)
    return checks


def _hosted_registry(registry: str) -> bool:
    host = registry.split("/")[0].lower()
    return host in HOSTED_REGISTRY_HOSTS or host.endswith(HOSTED_REGISTRY_SUFFIXES)


def check_disconnected(settings: DeploySettings, values: dict[str, Any]) -> list[Check]:
    """Disconnected profile: fail on every hosted dependency."""
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
            else "langgraph-server needs a LangSmith license check (a hosted call); use runtime fastapi",
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
                f"cd is {settings.cd} on GitHub Enterprise Server {', '.join(sorted(gitops.enterprise_hosts()))} (CD needs GitHub Actions on that server)",
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
        local = None
        if settings.cd == "skip":
            # The same cluster-based detection `deploy` uses, not the context name alone.
            local = local_load.detect(target.context)[0] is not None
        mode = _modes.derive_mode(settings.cd, target.context, local=local)
    else:
        mode = _modes.derive_mode(settings.cd, None)
    report = Report(env=env, profile=profile, mode=mode)
    report.checks += check_tools(settings, mode)
    if target is not None:
        report.checks += check_cluster(settings, env or "", target, values)
    else:
        report.checks.append(Check("cluster", SKIP, False, "pass --env to check the cluster"))
    report.checks += check_placeholders(settings, values, env)
    report.checks += check_github(settings)
    if profile == DISCONNECTED:
        report.checks += check_disconnected(settings, values)
    return report
