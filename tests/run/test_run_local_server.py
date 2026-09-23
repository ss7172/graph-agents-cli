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

"""Pid-file lifecycle of the local server with a monkeypatched popen."""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import click
import pytest
from filelock import FileLock

from graph_agents_cli.run import _local_server as ls


class _FakeProc:
    """A Popen stand-in: ``poll()`` reports the exit code the test scripted (None = running)."""

    def __init__(self, pid: int = 4242, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int | None:
        return self.returncode


@pytest.fixture
def started(monkeypatch, tmp_path: Path):
    """Patch everything that would touch a real process or socket."""
    popen_calls: list[dict] = []
    terminated: list[int] = []
    state = SimpleNamespace(root=tmp_path, popen_calls=popen_calls, terminated=terminated)
    state.proc = _FakeProc(pid=4242, returncode=None)

    def fake_popen(args, **kwargs):
        popen_calls.append({"args": args, **kwargs})
        return state.proc

    monkeypatch.setattr(ls, "popen_resolved_detached", fake_popen)
    monkeypatch.setattr(ls, "_find_free_port", lambda *a, **k: 18080)
    # The pid file is only trusted when the port answers; the fake never opens one.
    monkeypatch.setattr(
        ls, "_fetch_health", lambda port, timeout=1.0: {"status": "ok", "checkpointer": "memory"}
    )
    monkeypatch.setattr(ls, "_terminate_process", lambda pid: terminated.append(pid))
    return state


def _write_pid(root: Path, **overrides):
    now = datetime.now(UTC).isoformat()
    data = {
        "pid": 111,
        "port": 18081,
        "started_at": now,
        "last_activity": now,
        "runtime": "fastapi",
        "checkpointer": "memory",
    }
    data.update(overrides)
    path = ls.pid_file_path(root)
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data))
    return data


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def test_build_serve_command_per_runtime():
    assert ls.build_serve_command(agent_dir="app", port=18080, runtime="fastapi") == [
        "uv",
        "run",
        "uvicorn",
        "app.fast_api_app:app",
        "--host",
        "127.0.0.1",
        "--port",
        "18080",
    ]
    assert ls.build_serve_command(agent_dir="app", port=18081, runtime="langgraph-server") == [
        "uv",
        "run",
        "langgraph",
        "dev",
        "--no-browser",
        "--port",
        "18081",
    ]
    with pytest.raises(click.ClickException, match="Unsupported runtime"):
        ls.build_serve_command(agent_dir="app", port=1, runtime="adk")


# ---------------------------------------------------------------------------
# ensure_server
# ---------------------------------------------------------------------------


def test_ensure_server_starts_and_writes_pid_file(started):
    info = ls.ensure_server(started.root, "app", runtime="fastapi", checkpointer="postgres")
    assert info == ls.ServerInfo(
        port=18080, started=True, pid=4242, runtime="fastapi", checkpointer="memory"
    )
    assert info.base_url == "http://127.0.0.1:18080"

    (call,) = started.popen_calls
    assert call["args"] == [
        "uv",
        "run",
        "uvicorn",
        "app.fast_api_app:app",
        "--host",
        "127.0.0.1",
        "--port",
        "18080",
    ]
    assert call["cwd"] == str(started.root)
    assert call["env"]["PORT"] == "18080"

    data = json.loads(ls.pid_file_path(started.root).read_text())
    assert set(data) == {"pid", "port", "started_at", "last_activity", "runtime", "checkpointer"}
    assert data["pid"] == 4242 and data["port"] == 18080
    assert data["runtime"] == "fastapi"
    # /health reported memory, which wins over the manifest default.
    assert data["checkpointer"] == "memory"
    assert (started.root / ls.PID_DIR / ls.LOG_FILENAME).exists()


def test_ensure_server_langgraph_server_command(started):
    ls.ensure_server(started.root, "app", runtime="langgraph-server")
    assert started.popen_calls[0]["args"] == [
        "uv",
        "run",
        "langgraph",
        "dev",
        "--no-browser",
        "--port",
        "18080",
    ]
    assert json.loads(ls.pid_file_path(started.root).read_text())["runtime"] == "langgraph-server"


def test_ensure_server_rejects_unknown_runtime(started):
    with pytest.raises(click.ClickException, match="Unsupported runtime"):
        ls.ensure_server(started.root, "app", runtime="adk")
    assert not started.popen_calls


