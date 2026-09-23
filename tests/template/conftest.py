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

"""Fixtures for the template tests: rendered projects per combination."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The repo root is not on sys.path (pyproject sets pythonpath = ["src"]), so
# put it there before importing the harness as `tests.template.render`.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.template.render import Combo, render_project  # noqa: E402

COMBOS: dict[str, Combo] = {
    "fastapi-argocd": Combo(runtime="fastapi", cd="argocd"),
    "fastapi-skip": Combo(runtime="fastapi", cd="skip"),
    "server-helm-push": Combo(
        runtime="langgraph-server", cd="helm-push", model_provider="anthropic"
    ),
    "none": Combo(
        runtime="fastapi", cd="skip", deployment_target="none", checkpointer="memory", registry=""
    ),
    "compat-custom": Combo(
        runtime="fastapi",
        cd="helm-push",
        model_provider="openai-compatible",
        auth_policy="custom",
    ),
    "jwt": Combo(runtime="fastapi", cd="skip", auth_policy="jwt"),
    "custom-dir": Combo(
        runtime="fastapi",
        cd="skip",
        agent_directory="my_agent",
        has_api_policy=True,
        process="agentic-template/workflow.md",
    ),
}


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "slow: renders and installs a project; needs uv (and helm) on PATH"
    )


@pytest.fixture(scope="session")
def rendered(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Every combination rendered once per session: name -> project directory."""
    root = tmp_path_factory.mktemp("rendered")
    return {name: render_project(root / name, combo) for name, combo in COMBOS.items()}
