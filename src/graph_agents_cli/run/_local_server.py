# Copyright 2026 Google LLC
# Modifications Copyright 2026 graph-agents-cli contributors
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

"""Background local server management for ``run`` and ``eval generate``.

The pid file is ``.graph-agents-cli/run_server.json``
with keys ``{pid, port, started_at, last_activity, runtime, checkpointer, state}``.
The command depends on the manifest ``runtime``:

* ``fastapi``          -> ``uv run uvicorn <agent_dir>.fast_api_app:app --host 127.0.0.1 --port N``
* ``langgraph-server`` -> ``uv run langgraph dev --no-browser --port N``

The port is ``--port`` (``run``) or ``GRAPH_AGENTS_CLI_RUN_PORT`` when given
(used exactly, refused when busy), else the first free one of 18080-18089. A
port counts as busy when anything answers on 127.0.0.1 or it cannot be bound on
127.0.0.1 and on all interfaces: on macOS a loopback bind succeeds next to a
wildcard listener and would silently shadow it. A server idle for 30 minutes
is replaced. A live server started for a different runtime is a hard error
(never terminated silently). Readiness is ``GET /health`` answering 200.

The pid file is written as soon as the process is started (``"state":
"starting"``) and completed once it is ready, so a CLI killed during startup
still leaves a record that ``run --stop-server`` and the next invocation find.

Two invocations may race for the same project (``eval generate`` beside a
``run``, two CI steps): the check-start-write sequence runs under a lock file
(``.graph-agents-cli/run_server.lock``), the pid file is written atomically,
and ``stop_server(pid=...)`` always stops the process this invocation started
even when the pid file has since been replaced by another invocation.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

import click
import httpx
import psutil
from filelock import FileLock
from filelock import Timeout as LockTimeout

from graph_agents_cli._runner import popen_resolved_detached, redact_cmd

PID_DIR = ".graph-agents-cli"
PID_FILENAME = "run_server.json"
LOCK_FILENAME = "run_server.lock"
LOG_FILENAME = "run_server.log"
STDERR_LOG_FILENAME = "run_server.stderr.log"
BASE_PORT = 18080
_MAX_PORT_ATTEMPTS = 10
RUN_PORT_ENV = "GRAPH_AGENTS_CLI_RUN_PORT"
# Exit code for a port that cannot be used: the fix is configuration (--port).
EXIT_PORT_UNAVAILABLE = 3
STATE_STARTING = "starting"
STATE_READY = "ready"
DEFAULT_IDLE_TIMEOUT = 1800  # 30 minutes
_STARTUP_TIMEOUT_POSIX = 60
_STARTUP_TIMEOUT_WINDOWS = 120
DEFAULT_STARTUP_TIMEOUT = _STARTUP_TIMEOUT_WINDOWS if os.name == "nt" else _STARTUP_TIMEOUT_POSIX
# Extra seconds a second invocation waits for the first one to finish starting.
_LOCK_GRACE = 30
DEFAULT_HEARTBEAT_INTERVAL = 60.0

RUNTIME_FASTAPI = "fastapi"
RUNTIME_LANGGRAPH_SERVER = "langgraph-server"
SUPPORTED_RUNTIMES = (RUNTIME_FASTAPI, RUNTIME_LANGGRAPH_SERVER)


class ServerInfo(NamedTuple):
    """A running local server's port, PID, and whether *this* call started it.

    ``started`` is ``True`` only when ``ensure_server`` launched a new
    process; it is ``False`` when an already-running server was reused.
    Callers use it to avoid tearing down a server someone else is keeping
    alive (e.g. one started with ``--start-server``).
    """

    port: int
    started: bool
    pid: int
    runtime: str = RUNTIME_FASTAPI
    checkpointer: str = "memory"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def build_serve_command(*, agent_dir: str, port: int, runtime: str) -> list[str]:
    """Return the command that serves the application locally for ``runtime``."""
    if runtime == RUNTIME_FASTAPI:
        return [
            "uv",
            "run",
            "uvicorn",
            f"{agent_dir}.fast_api_app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ]
    if runtime == RUNTIME_LANGGRAPH_SERVER:
        return ["uv", "run", "langgraph", "dev", "--no-browser", "--port", str(port)]
    raise click.ClickException(
        f"Unsupported runtime {runtime!r} in graph-agents-cli-manifest.yaml.\n"
        f"  Expected one of: {', '.join(SUPPORTED_RUNTIMES)}"
    )


class PortUnavailableError(click.ClickException):
    """The requested local port is invalid or taken, or every candidate is (exit 3)."""

    exit_code = EXIT_PORT_UNAVAILABLE


def port_problem(port: int, host: str = "127.0.0.1") -> str | None:
    """Why ``port`` cannot be used for a local server on ``host``, or None when it is free.

    Three probes, because each misses a case: something already answering on
    127.0.0.1 (any listener that would receive our traffic), a bind on ``host``
    (the address the server will use), and a bind on all interfaces, which
    fails next to a wildcard listener even where the loopback bind would
    succeed (macOS), so the new server would shadow the other one on loopback.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return f"something is already listening on 127.0.0.1:{port}"
    except OSError:
        pass
    for address in dict.fromkeys((host, "0.0.0.0")):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind((address, port))
        except OSError as exc:
            where = "all interfaces" if address == "0.0.0.0" else address
            return f"port {port} cannot be bound on {where} ({exc.strerror or exc})"
    return None


