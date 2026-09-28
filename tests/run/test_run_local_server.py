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
    monkeypatch.delenv(ls.RUN_PORT_ENV, raising=False)
    # The pid file is only trusted when the port answers; the fake never opens one.
    monkeypatch.setattr(
        ls, "_fetch_health", lambda port, timeout=1.0: {"status": "ok", "checkpointer": "memory"}
    )
    monkeypatch.setattr(ls, "_terminate_process", lambda pid, **_: terminated.append(pid) or True)
    monkeypatch.setattr(ls, "_create_time", lambda pid: 1700000000.25)
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
    assert set(data) == {
        "pid",
        "port",
        "started_at",
        "last_activity",
        "runtime",
        "checkpointer",
        "state",
        "create_time",
    }
    assert data["create_time"] == 1700000000.25
    assert data["state"] == ls.STATE_READY
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
    monkeypatch.setattr(ls, "_is_server_alive", lambda pid, port, create_time=None: True)

    info = ls.ensure_server(started.root, "app", runtime="fastapi")
    assert info == ls.ServerInfo(
        port=18081, started=False, pid=111, runtime="fastapi", checkpointer="postgres"
    )
    assert not started.popen_calls
    assert json.loads(ls.pid_file_path(started.root).read_text())["last_activity"] > old


def test_ensure_server_runtime_mismatch_is_hard_error_and_leaves_server(started, monkeypatch):
    _write_pid(started.root, runtime="langgraph-server")
    monkeypatch.setattr(ls, "_is_server_alive", lambda pid, port, create_time=None: True)
    with pytest.raises(click.ClickException, match=r"langgraph-server.*fastapi"):
        ls.ensure_server(started.root, "app", runtime="fastapi")
    assert not started.terminated
    assert not started.popen_calls
    assert ls.pid_file_path(started.root).exists()


def test_ensure_server_replaces_idle_server(started, monkeypatch):
    stale = (datetime.now(UTC) - timedelta(minutes=31)).isoformat()
    _write_pid(started.root, last_activity=stale)
    monkeypatch.setattr(ls, "_is_server_alive", lambda pid, port, create_time=None: True)

    info = ls.ensure_server(started.root, "app", runtime="fastapi")
    assert started.terminated == [111]
    assert info.started and info.pid == 4242


def test_ensure_server_cleans_stale_pid_file(started, monkeypatch):
    _write_pid(started.root)
    monkeypatch.setattr(ls, "_is_server_alive", lambda pid, port, create_time=None: False)
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
    with pytest.raises(
        ls.ServerStartError, match=r"exited during startup \(exit code 3\)"
    ) as excinfo:
        ls.ensure_server(tmp_path, "app", runtime="fastapi", startup_timeout=30)
    assert time.monotonic() - before < 10
    assert not ls.pid_file_path(tmp_path).exists()
    # A tool failure (2), never the 1 that `eval run` reserves for a failed gate.
    assert excinfo.value.exit_code == 2


def test_start_failures_are_tool_failures_and_a_bad_runtime_a_config_error(started, monkeypatch):
    monkeypatch.setattr(ls, "_fetch_health", lambda port, timeout=1.0: None)
    ticks = iter(range(0, 1000))
    monkeypatch.setattr(ls.time, "monotonic", lambda: float(next(ticks)))
    with pytest.raises(ls.ServerStartError) as never_healthy:
        ls.ensure_server(started.root, "app", runtime="fastapi", startup_timeout=2)
    assert never_healthy.value.exit_code == 2

    _write_pid(started.root, runtime="langgraph-server")
    monkeypatch.setattr(ls, "_is_server_alive", lambda pid, port, create_time=None: True)
    with pytest.raises(ls.ServerStartError) as other_runtime:
        ls.ensure_server(started.root, "app", runtime="fastapi")
    assert other_runtime.value.exit_code == 2

    with pytest.raises(click.ClickException) as unsupported:
        ls.ensure_server(started.root, "app", runtime="adk")
    assert unsupported.value.exit_code == 3


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


# ---------------------------------------------------------------------------
# a record that outlived its server: PID identity and an interrupted teardown
# ---------------------------------------------------------------------------


@pytest.fixture
def unrelated_process():
    """A live process of this user that is not a server (a reused PID, as far as a record knows)."""
    import subprocess

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


