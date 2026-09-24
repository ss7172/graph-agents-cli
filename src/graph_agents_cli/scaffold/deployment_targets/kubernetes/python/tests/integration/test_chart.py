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

"""Lint and render the Helm chart when `helm` is on PATH (skipped otherwise).

The chart declares optional subcharts; `helm dependency build` needs the
chart registry, so it is attempted once and the test skips when it cannot run.

The values files are yours: every expectation below is read from them
(values.yaml overlaid with values-<env>.yaml, as helm merges them), so turning
the gateway off, an ingress on or the bundled Postgres off keeps these tests
green as long as the chart renders what the values ask for. They also hold
whatever image tags the values files carry: in argocd mode CI and
`graph-agents-cli deploy` commit the tag to values-<env>.yaml, and the pull
request that does so runs them too. What stays fixed is the security posture:
probes and metrics are never published, pods are hardened.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

PROJECT_NAME = "{{cookiecutter.project_name}}"
CHART = Path(__file__).resolve().parents[2] / "deployment" / "helm" / PROJECT_NAME
ENVIRONMENTS = ("dev", "staging", "prod")
HELM = shutil.which("helm")
# The Gateway the scaffolded staging/prod values leave for the operator to name.
GATEWAY = ("--set", "gateway.parentRef.name=gw")
# What `graph-agents-cli deploy` passes: the image tag it deploys (always as a
# string), and the Gateway.
DEPLOY_ARGS = ("--set-string", "image.tag=0123abc", *GATEWAY)

pytestmark = pytest.mark.skipif(HELM is None, reason="helm is not installed")


def _helm(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([HELM or "helm", *args], capture_output=True, text=True, check=False)


def _render(env: str, *extra: str) -> subprocess.CompletedProcess[str]:
    return _helm(
        "template",
        PROJECT_NAME,
        str(CHART),
        "-f",
        str(CHART / f"values-{env}.yaml"),
        "--namespace",
        f"{PROJECT_NAME}-{env}",
        *extra,
    )


def _docs(manifests: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(manifests) if doc]


def _agent_deployment(manifests: str) -> dict:
    for doc in _docs(manifests):
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == PROJECT_NAME:
            return doc
    raise AssertionError("the agent Deployment was not rendered")


def _committed_tag(env: str) -> str:
    values = yaml.safe_load((CHART / f"values-{env}.yaml").read_text()) or {}
    return str((values.get("image") or {}).get("tag") or "")


def _merge(base: dict, override: dict) -> dict:
    """Helm's values merge: maps merge key by key, anything else is replaced."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _values(env: str) -> dict:
    """The values `helm template -f values-<env>.yaml` renders with."""
    base = yaml.safe_load((CHART / "values.yaml").read_text()) or {}
    return _merge(base, yaml.safe_load((CHART / f"values-{env}.yaml").read_text()) or {})


def _enabled(values: dict, key: str) -> bool:
    return bool((values.get(key) or {}).get("enabled"))


def _published_paths(manifests: str) -> dict[str, str]:
    """Every path the HTTPRoute and the Ingress publish: path -> match type."""
    paths: dict[str, str] = {}
    for doc in _docs(manifests):
        if doc["kind"] == "HTTPRoute":
            for rule in doc["spec"]["rules"]:
                for match in rule["matches"]:
                    paths[match["path"]["value"]] = match["path"]["type"]
        elif doc["kind"] == "Ingress":
            for rule in doc["spec"]["rules"]:
                for entry in rule["http"]["paths"]:
                    kind = "Exact" if entry["pathType"] == "Exact" else "PathPrefix"
                    paths[entry["path"]] = kind
    return paths


def _configured_paths(values: dict) -> dict[str, str]:
    """route.publicPaths, plus route.devPaths while env.APP_ENV is exactly dev (as the chart does)."""
    route = values.get("route") or {}
    entries = list(route.get("publicPaths") or [])
    if str((values.get("env") or {}).get("APP_ENV", "")) == "dev":
        entries += list(route.get("devPaths") or [])
    return {str(e["path"]): str(e["type"]) for e in entries}


@pytest.fixture(scope="module")
def chart_with_dependencies() -> Path:
    if not (CHART / "charts").exists():
        result = _helm("dependency", "build", str(CHART))
        if result.returncode != 0:
            pytest.skip(
                f"helm dependency build failed (no registry access?): {result.stderr[-400:]}"
            )
    return CHART