def requested_port(explicit: int | None = None) -> int | None:
    """``explicit`` (``--port``), else ``GRAPH_AGENTS_CLI_RUN_PORT``, else None (search)."""
    if explicit is not None:
        return _valid_port(explicit, "--port")
    raw = os.environ.get(RUN_PORT_ENV, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise PortUnavailableError(f"{RUN_PORT_ENV}={raw!r} is not a port number.") from None
    return _valid_port(value, RUN_PORT_ENV)


def _valid_port(port: int, source: str) -> int:
    if not 1 <= port <= 65535:
        raise PortUnavailableError(f"{source} must be between 1 and 65535 (got {port}).")
    return port


def ensure_server(
    project_root: Path,
    agent_dir: str,
    *,
    runtime: str,
    checkpointer: str = "memory",
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
    keep_running: bool = False,
    startup_timeout: int = DEFAULT_STARTUP_TIMEOUT,
    lock_timeout: float | None = None,
    port: int | None = None,
) -> ServerInfo:
    """Return a running local server's port, starting one if needed.

    A recorded server is reused when its process is alive, its port answers,
    it was started for the same ``runtime``, and it has been active within
    ``idle_timeout`` seconds. A stale pid file is cleaned up; an idle server is
    stopped and replaced. A live server for a different runtime is a hard
    error and is left running so the user can decide.

    ``checkpointer`` is the manifest value; the value recorded in the pid file
    is what ``GET /health`` reports once the server is up (the local ``.env``
    usually selects ``memory``), falling back to the argument.

    ``port`` (or ``GRAPH_AGENTS_CLI_RUN_PORT``) pins the port of a server this
    call starts; it must be free (:class:`PortUnavailableError` otherwise), and
    a running server on another port is not reused silently.

    The whole read -> check -> start -> wait -> write sequence holds the
    project's lock file, so a concurrent invocation waits (up to
    ``lock_timeout``, default the startup timeout plus a grace period) and
    then reuses the server instead of starting a second one.
    """
    if runtime not in SUPPORTED_RUNTIMES:
        raise click.ClickException(
            f"Unsupported runtime {runtime!r} in graph-agents-cli-manifest.yaml.\n"
            f"  Expected one of: {', '.join(SUPPORTED_RUNTIMES)}"
        )

    pinned_port = requested_port(port)
    state_dir = project_root / PID_DIR
    state_dir.mkdir(exist_ok=True)
    wait = float(startup_timeout + _LOCK_GRACE) if lock_timeout is None else lock_timeout
    lock = FileLock(str(state_dir / LOCK_FILENAME))
    try:
        with lock.acquire(timeout=wait):
            return _ensure_server_locked(
                project_root,
                agent_dir,
                runtime=runtime,
                checkpointer=checkpointer,
                idle_timeout=idle_timeout,
                keep_running=keep_running,
                startup_timeout=startup_timeout,
                pinned_port=pinned_port,
            )
    except LockTimeout as exc:
        raise click.ClickException(
            f"Another graph-agents-cli invocation has held the local server lock for more than "
            f"{wait:.0f}s ({PID_DIR}/{LOCK_FILENAME}).\n"
            "  Wait for it to finish, or remove the lock file if that process is gone."
        ) from exc


def _ensure_server_locked(
    project_root: Path,
    agent_dir: str,
    *,
    runtime: str,
    checkpointer: str,
    idle_timeout: int,
    keep_running: bool,
    startup_timeout: int,
    pinned_port: int | None = None,
) -> ServerInfo:
    info = read_pid_file(project_root)
    if info:
        if _is_server_alive(info.get("pid", 0), info.get("port", 0)):
            if pinned_port is not None and info.get("port") != pinned_port:
                raise PortUnavailableError(
                    f"This project's local server is already running on port "
                    f"{info.get('port')}, not {pinned_port}.\n"
                    "  Drop --port / GRAPH_AGENTS_CLI_RUN_PORT to reuse it, or stop it first: "
                    "graph-agents-cli run --stop-server"
                )
            existing_runtime = info.get("runtime")
            if existing_runtime and existing_runtime != runtime:
                raise click.ClickException(
                    f"Cannot reuse the running local server: it was started for the "
                    f"{existing_runtime!r} runtime, but the project now uses {runtime!r}.\n"
                    "  Run 'graph-agents-cli run --stop-server' first, then retry."
                )
            if _is_idle(info, idle_timeout):
                _cleanup(project_root, info)
            else:
                _update_activity(project_root)
                return ServerInfo(
                    port=info["port"],
                    started=False,
                    pid=info["pid"],
                    runtime=existing_runtime or runtime,
                    checkpointer=info.get("checkpointer") or checkpointer,
                )
        else:
            # Stale pid file: clean up before starting fresh.
            _cleanup(project_root, info)

    if pinned_port is not None:
        problem = port_problem(pinned_port)
        if problem:
            raise PortUnavailableError(
                f"Cannot start the local server on port {pinned_port}: {problem}.\n"
                f"  Pick another one with --port or {RUN_PORT_ENV}."
            )
        port = pinned_port
    else:
        port = _find_free_port()
    proc = _start_server(project_root=project_root, agent_dir=agent_dir, port=port, runtime=runtime)
    pid = proc.pid
    try:
        # Recorded before the (long) readiness wait: if this CLI is killed now,
        # `run --stop-server` and the next invocation still find the process.
        write_pid_file(
            project_root,
            pid=pid,
            port=port,
            runtime=runtime,
            checkpointer=checkpointer,
            state=STATE_STARTING,
        )
        health = _wait_for_ready(project_root, port, proc=proc, timeout=startup_timeout)
    except BaseException:
        # A server that failed to come up (or a Ctrl-C/SIGTERM while waiting)
        # must not keep running in the background on the chosen port. A child
        # that already exited was reaped by poll(); terminating it again would
        # only log a spurious "not found" warning.
        if proc.poll() is None:
            _terminate_process(pid)
            try:
                proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                pass
        _remove_pid_file_if(project_root, pid)
        raise
    live_checkpointer = str(health.get("checkpointer") or checkpointer)
    write_pid_file(
        project_root,
        pid=pid,
        port=port,
        runtime=runtime,
        checkpointer=live_checkpointer,
        state=STATE_READY,
    )
    if keep_running:
        click.secho(f"Local server started on port {port} (PID {pid}, {runtime}).", dim=True)
        click.secho("  Stop with: graph-agents-cli run --stop-server", dim=True)
    else:
        click.secho(
            f"Starting a temporary local server on port {port} ({runtime}; "
            "stops automatically when done).",
            dim=True,
        )
    return ServerInfo(
        port=port, started=True, pid=pid, runtime=runtime, checkpointer=live_checkpointer
    )


def stop_server(project_root: Path, pid: int | None = None) -> bool:
    """Stop the background server.

    Without ``pid`` the recorded server is stopped and the pid file removed.
    With ``pid`` (a server this invocation started): when the pid file still
    names it, same thing; when the file is missing or names another process
    (a concurrent invocation replaced it), only ``pid`` is terminated and the
    file is left alone, so a server started by this invocation is never
    orphaned and another invocation's record is never deleted.

    Returns ``True`` if a server was stopped.
    """
    info = read_pid_file(project_root)
    if pid is not None and (not info or info.get("pid") != pid):
        _terminate_process(pid)
        click.secho("Local server stopped.", dim=True)
        return True
    if not info:
        return False
    _cleanup(project_root, info)
    click.secho("Local server stopped.", dim=True)
    return True


def get_server_port(project_root: Path) -> int | None:
    """Return the port of the running local server, or ``None`` if absent."""
    info = read_pid_file(project_root)
    if not info:
        return None
    if not _is_server_alive(info.get("pid", 0), info.get("port", 0)):
        return None
    return info["port"]


def touch_activity(project_root: Path) -> None:
    """Stamp ``last_activity`` on the pid file (a request was just served)."""
    _update_activity(project_root)


def start_activity_heartbeat(
    project_root: Path, interval: float = DEFAULT_HEARTBEAT_INTERVAL
) -> Callable[[], None]:
    """Stamp ``last_activity`` every ``interval`` seconds until the returned stop() is called.

    A long ``eval generate`` session otherwise looks idle to a concurrent
    ``run`` after 30 minutes, which would replace the server under it.
    """
    stop = threading.Event()

    def _beat() -> None:
        while not stop.wait(interval):
            _update_activity(project_root)

    thread = threading.Thread(target=_beat, name="graph-agents-cli-heartbeat", daemon=True)
    thread.start()

    def _stop() -> None:
        stop.set()
        thread.join(timeout=1)

    return _stop


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_free_port(base: int | None = None, max_attempts: int = _MAX_PORT_ATTEMPTS) -> int:
    """The first free local port (see :func:`port_problem`) from *base* (``BASE_PORT``)."""
    base = BASE_PORT if base is None else base
    for offset in range(max_attempts):
        port = base + offset
        if port_problem(port) is None:
            return port
    raise PortUnavailableError(
        f"No free port found in range {base}-{base + max_attempts - 1}.\n"
        f"  Pick one with --port or {RUN_PORT_ENV}, stop other servers "
        "(graph-agents-cli run --stop-server), or use --url to query a remote agent."
    )


def _start_server(
    *, project_root: Path, agent_dir: str, port: int, runtime: str
) -> subprocess.Popen:
    """Start the local server as a detached background process and return its handle.

    The ``Popen`` is what ``_wait_for_ready`` polls: ``psutil.pid_exists`` is
    true for a zombie, so a child that crashed at import time would otherwise
    only be noticed when the readiness timeout expires.
    """
    state_dir = project_root / PID_DIR
    state_dir.mkdir(exist_ok=True)
    log_path = state_dir / LOG_FILENAME

    cmd = build_serve_command(agent_dir=agent_dir, port=port, runtime=runtime)

    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    # The app reads PORT for its own logging; keep it consistent with --port.
    env["PORT"] = str(port)

    log_file = open(log_path, "a", encoding="utf-8")
    stderr_file = None
    try:
        log_file.write(f"=== Starting server at {datetime.now(UTC).isoformat()} ===\n")
        log_file.write(f"Command: {redact_cmd(cmd)}\n")
        log_file.write(f"CWD: {project_root}\n")
        log_file.flush()

        if sys.platform == "win32":
            stderr_path = state_dir / STDERR_LOG_FILENAME
            stderr_file = open(stderr_path, "a", encoding="utf-8")
            stderr_file.write(
                f"=== Starting server stderr at {datetime.now(UTC).isoformat()} ===\n"
            )
            stderr_file.flush()
            child_stdout, child_stderr = log_file, stderr_file
        else:
            child_stdout = child_stderr = log_file

        proc = popen_resolved_detached(
            cmd,
            cwd=str(project_root),
            stdout=child_stdout,
            stderr=child_stderr,
            env=env,
        )
        log_file.write(f"=== Started server process {proc.pid} ===\n")
        log_file.flush()
    finally:
        # Close the parent's copy of the fd; the child inherits its own.
        log_file.close()
        if stderr_file:
            stderr_file.close()
    return proc


def _fetch_health(port: int, timeout: float = 1.0) -> dict[str, Any] | None:
    """GET ``/health`` on the local port; ``None`` until it answers 200."""
    try:
        resp = httpx.get(f"http://127.0.0.1:{port}/health", timeout=timeout)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _tail_log(path: Path, limit: int = 50) -> str:
    if not path.exists():
        return "<Log file does not exist>"
    try:
        lines: list[str] = []
        truncated = False
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                lines.append(line)
                if len(lines) > limit:
                    lines.pop(0)
                    truncated = True
        content = "".join(lines)
        return "... (truncated) ...\n" + content if truncated else content
    except Exception as exc:
        return f"<Failed to read log file: {exc}>"


def _log_hint(project_root: Path) -> str:
    log_name = STDERR_LOG_FILENAME if sys.platform == "win32" else LOG_FILENAME
    return (
        f"  Check logs: {PID_DIR}/{LOG_FILENAME}\n"
        f"  Log content:\n{_tail_log(project_root / PID_DIR / log_name)}"
    )


def _wait_for_ready(
    project_root: Path,
    port: int,
    *,
    proc: subprocess.Popen | None = None,
    timeout: int = DEFAULT_STARTUP_TIMEOUT,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Wait until ``GET /health`` answers 200 and return its body.

    ``proc.poll()`` (which also reaps the child) fails fast with the exit code
    and the log tail when the server process exits during startup.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            raise click.ClickException(
                f"Local server process exited during startup (exit code {proc.returncode}).\n"
                + _log_hint(project_root)
            )
        health = _fetch_health(port)
        if health is not None:
            return health
        sleep(0.3)

    raise click.ClickException(
        f"Local server did not become healthy within {timeout}s (GET /health).\n"
        + _log_hint(project_root)
    )


# --- pid file helpers ---


def pid_file_path(project_root: Path) -> Path:
    return project_root / PID_DIR / PID_FILENAME


def read_pid_file(project_root: Path) -> dict[str, Any] | None:
    path = pid_file_path(project_root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    """Write ``data`` to ``path`` through a temporary file so readers never see a torn file."""
    path.parent.mkdir(exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2) + "\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_pid_file(
    project_root: Path,
    *,
    pid: int,
    port: int,
    runtime: str,
    checkpointer: str,
    state: str = STATE_READY,
) -> None:
    now = datetime.now(UTC).isoformat()
    data = {
        "pid": pid,
        "port": port,
        "started_at": now,
        "last_activity": now,
        "runtime": runtime,
        "checkpointer": checkpointer,
        "state": state,
    }
    _write_json_atomic(pid_file_path(project_root), data)


def _remove_pid_file_if(project_root: Path, pid: int) -> None:
    """Remove the pid file when it still names ``pid`` (never another invocation's)."""
    info = read_pid_file(project_root)
    if info and info.get("pid") == pid:
        try:
            pid_file_path(project_root).unlink(missing_ok=True)
        except OSError as exc:
            logging.warning("Failed to remove pid file %s: %s", pid_file_path(project_root), exc)


def _update_activity(project_root: Path) -> None:
    """Stamp ``last_activity`` so idle detection resets."""
    path = pid_file_path(project_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        data["last_activity"] = datetime.now(UTC).isoformat()
        _write_json_atomic(path, data)
    except (json.JSONDecodeError, OSError):
        pass


def _is_idle(info: dict, idle_timeout: int) -> bool:
    """Return ``True`` if the server has been idle longer than *idle_timeout*."""
    try:
        last = datetime.fromisoformat(info["last_activity"])
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        idle = (datetime.now(UTC) - last).total_seconds()
        return idle > idle_timeout
    except (KeyError, TypeError, ValueError):
        # Treat missing or unparseable timestamps as stale.
        return True


def _is_server_alive(pid: int, port: int) -> bool:
    """Return ``True`` if the process exists AND the port is open."""
    if not pid or not port or not psutil.pid_exists(pid):
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _terminate_process(pid: int) -> None:
    """Terminate ``pid`` and its children; a vanished process is not an error."""
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        parent.terminate()
        psutil.wait_procs([*children, parent], timeout=3)
    except psutil.NoSuchProcess:
        logging.warning("Local server process with PID %d not found, skipping termination.", pid)


def _cleanup(project_root: Path, info: dict) -> None:
    """Terminate the server process and remove the pid file."""
    pid = info.get("pid")
    if pid:
        _terminate_process(pid)
    path = pid_file_path(project_root)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logging.warning("Failed to remove pid file %s: %s", path, exc)