def test_a_reused_pid_is_never_the_recorded_server(unrelated_process):
    import psutil

    pid = unrelated_process.pid
    created = psutil.Process(pid).create_time()
    # The record's own creation time identifies it; any other time is another process.
    assert ls._server_process(pid, create_time=created, port=18080) is not None
    assert ls._server_process(pid, create_time=created - 50, port=18080) is None
    # A record without create_time (an older CLI): only a uvicorn/langgraph command line
    # serving the recorded port qualifies.
    assert ls._server_process(pid, port=18080) is None
    assert ls._server_process(0) is None
    assert ls._is_server_alive(pid, 18080, created - 50) is False


def test_a_stale_record_never_signals_the_process_that_reused_its_pid(tmp_path, unrelated_process):
    """The next `run` (or --stop-server) used to SIGTERM whatever process had the PID."""
    _write_pid(tmp_path, pid=unrelated_process.pid, port=18865, create_time=1.0, state="ready")
    assert ls.stop_server(tmp_path) is False  # nothing of ours was running
    assert not ls.pid_file_path(tmp_path).exists()  # the stale record is gone
    time.sleep(0.2)
    assert unrelated_process.poll() is None  # and the other process is untouched

    _write_pid(tmp_path, pid=unrelated_process.pid, port=18865)  # an older CLI's record
    assert ls.stop_server(tmp_path) is False
    assert unrelated_process.poll() is None


def test_the_recorded_server_is_stopped_when_it_is_still_that_process(tmp_path, unrelated_process):
    import psutil

    created = psutil.Process(unrelated_process.pid).create_time()
    _write_pid(tmp_path, pid=unrelated_process.pid, port=18866, create_time=created)
    assert ls.stop_server(tmp_path) is True
    assert unrelated_process.wait(timeout=10) is not None
    assert not ls.pid_file_path(tmp_path).exists()


def _refuse_signals(monkeypatch):
    """What psutil does in a sandbox that lets a command signal only its own processes."""
    import psutil

    def refused(self):
        raise psutil.AccessDenied(self.pid)

    monkeypatch.setattr(psutil.Process, "terminate", refused)
    monkeypatch.setattr(psutil.Process, "kill", refused)
    monkeypatch.setattr(ls, "_TERM_WAIT", 0.3, raising=False)
    monkeypatch.setattr(ls, "_KILL_WAIT", 0.3, raising=False)


def test_a_refused_signal_is_never_reported_as_stopped(
    tmp_path, unrelated_process, monkeypatch, capsys
):
    """`run --stop-server` printed "Local server stopped." and dropped the record while the
    server kept its port (Claude Code's sandbox, a server started by an earlier command)."""
    import psutil

    pid = unrelated_process.pid
    created = psutil.Process(pid).create_time()
    _write_pid(tmp_path, pid=pid, port=18867, create_time=created)
    _refuse_signals(monkeypatch)
    with pytest.raises(click.ClickException) as excinfo:
        ls.stop_server(tmp_path)
    assert isinstance(excinfo.value, ls.ServerStopError)
    message = excinfo.value.format_message()
    assert excinfo.value.exit_code == 2
    assert excinfo.value.left == [pid]
    assert f"Could not stop the local server (PID {pid}, port 18867)" in message
    assert "permission denied" in message
    assert f"`kill {pid}`" in message
    # Nothing answers on its port, so no later run can reuse it: the message must not say so.
    assert "Its record is kept, and `graph-agents-cli run --stop-server` tries again." in message
    assert "later runs reuse" not in message
    assert "Local server stopped." not in capsys.readouterr().out
    assert unrelated_process.poll() is None  # still running
    assert ls.read_pid_file(tmp_path)["pid"] == pid  # and still recorded


def test_a_server_this_run_started_is_reported_when_it_cannot_be_stopped(
    tmp_path, unrelated_process, monkeypatch
):
    import psutil

    pid = unrelated_process.pid
    ls._STARTED[pid] = psutil.Process(pid).create_time()
    _write_pid(tmp_path, pid=111)  # another invocation's record: left alone
    _refuse_signals(monkeypatch)
    try:
        with pytest.raises(click.ClickException, match=f"PID {pid}") as excinfo:
            ls.stop_server(tmp_path, pid=pid)
    finally:
        ls._STARTED.pop(pid, None)
    assert "record is kept" not in excinfo.value.format_message()  # it has no record
    assert unrelated_process.poll() is None
    assert ls.read_pid_file(tmp_path)["pid"] == 111


