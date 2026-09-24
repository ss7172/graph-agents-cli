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

"""A generated project keeps working as its owner changes it (slow: installs one project).

One `jwt` project is created with the real engine and installed once. Then:

- `auth dev-token` signs with the project's own environment and the token opens
  the local server `run` starts (the path a coding agent follows for a jwt
  project), while a run without it gets a 401 whose hint names the command;
- the project's own tests pass after the owner removes the example tool,
  turns the staging gateway off and an ingress on in prod, and fills `.env`
  (and the shell) with settings of their own: the tests depend on none of it.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

UV = shutil.which("uv")
CLI = [sys.executable, "-m", "graph_agents_cli.main"]

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(UV is None, reason="uv is not on PATH"),
]


def _env(**extra: str) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("AUTH_", "MODEL_", "JUDGE_", "GRAPH_AGENTS_CLI_"))
        and key not in {"VIRTUAL_ENV", "APP_ENV", "API_KEY", "OPENAI_API_KEY"}
    }
    env.update(
        {
            "GRAPH_AGENTS_CLI_NO_UPDATE_CHECK": "1",
            "GRAPH_AGENTS_CLI_DISABLE_OVERRIDES": "1",
            "UV_NO_CONFIG": "1",
            "NO_COLOR": "1",
        }
    )
    env.update(extra)
    return env


def _run(args: list[str], cwd: Path, env: dict[str, str], timeout: int = 900):
    return subprocess.run(
        args, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False
    )


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="module")
def jwt_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("independence")
    created = _run(
        [
            *CLI,
            *("create", "vj", "-y", "--skip-checks", "--auth-policy", "jwt"),
            *("--registry", "localhost/dev", "-o", str(out)),
        ],
        out,
        _env(),
    )
    assert created.returncode == 0, created.stdout + created.stderr
    project = out / "vj"
    sync = _run([UV or "uv", "sync", "--locked"], project, _env())
    assert sync.returncode == 0, sync.stderr[-3000:]
    env_text = (project / ".env.example").read_text(encoding="utf-8")
    (project / ".env").write_text(
        env_text.replace("MODEL_PROVIDER=openai", "MODEL_PROVIDER=fake"), encoding="utf-8"
    )
    yield project
    _run([*CLI, "run", "--stop-server"], project, _env())


def test_a_dev_token_opens_the_local_server_and_nothing_else_does(jwt_project: Path) -> None:
    port = str(_free_port())
    anonymous = _run([*CLI, "run", "hi"], jwt_project, _env(GRAPH_AGENTS_CLI_RUN_PORT=port))
    assert anonymous.returncode == 1, anonymous.stdout + anonymous.stderr
    assert "503" in anonymous.stderr and "auth dev-token --sub" in anonymous.stderr

    minted = _run(
        [*CLI, "auth", "dev-token", "--sub", "alice", "--roles", "user"], jwt_project, _env()
    )
    assert minted.returncode == 0, minted.stdout + minted.stderr
    token = minted.stdout.strip()
    assert token.count(".") == 2 and "\n" not in token
    assert (
        jwt_project / ".graph-agents-cli" / "dev-jwt" / "private-key.pem"
    ).stat().st_mode & 0o077 == 0
    assert "AUTH_JWT_PUBLIC_KEY=" in (jwt_project / ".env").read_text(encoding="utf-8")

    without = _run([*CLI, "run", "hi"], jwt_project, _env(GRAPH_AGENTS_CLI_RUN_PORT=port))
    assert without.returncode == 1
    assert "Missing bearer token" in without.stderr
    assert 'export GRAPH_AGENTS_CLI_API_KEY="$(graph-agents-cli auth dev-token' in without.stderr

    answered = _run(
        [*CLI, "run", "What's the weather in Paris?"],
        jwt_project,
        _env(GRAPH_AGENTS_CLI_RUN_PORT=port, GRAPH_AGENTS_CLI_API_KEY=token),
    )
    assert answered.returncode == 0, answered.stdout + answered.stderr
    assert "[tool_call: get_weather" in answered.stdout and "sunny" in answered.stdout
    assert token not in answered.stdout + answered.stderr

    forged = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    refused = _run(
        [*CLI, "run", "hi"],
        jwt_project,
        _env(GRAPH_AGENTS_CLI_RUN_PORT=port, GRAPH_AGENTS_CLI_API_KEY=forged),
    )
    assert refused.returncode == 1 and "Invalid bearer token" in refused.stderr


def test_the_projects_tests_hold_after_the_owner_changes_it(jwt_project: Path) -> None:
    (jwt_project / "app" / "tools" / "weather.py").unlink()
    chart = jwt_project / "deployment" / "helm" / "vj"
    staging = chart / "values-staging.yaml"
    staging.write_text(
        staging.read_text().replace("gateway:\n  enabled: true", "gateway:\n  enabled: false")
    )
    prod = chart / "values-prod.yaml"
    prod.write_text(
        prod.read_text().replace("gateway:\n  enabled: true", "gateway:\n  enabled: false")
        + "ingress:\n  enabled: true\n  hostname: agent.example.com\n"
    )
    env_file = jwt_project / ".env"
    env_file.write_text(
        env_file.read_text()
        + "MODEL_PROVIDER=openai\nOPENAI_API_KEY=sk-not-real\nLANGSMITH_TRACING=true\n",
    )
    hostile = _env(
        AUTH_POLICY="jwt",
        API_KEY="from-the-shell",
        APP_ENV="prod",
        CHECKPOINTER="postgres",
        LANGCHAIN_TRACING_V2="true",
        MODEL_PROVIDER="openai",
        OPENAI_API_KEY="sk-not-real",
    )
    tests = _run(
        [
            UV or "uv",
            "run",
            "pytest",
            "tests/unit",
            "tests/integration",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        jwt_project,
        hostile,
    )
    assert tests.returncode == 0, tests.stdout[-5000:] + tests.stderr[-2000:]
    assert " passed" in tests.stdout
