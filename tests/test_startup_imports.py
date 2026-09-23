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

"""Startup must import no agent framework, model SDK, or cluster client (D26, C1).

Each check runs in a fresh interpreter so imports made by other tests in the
session cannot leak into ``sys.modules``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

FORBIDDEN_PREFIXES = (
    "langchain",
    "langgraph",
    "langsmith",
    "openai",
    "anthropic",
    "google.generativeai",
    "google.genai",
    "google.cloud",
    "kubernetes",
    "docker",
    "httpx",
    "a2a",
)

_SCRIPT = textwrap.dedent(
    """
    import json, sys
    from click.testing import CliRunner
    from graph_agents_cli import main

    args = json.loads(sys.argv[1])
    result = CliRunner().invoke(main.main, [*args, "--help"])
    forbidden = json.loads(sys.argv[2])
    loaded = sorted(
        name for name in sys.modules
        if any(name == p or name.startswith(p + ".") for p in forbidden)
    )
    print(json.dumps({"exit_code": result.exit_code, "loaded": loaded, "output": result.output}))
    """
)


def _run_help(args: list[str]) -> dict:
    env = {
        **os.environ,
        "GRAPH_AGENTS_CLI_NO_UPDATE_CHECK": "1",
        "GRAPH_AGENTS_CLI_DISABLE_OVERRIDES": "1",
    }
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT, json.dumps(args), json.dumps(FORBIDDEN_PREFIXES)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["create"],
        ["scaffold"],
        ["scaffold", "create"],
        ["scaffold", "enhance"],
        ["scaffold", "upgrade"],
        ["info"],
    ],
)
def test_help_imports_no_framework_or_vendor_sdk(args: list[str]) -> None:
    result = _run_help(args)
    assert result["exit_code"] == 0, result["output"]
    assert "Usage:" in result["output"]
    assert result["loaded"] == [], f"forbidden modules imported at startup: {result['loaded']}"


def test_root_help_lists_every_command() -> None:
    result = _run_help([])
    for command in (
        "setup",
        "update",
        "login",
        "create",
        "scaffold",
        "playground",
        "run",
        "install",
        "lint",
        "build",
        "eval",
        "deploy",
        "secrets",
        "infra",
        "extension",
        "info",
    ):
        assert f"\n  {command} " in result["output"] or f"\n  {command}\n" in result["output"], (
            command
        )