def test_ensure_server_reuses_live_server_and_stamps_activity(started, monkeypatch):
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    _write_pid(started.root, last_activity=old, checkpointer="postgres")
    monkeypatch.setattr(ls, "_is_server_alive", lambda pid, port: True)

    info = ls.ensure_server(started.root, "app", runtime="fastapi")
    assert info == ls.ServerInfo(
        port=18081, started=False, pid=111, runtime="fastapi", checkpointer="postgres"
    )
    assert not started.popen_calls
    assert json.loads(ls.pid_file_path(started.root).read_text())["last_activity"] > old


def test_ensure_server_runtime_mismatch_is_hard_error_and_leaves_server(started, monkeypatch):
    _write_pid(started.root, runtime="langgraph-server")
    monkeypatch.setattr(ls, "_is_server_alive", lambda pid, port: True)
    with pytest.raises(click.ClickException, match=r"langgraph-server.*fastapi"):
        ls.ensure_server(started.root, "app", runtime="fastapi")
    assert not started.terminated
    assert not started.popen_calls
    assert ls.pid_file_path(started.root).exists()


def test_ensure_server_replaces_idle_server(started, monkeypatch):
    stale = (datetime.now(UTC) - timedelta(minutes=31)).isoformat()
    _write_pid(started.root, last_activity=stale)
    monkeypatch.setattr(ls, "_is_server_alive", lambda pid, port: True)

    info = ls.ensure_server(started.root, "app", runtime="fastapi")
    assert started.terminated == [111]
    assert info.started and info.pid == 4242


def test_ensure_server_cleans_stale_pid_file(started, monkeypatch):
    _write_pid(started.root)
    monkeypatch.setattr(ls, "_is_server_alive", lambda pid, port: False)
    info = ls.ensure_server(started.root, "app", runtime="fastapi")
    assert started.terminated == [111]
    assert info.started


def test_ensure_server_fails_fast_when_process_exits_during_startup(started, monkeypatch):
    """A crashed child is detected through Popen.poll() (a zombie still satisfies pid_exists)."""
    started.proc.returncode = 1
    monkeypatch.setattr(ls, "_fetch_health", lambda port, timeout=1.0: None)
    log = started.root / ls.PID_DIR / ls.LOG_FILENAME
    log.parent.mkdir(exist_ok=True)
    log.write_text("ImportError: cannot import name 'graph'\n")
    with pytest.raises(click.ClickException) as excinfo:
        ls.ensure_server(started.root, "app", runtime="fastapi")
    message = str(excinfo.value)
    assert "exited during startup (exit code 1)" in message
    assert "cannot import name 'graph'" in message  # the log tail is included
    assert not ls.pid_file_path(started.root).exists()
    # The child was already reaped by poll(): no spurious termination of a dead pid.
    assert started.terminated == []


def test_ensure_server_terminates_a_hung_child_on_startup_timeout(started, monkeypatch):
    monkeypatch.setattr(ls, "_fetch_health", lambda port, timeout=1.0: None)
    ticks = iter(range(0, 1000))
    monkeypatch.setattr(ls.time, "monotonic", lambda: float(next(ticks)))
    with pytest.raises(click.ClickException, match="did not become healthy"):
        ls.ensure_server(started.root, "app", runtime="fastapi", startup_timeout=2)
    assert started.terminated == [4242]
    assert not ls.pid_file_path(started.root).exists()


def test_real_child_that_exits_immediately_fails_within_seconds(tmp_path: Path, monkeypatch):
    """End to end on POSIX: an immediately exiting child must not cost the full startup timeout."""
    monkeypatch.setattr(
        ls,
        "build_serve_command",
        lambda **kw: [sys.executable, "-c", "import sys; sys.exit(3)"],
    )
    monkeypatch.setattr(ls, "_find_free_port", lambda *a, **k: 18099)
    monkeypatch.setattr(ls, "_fetch_health", lambda port, timeout=1.0: None)
    before = time.monotonic()
    with pytest.raises(click.ClickException, match=r"exited during startup \(exit code 3\)"):
        ls.ensure_server(tmp_path, "app", runtime="fastapi", startup_timeout=30)
    assert time.monotonic() - before < 10
    assert not ls.pid_file_path(tmp_path).exists()


