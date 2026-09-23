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
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

PROJECT_NAME = "{{cookiecutter.project_name}}"
CHART = Path(__file__).resolve().parents[2] / "deployment" / "helm" / PROJECT_NAME
HELM = shutil.which("helm")
# What `graph-agents-cli deploy` passes: the image tag it deploys, and the
# Gateway the scaffolded staging/prod values leave for the operator to name.
DEPLOY_ARGS = ("--set", "image.tag=0123abc", "--set", "gateway.parentRef.name=gw")

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


def _agent_deployment(manifests: str) -> dict:
    for doc in yaml.safe_load_all(manifests):
        if doc and doc["kind"] == "Deployment" and doc["metadata"]["name"] == PROJECT_NAME:
            return doc
    raise AssertionError("the agent Deployment was not rendered")


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
    for env in ("dev", "staging", "prod"):
        result = _helm(
            "lint",
            str(chart_with_dependencies),
            "-f",
            str(chart_with_dependencies / f"values-{env}.yaml"),
            *DEPLOY_ARGS,
        )
        assert result.returncode == 0, f"{env}: {result.stdout}\n{result.stderr}"


def test_helm_template_renders_every_environment(chart_with_dependencies: Path) -> None:
    for env in ("dev", "staging", "prod"):
        result = _render(env, *DEPLOY_ARGS)
        assert result.returncode == 0, f"{env}: {result.stderr}"
        out = result.stdout
        assert "kind: Deployment" in out and "kind: Service" in out and "kind: ConfigMap" in out
        assert "secretRef:" in out and f"name: {PROJECT_NAME}-app" in out
        if env == "dev":
            assert "kind: HTTPRoute" not in out
            assert f"$(POSTGRES_PASSWORD)@{PROJECT_NAME}-postgresql" in out
        else:
            assert "kind: HTTPRoute" in out
            assert "POSTGRES_PASSWORD" not in out


def test_pods_are_hardened_probed_and_sized(chart_with_dependencies: Path) -> None:
    for env in ("dev", "staging", "prod"):
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
    result = _render("prod", "--set", "gateway.parentRef.name=gw")
    assert result.returncode != 0
    assert "image.tag is empty" in result.stderr