def test_helm_lint(chart_with_dependencies: Path) -> None:
    for env in ENVIRONMENTS:
        result = _helm(
            "lint",
            str(chart_with_dependencies),
            "-f",
            str(chart_with_dependencies / f"values-{env}.yaml"),
            *DEPLOY_ARGS,
        )
        assert result.returncode == 0, f"{env}: {result.stdout}\n{result.stderr}"


def test_helm_template_renders_every_environment(chart_with_dependencies: Path) -> None:
    for env in ENVIRONMENTS:
        values = _values(env)
        result = _render(env, *DEPLOY_ARGS)
        assert result.returncode == 0, f"{env}: {result.stderr}"
        out = result.stdout
        assert "kind: Deployment" in out and "kind: Service" in out and "kind: ConfigMap" in out
        kinds = {doc["kind"] for doc in _docs(out)}
        container = _agent_deployment(out)["spec"]["template"]["spec"]["containers"][0]
        secret_ref = container["envFrom"][0]["secretRef"]
        assert secret_ref["name"] == f"{PROJECT_NAME}-app"
        # secretOptional: false (the default) keeps pods without their keys from starting.
        assert secret_ref["optional"] is bool(values.get("secretOptional", False)), env
        assert ("HTTPRoute" in kinds) is _enabled(values, "gateway"), env
        assert ("Ingress" in kinds) is _enabled(values, "ingress"), env
        bundled = f"$(POSTGRES_PASSWORD)@{PROJECT_NAME}-postgresql" in out
        assert bundled is _enabled(values, "postgresql"), env


def test_the_route_publishes_the_api_but_not_probes_or_metrics(
    chart_with_dependencies: Path,
) -> None:
    routed = [
        env
        for env in ENVIRONMENTS
        if _enabled(_values(env), "gateway") or _enabled(_values(env), "ingress")
    ]
    if not routed:
        pytest.skip("no environment enables gateway or ingress")
    for env in routed:
        values = _values(env)
        result = _render(env, *DEPLOY_ARGS)
        assert result.returncode == 0, f"{env}: {result.stderr}"
        paths = _published_paths(result.stdout)
        # Exactly what the values list: nothing else reaches the Gateway or Ingress.
        assert paths == _configured_paths(values), env
        for private in ("/", "/health", "/ready", "/metrics"):
            assert private not in paths, (env, private)


def test_pods_are_hardened_probed_and_sized(chart_with_dependencies: Path) -> None:
    for env in ENVIRONMENTS:
        result = _render(env, *DEPLOY_ARGS)
        assert result.returncode == 0, f"{env}: {result.stderr}"
        pod = _agent_deployment(result.stdout)["spec"]["template"]["spec"]
        container = pod["containers"][0]
        assert container["image"].endswith(":0123abc")
        assert pod["automountServiceAccountToken"] is False
        assert pod["securityContext"]["runAsNonRoot"] is True
        assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
        security = container["securityContext"]
        assert security["readOnlyRootFilesystem"] is True
        assert security["allowPrivilegeEscalation"] is False
        assert security["capabilities"]["drop"] == ["ALL"]
        assert {"name": "tmp", "mountPath": "/tmp"} in container["volumeMounts"]
        assert container["readinessProbe"]["httpGet"]["path"] == "/ready"
        assert container["livenessProbe"]["httpGet"]["path"] == "/health"
        assert container["startupProbe"]["httpGet"]["path"] == "/health"
        assert container["resources"]["requests"]["cpu"]
        assert container["resources"]["limits"]["memory"]


def test_the_chart_refuses_to_render_without_an_image_tag(chart_with_dependencies: Path) -> None:
    # An explicit empty tag: the committed values-<env>.yaml may already name one.
    for env in ENVIRONMENTS:
        result = _render(env, "--set-string", "image.tag=", *GATEWAY)
        assert result.returncode != 0, env
        assert "image.tag is empty" in result.stderr, env


def test_committed_image_tags_are_the_ones_deployed(chart_with_dependencies: Path) -> None:
    """A tag written into values-<env>.yaml (argocd mode) renders as Argo CD renders it."""
    tagged = {env: tag for env in ENVIRONMENTS if (tag := _committed_tag(env))}
    if not tagged:
        pytest.skip("no values-<env>.yaml names an image tag yet")
    for env, tag in tagged.items():
        result = _render(env, *GATEWAY)
        assert result.returncode == 0, f"{env}: {result.stderr}"
        container = _agent_deployment(result.stdout)["spec"]["template"]["spec"]["containers"][0]
        assert container["image"].endswith(f":{tag}"), (env, container["image"])
