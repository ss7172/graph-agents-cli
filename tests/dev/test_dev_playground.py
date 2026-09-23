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

from __future__ import annotations

import click
import pytest
from click.testing import CliRunner

from graph_agents_cli.dev import cmd_playground
from graph_agents_cli.dev.cmd_playground import build_plan
from graph_agents_cli.dev.cmd_playground import cmd_playground as playground


def test_build_plan_fastapi():
    plan = build_plan(agent_dir="app", runtime="fastapi", port=8000, graph=False, open_browser=True)
    assert plan.args == [
        "uv",
        "run",
        "uvicorn",
        "app.fast_api_app:app",
        "--reload",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
    ]
    assert plan.env == {"APP_ENV": "dev"}
    assert plan.url == "http://127.0.0.1:8000/playground"
    assert plan.ready_url == "http://127.0.0.1:8000/health"


def test_build_plan_langgraph_server():
    plan = build_plan(
        agent_dir="app", runtime="langgraph-server", port=2024, graph=False, open_browser=True
    )
    assert plan.args == ["uv", "run", "langgraph", "dev", "--no-browser", "--port", "2024"]
    assert plan.env == {"APP_ENV": "dev"}
    assert plan.url == "http://127.0.0.1:2024/playground"


@pytest.mark.parametrize("runtime", ["fastapi", "langgraph-server"])
def test_build_plan_graph_runs_langgraph_dev_under_either_runtime(runtime):
    plan = build_plan(agent_dir="app", runtime=runtime, port=2024, graph=True, open_browser=True)
    assert plan.args == ["uv", "run", "langgraph", "dev", "--port", "2024"]
    assert plan.ready_url is None  # langgraph dev opens Studio itself
    assert "baseUrl=http://127.0.0.1:2024" in plan.url

    quiet = build_plan(agent_dir="app", runtime=runtime, port=2024, graph=True, open_browser=False)
    assert quiet.args[-1] == "--no-browser"


def test_build_plan_rejects_unknown_runtime():
    with pytest.raises(click.ClickException, match="Unsupported runtime"):
        build_plan(agent_dir="app", runtime="adk", port=1, graph=False, open_browser=True)


def test_playground_command_runs_uvicorn_with_app_env_dev(fake_project, recorded_runs, monkeypatch):
    opened = []
    monkeypatch.setattr(cmd_playground, "_open_when_ready", lambda plan: opened.append(plan.url))
    result = CliRunner().invoke(playground, ["--port", "8123"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    (call,) = recorded_runs.calls
    assert call["args"] == [
        "uv",
        "run",
        "uvicorn",
        "app.fast_api_app:app",
        "--reload",
        "--host",
        "127.0.0.1",
        "--port",
        "8123",
    ]
    assert call["env"]["APP_ENV"] == "dev"
    assert opened == ["http://127.0.0.1:8123/playground"]
    assert "http://127.0.0.1:8123/playground" in result.output


def test_playground_no_open_and_langgraph_server(fake_project, recorded_runs, monkeypatch):
    fake_project.cfg.runtime = "langgraph-server"
    opened = []
    monkeypatch.setattr(cmd_playground, "_open_when_ready", lambda plan: opened.append(plan.url))
    result = CliRunner().invoke(playground, ["--no-open"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert recorded_runs.commands == [
        ["uv", "run", "langgraph", "dev", "--no-browser", "--port", "8000"]
    ]
    assert opened == []


def test_playground_graph_flag(fake_project, recorded_runs, monkeypatch):
    monkeypatch.setattr(cmd_playground, "_open_when_ready", lambda plan: None)
    result = CliRunner().invoke(playground, ["--graph", "--no-open"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert recorded_runs.commands == [
        ["uv", "run", "langgraph", "dev", "--port", "8000", "--no-browser"]
    ]


def test_playground_reports_server_failure(fake_project, recorded_runs, monkeypatch):
    monkeypatch.setattr(cmd_playground, "_open_when_ready", lambda plan: None)
    recorded_runs.returncodes = [3]
    result = CliRunner().invoke(playground, ["--no-open"])
    assert result.exit_code == 1
    assert "Failed to start playground" in result.output


def test_wait_and_open_opens_once_ready(monkeypatch):
    import httpx

    opened = []
    calls = iter([httpx.ConnectError("refused"), httpx.Response(200)])

    def fake_get(url, timeout):
        value = next(calls)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(cmd_playground.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(cmd_playground.time, "sleep", lambda s: None)
    assert (
        cmd_playground._wait_and_open("http://x/playground", "http://x/health", timeout=5) is True
    )
    assert opened == ["http://x/playground"]
