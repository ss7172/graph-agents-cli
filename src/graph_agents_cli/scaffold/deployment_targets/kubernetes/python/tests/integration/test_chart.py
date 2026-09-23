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

PROJECT_NAME = "{{cookiecutter.project_name}}"
CHART = Path(__file__).resolve().parents[2] / "deployment" / "helm" / PROJECT_NAME
HELM = shutil.which("helm")

pytestmark = pytest.mark.skipif(HELM is None, reason="helm is not installed")


def _helm(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([HELM or "helm", *args], capture_output=True, text=True, check=False)


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
            "--set",
            "gateway.parentRef.name=gw",
        )
        assert result.returncode == 0, f"{env}: {result.stdout}\n{result.stderr}"


def test_helm_template_renders_every_environment(chart_with_dependencies: Path) -> None:
    for env in ("dev", "staging", "prod"):
        result = _helm(
            "template",
            PROJECT_NAME,
            str(chart_with_dependencies),
            "-f",
            str(chart_with_dependencies / f"values-{env}.yaml"),
            "--set",
            "gateway.parentRef.name=gw",
            "--namespace",
            f"{PROJECT_NAME}-{env}",
        )
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