# The real teardown, for tests whose `started` fixture replaced it.
_REAL_TERMINATE = ls._terminate_process


@pytest.fixture
def listening_process():
    """A live process of this user listening on a loopback port: (process, port)."""
    import subprocess

    code = (
        "import socket, time\n"
        "s = socket.socket()\n"
        "s.bind(('127.0.0.1', 0))\n"
        "s.listen()\n"
        "print(s.getsockname()[1], flush=True)\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        port = int(proc.stdout.readline())
        yield proc, port
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        proc.stdout.close()


def test_an_idle_server_that_cannot_be_stopped_is_reused_with_a_warning(
    started, listening_process, monkeypatch, capsys
):
    """Every `run` and `eval run` exited 2 ("Could not stop the local server") once a server
    an earlier sandboxed command started had been idle for 30 minutes: the idle restart could
    not stop it, and nothing stops it later either, so the runs failed until a human did."""
    import psutil

    proc, port = listening_process
    created = psutil.Process(proc.pid).create_time()
    idle = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    _write_pid(started.root, pid=proc.pid, port=port, create_time=created, last_activity=idle)
    monkeypatch.setattr(ls, "_terminate_process", _REAL_TERMINATE)
    _refuse_signals(monkeypatch)

    info = ls.ensure_server(started.root, "app", runtime="fastapi")

    assert info == ls.ServerInfo(
        port=port, started=False, pid=proc.pid, runtime="fastapi", checkpointer="memory"
    )
    assert not started.popen_calls
    assert proc.poll() is None
    record = ls.read_pid_file(started.root)
    assert record["pid"] == proc.pid and record["last_activity"] > idle
    err = capsys.readouterr().err
    assert (
        f"Warning: The local server (PID {proc.pid}, port {port}) has been idle for more "
        "than 30 minutes, but it could not be stopped:"
    ) in err
    assert "permission denied" in err and f"`kill {proc.pid}`" in err
    assert "Reusing it" in err

    # Stamped: the next run reuses it without trying (and warning) again.
    assert not ls.ensure_server(started.root, "app", runtime="fastapi").started
    assert capsys.readouterr().err == ""
    # The record is kept and the server answers, so reuse is what --stop-server promises.
    with pytest.raises(ls.ServerStopError) as excinfo:
        ls.stop_server(started.root)
    assert "Its record is kept: later runs reuse the server" in excinfo.value.format_message()


def test_a_server_that_no_longer_answers_and_cannot_be_stopped_is_left_with_a_warning(
    started, unrelated_process, monkeypatch, capsys
):
    """The same failure for a recorded server whose process lives on with its port closed."""
    import socket

    import psutil

    pid = unrelated_process.pid
    with socket.socket() as probe:  # a port nothing listens on
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
    _write_pid(started.root, pid=pid, port=closed, create_time=psutil.Process(pid).create_time())
    monkeypatch.setattr(ls, "_terminate_process", _REAL_TERMINATE)
    _refuse_signals(monkeypatch)

    info = ls.ensure_server(started.root, "app", runtime="fastapi")

    assert info.started and info.pid == 4242
    assert len(started.popen_calls) == 1
    assert ls.read_pid_file(started.root)["pid"] == 4242
    assert unrelated_process.poll() is None
    err = capsys.readouterr().err
    assert (
        f"Warning: The recorded local server (PID {pid}, port {closed}) no longer answers on "
        "its port, but it could not be stopped:"
    ) in err
    assert (
        f"Starting a fresh local server; stop the old one from a shell that may signal it (`kill {pid}`)"
        in err
    )


def test_an_idle_server_left_half_stopped_is_replaced_with_a_warning(started, monkeypatch, capsys):
    """A partial stop (the leader gone, a child refused) leaves nothing to reuse: start afresh."""
    idle = (datetime.now(UTC) - timedelta(minutes=31)).isoformat()
    _write_pid(started.root, last_activity=idle)
    answers = iter([True, False])
    monkeypatch.setattr(ls, "_is_server_alive", lambda *a, **k: next(answers))

    def half_stopped(pid, **_):
        raise ls.ServerStopError(
            "Could not stop",
            pid=pid,
            port=18081,
            left=[112],
            reason="PID 112 still running",
            kill_command="kill 112",
        )

    monkeypatch.setattr(ls, "_terminate_process", half_stopped)
    info = ls.ensure_server(started.root, "app", runtime="fastapi")
    assert info.started and info.pid == 4242
    err = capsys.readouterr().err
    assert "idle for more than 30 minutes, but it could not be stopped: PID 112" in err
    assert "Starting a fresh local server" in err and "`kill 112`" in err


def test_span_words():
    assert ls._span(1800) == "30 minutes"
    assert ls._span(60) == "1 minute"
    assert ls._span(45) == "45 seconds"
    assert ls._span(90) == "90 seconds"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_sigterm_during_the_teardown_still_removes_the_record(started, monkeypatch):
    """The verifier's case: SIGTERM while `run` stops its server after the answer."""
    import os
    import signal

    from graph_agents_cli.run._signals import TerminationSignal, terminate_like_interrupt

    _write_pid(started.root, pid=111)

    def stop_and_get_signalled(pid, **_):
        os.kill(os.getpid(), signal.SIGTERM)  # arrives in the middle of the teardown
        started.terminated.append(pid)
        return True

    monkeypatch.setattr(ls, "_terminate_process", stop_and_get_signalled)
    with pytest.raises(TerminationSignal), terminate_like_interrupt():
        ls.stop_server(started.root, pid=111)
    assert started.terminated == [111]
    assert not ls.pid_file_path(started.root).exists()
    # Idempotent: a second teardown finds nothing left to do.
    assert ls.stop_server(started.root) is False


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
    monkeypatch.setattr(ls, "_is_server_alive", lambda pid, port, create_time=None: True)
    assert ls.get_server_port(started.root) == 18085
    monkeypatch.setattr(ls, "_is_server_alive", lambda pid, port, create_time=None: False)
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


# ---------------------------------------------------------------------------
# a sandbox that denies the process table (Codex): the teardown still stops the tree
# ---------------------------------------------------------------------------

# A server stand-in: it starts a child of its own (uvicorn under `uv run`), prints the
# child's PID and waits. Started in a new session like the real one.
_SERVER_TREE = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "print(child.pid, flush=True)\n"
    "time.sleep(60)\n"
)


