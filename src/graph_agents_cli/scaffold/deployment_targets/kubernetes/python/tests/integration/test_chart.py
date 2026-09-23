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

These tests hold whatever image tags the values files carry: in argocd mode CI
and `graph-agents-cli deploy` commit the tag to values-<env>.yaml, and the pull
request that does so runs them too.
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
        result = _render(env, *DEPLOY_ARGS)
        assert result.returncode == 0, f"{env}: {result.stderr}"
        out = result.stdout
        assert "kind: Deployment" in out and "kind: Service" in out and "kind: ConfigMap" in out
        container = _agent_deployment(out)["spec"]["template"]["spec"]["containers"][0]
        secret_ref = container["envFrom"][0]["secretRef"]
        assert secret_ref["name"] == f"{PROJECT_NAME}-app"
        # Outside dev a missing Secret keeps the pods from starting without keys.
        assert secret_ref["optional"] is (env == "dev")
        if env == "dev":
            assert "kind: HTTPRoute" not in out
            assert f"$(POSTGRES_PASSWORD)@{PROJECT_NAME}-postgresql" in out
        else:
            assert "kind: HTTPRoute" in out
            assert "POSTGRES_PASSWORD" not in out


def test_the_route_publishes_the_api_but_not_probes_or_metrics(
    chart_with_dependencies: Path,
) -> None:
    for env in ("staging", "prod"):
        result = _render(env, *DEPLOY_ARGS)
        assert result.returncode == 0, f"{env}: {result.stderr}"
        route = next(d for d in _docs(result.stdout) if d["kind"] == "HTTPRoute")
        paths = {
            m["path"]["value"]: m["path"]["type"]
            for rule in route["spec"]["rules"]
            for m in rule["matches"]
        }
        assert paths["/chat"] == "Exact" and paths["/threads"] == "PathPrefix"
        assert any(p.startswith("/a2a/") for p in paths)
        for private in ("/", "/health", "/ready", "/metrics", "/playground"):
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
