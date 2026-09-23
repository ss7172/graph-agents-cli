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


class _Foreground:
    """Records what the playground would run in the foreground; returns a scripted code."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.returncode = 0

    def __call__(self, args, env):
        self.calls.append({"args": list(args), "env": dict(env)})
        return self.returncode

    @property
    def commands(self) -> list[list[str]]:
        return [c["args"] for c in self.calls]


@pytest.fixture
def foreground(monkeypatch) -> _Foreground:
    rec = _Foreground()
    monkeypatch.setattr(cmd_playground, "_run_foreground", rec)
    # The developer's machine may use any port: the preflight is tested on its own.
    monkeypatch.setattr(cmd_playground, "port_problem", lambda port, host="127.0.0.1": None)
    return rec


def test_playground_command_runs_uvicorn_with_app_env_dev(fake_project, foreground, monkeypatch):
    opened = []
    monkeypatch.setattr(cmd_playground, "_open_when_ready", lambda plan: opened.append(plan.url))
    result = CliRunner().invoke(playground, ["--port", "8123"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    (call,) = foreground.calls
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


def test_playground_no_open_and_langgraph_server(fake_project, foreground, monkeypatch):
    fake_project.cfg.runtime = "langgraph-server"
    opened = []
    monkeypatch.setattr(cmd_playground, "_open_when_ready", lambda plan: opened.append(plan.url))
    result = CliRunner().invoke(playground, ["--no-open"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert foreground.commands == [
        ["uv", "run", "langgraph", "dev", "--no-browser", "--port", "8000"]
    ]
    assert opened == []


def test_playground_graph_flag(fake_project, foreground, monkeypatch):
    monkeypatch.setattr(cmd_playground, "_open_when_ready", lambda plan: None)
    result = CliRunner().invoke(playground, ["--graph", "--no-open"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert foreground.commands == [
        ["uv", "run", "langgraph", "dev", "--port", "8000", "--no-browser"]
    ]


def test_playground_reports_server_failure(fake_project, foreground, monkeypatch):
    monkeypatch.setattr(cmd_playground, "_open_when_ready", lambda plan: None)
    foreground.returncode = 3
    result = CliRunner().invoke(playground, ["--no-open"])
    assert result.exit_code == 1
    assert "Failed to start playground (exit code 3)" in result.output


def _listen(host: str, port: int):
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(1)
    return sock


@pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0"])
def test_playground_refuses_a_port_in_use_before_starting_anything(fake_project, monkeypatch, host):
    """A loopback or a wildcard listener (which a loopback bind would shadow on macOS)."""
    started = []
    monkeypatch.setattr(cmd_playground, "_run_foreground", lambda a, e: started.append(a) or 0)
    monkeypatch.setattr(cmd_playground, "_open_when_ready", lambda plan: None)
    sock = _listen(host, 18631)
    try:
        result = CliRunner().invoke(playground, ["--no-open", "--port", "18631"])
    finally:
        sock.close()
    assert result.exit_code == 3, result.output
    assert "Cannot start the playground on port 18631" in result.output
    assert "--port" in result.output
    assert started == []


def test_port_problem_is_none_for_a_free_port():
    from graph_agents_cli.run._local_server import port_problem

    assert port_problem(18632) is None
    sock = _listen("127.0.0.1", 18632)
    try:
        assert "18632" in (port_problem(18632) or "")
    finally:
        sock.close()


def test_stop_tree_stops_the_server_and_its_children(tmp_path):
    """SIGTERM to the direct child, then anything it spawned is gone too."""
    import subprocess
    import sys
    import time

    import psutil

    marker = tmp_path / "grandchild.pid"
    script = (
        "import subprocess, sys, time, pathlib\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"pathlib.Path({str(marker)!r}).write_text(str(p.pid))\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", script])
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    grandchild = int(marker.read_text())
    cmd_playground._stop_tree(proc)
    assert proc.poll() is not None

    def _gone(pid: int) -> bool:
        try:
            return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return True

    deadline = time.monotonic() + 5
    while not _gone(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _gone(grandchild)


def test_run_foreground_stops_the_tree_on_interrupt(monkeypatch):
    from graph_agents_cli.run._signals import TerminationSignal

    stopped = []

    class FakeProc:
        pid = 1

        def wait(self):
            raise TerminationSignal(15)

    monkeypatch.setattr(cmd_playground._runner, "popen_resolved", lambda args, env: FakeProc())
    monkeypatch.setattr(cmd_playground, "_stop_tree", lambda proc: stopped.append(proc))
    with pytest.raises(TerminationSignal):
        cmd_playground._run_foreground(["uv", "run", "x"], {"APP_ENV": "dev"})
    assert len(stopped) == 1


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