def _start_tree(**kwargs):
    import subprocess

    return subprocess.Popen(
        [sys.executable, "-c", _SERVER_TREE],
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
        **kwargs,
    )


def _deny_process_listing(monkeypatch):
    """What psutil does when the sandbox refuses sysctl(kern.proc.all): PermissionError."""
    import psutil

    def denied(self, recursive=False):
        raise PermissionError(1, "Operation not permitted (originated from sysctl() malloc 1/3)")

    monkeypatch.setattr(psutil.Process, "children", denied)


def _gone(pid: int, timeout: float = 5.0) -> bool:
    import psutil

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.05)
    return False


def _kill_leftovers(*pids: int) -> None:
    import os
    import signal

    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_the_tree_is_stopped_when_process_listing_is_denied(monkeypatch):
    """psutil's children() raised PermissionError (not a psutil.Error): the server leaked."""
    proc = _start_tree()
    child = int(proc.stdout.readline())
    try:
        _deny_process_listing(monkeypatch)
        assert ls._terminate_process(proc.pid, own_child=True) is True
        assert proc.wait(timeout=5) is not None
        assert _gone(child), "the server's child (uvicorn) survived the teardown"
    finally:
        _kill_leftovers(proc.pid, child)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_a_failed_start_raises_its_own_error_when_process_listing_is_denied(
    monkeypatch, tmp_path: Path
):
    """The start-failure cleanup used to raise PermissionError over the real error."""
    trees = []

    def popen(args, **kwargs):
        trees.append(_start_tree())
        return trees[-1]

    monkeypatch.setattr(ls, "popen_resolved_detached", popen)
    monkeypatch.setattr(ls, "_find_free_port", lambda *a, **k: 18643)
    monkeypatch.delenv(ls.RUN_PORT_ENV, raising=False)
    monkeypatch.setattr(ls, "_fetch_health", lambda port, timeout=1.0: None)
    _deny_process_listing(monkeypatch)
    try:
        with pytest.raises(ls.ServerStartError, match="did not become healthy"):
            ls.ensure_server(tmp_path, "app", runtime="fastapi", startup_timeout=1)
        (proc,) = trees
        child = int(proc.stdout.readline())
        assert proc.poll() is not None
        assert _gone(child)
        assert ls.read_pid_file(tmp_path) is None
    finally:
        for proc in trees:
            _kill_leftovers(proc.pid)