def test_wait_for_ready_times_out_with_log_tail(started, monkeypatch):
    monkeypatch.setattr(ls, "_fetch_health", lambda port, timeout=1.0: None)
    ticks = iter(range(0, 1000))
    monkeypatch.setattr(ls.time, "monotonic", lambda: float(next(ticks)))
    log = started.root / ls.PID_DIR / ls.LOG_FILENAME
    log.parent.mkdir(exist_ok=True)
    log.write_text("boom: ModuleNotFoundError\n")
    with pytest.raises(click.ClickException) as excinfo:
        ls._wait_for_ready(started.root, 18080, proc=None, timeout=3, sleep=lambda s: None)
    assert "GET /health" in str(excinfo.value)
    assert "ModuleNotFoundError" in str(excinfo.value)


def test_ensure_server_waits_on_the_lock_and_reports_a_stuck_holder(started):
    lock = FileLock(str(started.root / ls.PID_DIR / ls.LOCK_FILENAME))
    (started.root / ls.PID_DIR).mkdir(exist_ok=True)
    lock.acquire()
    try:
        with pytest.raises(click.ClickException, match="held the local server lock"):
            ls.ensure_server(started.root, "app", runtime="fastapi", lock_timeout=0.2)
        assert not started.popen_calls
    finally:
        lock.release()
    # Once released, the start proceeds and the lock is not left held.
    assert ls.ensure_server(started.root, "app", runtime="fastapi", lock_timeout=0.2).started
    assert not lock.is_locked


# ---------------------------------------------------------------------------
# stop_server / get_server_port
# ---------------------------------------------------------------------------


def test_stop_server_ownership_aware(started):
    """Our own pid is always stopped; another invocation's record is never deleted."""
    _write_pid(started.root, pid=111)
    # The pid file names another server (a concurrent invocation replaced it):
    # the server this invocation started (999) is stopped, the file stays.
    assert ls.stop_server(started.root, pid=999) is True
    assert started.terminated == [999]
    assert ls.pid_file_path(started.root).exists()
    assert ls.stop_server(started.root, pid=111) is True
    assert started.terminated == [999, 111]
    assert not ls.pid_file_path(started.root).exists()
    assert ls.stop_server(started.root) is False
    # No pid file at all: the process this invocation started is still stopped.
    assert ls.stop_server(started.root, pid=555) is True
    assert started.terminated == [999, 111, 555]


def test_pid_file_is_written_atomically(started):
    ls.write_pid_file(started.root, pid=1, port=2, runtime="fastapi", checkpointer="memory")
    state_dir = started.root / ls.PID_DIR
    assert sorted(p.name for p in state_dir.iterdir() if p.name.startswith("run_server")) == [
        "run_server.json"
    ]
    before = json.loads(ls.pid_file_path(started.root).read_text())
    ls.touch_activity(started.root)
    after = json.loads(ls.pid_file_path(started.root).read_text())
    assert after["last_activity"] >= before["last_activity"] and after["pid"] == 1
    assert not list(state_dir.glob("*.tmp"))


def test_activity_heartbeat_stamps_until_stopped(started):
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    _write_pid(started.root, last_activity=old)
    stop = ls.start_activity_heartbeat(started.root, interval=0.05)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if json.loads(ls.pid_file_path(started.root).read_text())["last_activity"] > old:
                break
            time.sleep(0.02)
    finally:
        stop()
    assert json.loads(ls.pid_file_path(started.root).read_text())["last_activity"] > old


def test_get_server_port(started, monkeypatch):
    assert ls.get_server_port(started.root) is None
    _write_pid(started.root, port=18085)
    monkeypatch.setattr(ls, "_is_server_alive", lambda pid, port: True)
    assert ls.get_server_port(started.root) == 18085
    monkeypatch.setattr(ls, "_is_server_alive", lambda pid, port: False)
    assert ls.get_server_port(started.root) is None


def test_is_idle_treats_missing_timestamp_as_stale():
    assert ls._is_idle({}, 10) is True
    assert ls._is_idle({"last_activity": "not a date"}, 10) is True
    assert ls._is_idle({"last_activity": datetime.now(UTC).isoformat()}, 10) is False


def test_read_pid_file_tolerates_garbage(tmp_path):
    path = ls.pid_file_path(tmp_path)
    path.parent.mkdir()
    path.write_text("{not json")
    assert ls.read_pid_file(tmp_path) is None
    path.write_text("[1, 2]")
    assert ls.read_pid_file(tmp_path) is None