# ---------------------------------------------------------------------------
# ports and the provisional pid record
# ---------------------------------------------------------------------------


def _listen(port: int, host: str = "127.0.0.1"):
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(1)
    return sock


def test_pid_file_is_written_before_the_readiness_wait(started, monkeypatch):
    """A CLI killed while the server boots still leaves a record to stop it by."""
    seen = {}

    def fake_wait(root, port, *, proc=None, timeout=0, sleep=None):
        seen.update(json.loads(ls.pid_file_path(root).read_text()))
        return {"status": "ok"}

    monkeypatch.setattr(ls, "_wait_for_ready", fake_wait)
    ls.ensure_server(started.root, "app", runtime="fastapi")
    assert seen["pid"] == 4242 and seen["state"] == ls.STATE_STARTING
    assert json.loads(ls.pid_file_path(started.root).read_text())["state"] == ls.STATE_READY


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_interrupted_startup_stops_the_server_and_removes_its_record(
    started, monkeypatch, interrupt
):
    def fake_wait(*a, **k):
        raise interrupt()

    monkeypatch.setattr(ls, "_wait_for_ready", fake_wait)
    with pytest.raises(interrupt):
        ls.ensure_server(started.root, "app", runtime="fastapi")
    assert started.terminated == [4242]
    assert not ls.pid_file_path(started.root).exists()


def test_interrupted_startup_never_removes_another_invocations_record(started, monkeypatch):
    def fake_wait(root, *a, **k):
        _write_pid(root, pid=999)  # a concurrent invocation replaced the record
        raise KeyboardInterrupt

    monkeypatch.setattr(ls, "_wait_for_ready", fake_wait)
    with pytest.raises(KeyboardInterrupt):
        ls.ensure_server(started.root, "app", runtime="fastapi")
    assert json.loads(ls.pid_file_path(started.root).read_text())["pid"] == 999


def test_pinned_port_is_used_exactly(started, monkeypatch):
    monkeypatch.setattr(ls, "port_problem", lambda port, host="127.0.0.1": None)
    info = ls.ensure_server(started.root, "app", runtime="fastapi", port=18640)
    assert info.port == 18640
    assert started.popen_calls[0]["args"][-1] == "18640"

    monkeypatch.setenv(ls.RUN_PORT_ENV, "18641")
    ls.pid_file_path(started.root).unlink()
    info = ls.ensure_server(started.root, "app", runtime="fastapi")
    assert info.port == 18641


def test_pinned_port_in_use_is_refused_with_exit_3(started):
    sock = _listen(18642)
    try:
        with pytest.raises(ls.PortUnavailableError) as excinfo:
            ls.ensure_server(started.root, "app", runtime="fastapi", port=18642)
    finally:
        sock.close()
    assert excinfo.value.exit_code == 3
    assert "Cannot start the local server on port 18642" in str(excinfo.value)
    assert ls.RUN_PORT_ENV in str(excinfo.value)
    assert not started.popen_calls


def test_a_wildcard_listener_counts_as_in_use():
    """macOS lets a loopback bind succeed next to *:port, shadowing it on 127.0.0.1."""
    sock = _listen(18643, host="0.0.0.0")
    try:
        assert ls.port_problem(18643) is not None
    finally:
        sock.close()
    assert ls.port_problem(18643) is None


@pytest.mark.skipif(sys.platform == "win32", reason="TIME_WAIT bind semantics are POSIX")
def test_a_port_whose_last_connections_linger_is_free():
    """A restart right after a browser session: only TIME_WAIT/FIN_WAIT_2 sockets remain.

    uvicorn binds with SO_REUSEADDR and starts fine there; the preflight used to
    refuse the port for about 30 seconds.
    """
    import socket

    port = 18861
    server = _listen(port)
    client = socket.create_connection(("127.0.0.1", port))
    conn, _addr = server.accept()
    conn.close()  # the server side closes first: its port goes to FIN_WAIT_2, then TIME_WAIT
    server.close()
    try:
        time.sleep(0.1)
        assert ls.port_problem(port) is None
        client.close()
        time.sleep(0.1)
        assert ls.port_problem(port) is None
    finally:
        client.close()


@pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0"])
def test_bind_probes_still_see_a_listener_that_does_not_answer(monkeypatch, host):
    """The connect probe can miss a listener (a full backlog, a stopped process): the
    binds still refuse it, the wildcard one included (the macOS shadowing case)."""
    import socket

    sock = _listen(18862, host=host)
    try:
        with monkeypatch.context() as patched:
            patched.setattr(
                socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(OSError())
            )
            problem = ls.port_problem(18862)
    finally:
        sock.close()
    assert problem is not None and "cannot be bound" in problem


def test_pinned_port_does_not_silently_reuse_a_server_elsewhere(started, monkeypatch):
    _write_pid(started.root, pid=111, port=18081)
    monkeypatch.setattr(ls, "_is_server_alive", lambda pid, port, create_time=None: True)
    with pytest.raises(ls.PortUnavailableError, match="already running on port 18081"):
        ls.ensure_server(started.root, "app", runtime="fastapi", port=18644)
    # The same port is simply reused.
    info = ls.ensure_server(started.root, "app", runtime="fastapi", port=18081)
    assert info.port == 18081 and not info.started


@pytest.mark.parametrize("raw", ["abc", "0", "70000"])
def test_invalid_port_env_is_a_config_error(monkeypatch, raw):
    monkeypatch.setenv(ls.RUN_PORT_ENV, raw)
    with pytest.raises(click.ClickException) as excinfo:
        ls.requested_port()
    assert excinfo.value.exit_code == 3
    assert ls.RUN_PORT_ENV in str(excinfo.value)


def test_find_free_port_skips_busy_ports(monkeypatch):
    busy = {18080, 18081}

    def fake_problem(port, host="127.0.0.1"):
        return "busy" if port in busy else None

    monkeypatch.setattr(ls, "port_problem", fake_problem)
    assert ls._find_free_port() == 18082
    busy.update(range(18080, 18090))
    with pytest.raises(ls.PortUnavailableError, match="--port or GRAPH_AGENTS_CLI_RUN_PORT"):
        ls._find_free_port()


def test_the_server_gets_env_settings_from_its_start_below_the_shell(started, monkeypatch):
    """Settings read while the app is assembled (auth startup check, A2A card) see .env."""
    (started.root / ".env").write_text(
        "AUTH_POLICY=jwt\nAPP_ENV=dev\nAUTH_JWT_AUDIENCE=from-dotenv\nPORT=1\nEMPTY\n"
        "AUTH_JWT_PUBLIC_KEY='-----BEGIN PUBLIC KEY-----\\nAAA\\n-----END PUBLIC KEY-----'\n"
    )
    monkeypatch.setenv("AUTH_JWT_AUDIENCE", "from-shell")
    ls.ensure_server(started.root, "app", runtime="fastapi")
    env = started.popen_calls[0]["env"]
    assert env["AUTH_POLICY"] == "jwt" and env["APP_ENV"] == "dev"
    assert env["AUTH_JWT_AUDIENCE"] == "from-shell"  # the environment wins, as with load_dotenv
    assert env["PORT"] == "18080"  # the port this run chose
    # The A2A card advertises where this server listens (langgraph dev loads .env over PORT).
    assert env["APP_URL"] == "http://127.0.0.1:18080"
    assert "EMPTY" not in env
    assert env["AUTH_JWT_PUBLIC_KEY"].startswith("-----BEGIN PUBLIC KEY-----\\nAAA")


def test_dotenv_settings_tolerates_a_missing_or_broken_file(tmp_path: Path):
    assert ls.dotenv_settings(tmp_path / ".env") == {}
    (tmp_path / ".env").write_bytes(b"\xff\xfe not text")
    assert isinstance(ls.dotenv_settings(tmp_path / ".env"), dict)


def test_an_app_url_the_project_sets_is_kept(started, monkeypatch):
    (started.root / ".env").write_text("APP_URL=https://agent.example\n")
    ls.ensure_server(started.root, "app", runtime="langgraph-server")
    assert started.popen_calls[0]["env"]["APP_URL"] == "https://agent.example"
