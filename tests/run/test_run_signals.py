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

"""SIGTERM/SIGHUP unwind like Ctrl-C around the commands that start servers."""

from __future__ import annotations

import os
import signal
import sys
import threading
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from graph_agents_cli.run import cmd_run
from graph_agents_cli.run._signals import TerminationSignal, shielded, terminate_like_interrupt

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")


@posix_only
def test_sigterm_raises_termination_signal_and_restores_the_handler():
    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(TerminationSignal) as excinfo, terminate_like_interrupt():
        os.kill(os.getpid(), signal.SIGTERM)
        signal.pause() if hasattr(signal, "pause") else None
    assert excinfo.value.exit_code == 128 + signal.SIGTERM
    assert isinstance(excinfo.value, KeyboardInterrupt)
    assert signal.getsignal(signal.SIGTERM) == before


@posix_only
def test_a_second_signal_does_not_interrupt_the_cleanup():
    cleaned = []
    with pytest.raises(TerminationSignal), terminate_like_interrupt():
        try:
            os.kill(os.getpid(), signal.SIGTERM)
            signal.pause()
        finally:
            # Cleanup (stopping the server) runs while a second SIGTERM arrives.
            os.kill(os.getpid(), signal.SIGHUP)
            cleaned.append(True)
    assert cleaned == [True]


@posix_only
def test_a_signal_during_a_shielded_cleanup_is_handled_after_it():
    """The normal-path teardown: SIGTERM arrives while the server is being stopped."""
    steps = []
    with pytest.raises(TerminationSignal) as excinfo, terminate_like_interrupt():
        with shielded():
            os.kill(os.getpid(), signal.SIGTERM)
            steps.append("server stopped")  # not cut short by the signal
            steps.append("record removed")
        steps.append("never reached")
    assert steps == ["server stopped", "record removed"]
    assert excinfo.value.exit_code == 128 + signal.SIGTERM


@posix_only
def test_shielded_holds_ctrl_c_and_restores_the_handlers():
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    done = []
    with pytest.raises(KeyboardInterrupt), shielded():
        os.kill(os.getpid(), signal.SIGINT)
        os.kill(os.getpid(), signal.SIGINT)  # a second Ctrl-C is not a second exception
        done.append(True)
    assert done == [True]
    assert {sig: signal.getsignal(sig) for sig in before} == before


@posix_only
def test_shielded_drops_a_signal_that_was_being_ignored():
    previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        with shielded():
            os.kill(os.getpid(), signal.SIGHUP)
        assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, previous)


def test_outside_the_main_thread_it_is_a_no_op():
    seen = []

    def work():
        with terminate_like_interrupt():
            seen.append(signal.getsignal(signal.SIGTERM))

    before = signal.getsignal(signal.SIGTERM)
    thread = threading.Thread(target=work)
    thread.start()
    thread.join()
    assert seen == [before]


def test_run_exits_143_and_stops_nothing_it_did_not_start(monkeypatch, tmp_path):
    """A SIGTERM while the server starts ends `run` with 128+15 through the main group."""
    from graph_agents_cli.main import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GRAPH_AGENTS_CLI_NO_UPDATE_CHECK", "1")
    monkeypatch.setenv("GRAPH_AGENTS_CLI_DISABLE_OVERRIDES", "1")
    cfg = SimpleNamespace(agent_directory="app", runtime="fastapi", checkpointer="memory")
    monkeypatch.setattr(cmd_run, "chdir_project_root", lambda *a, **k: None)
    monkeypatch.setattr(cmd_run, "read_project_config", lambda *a, **k: cfg)
    monkeypatch.setattr(cmd_run, "require_agent_directory", lambda cfg: None)

    def boom(*a, **k):
        raise TerminationSignal(signal.SIGTERM)

    monkeypatch.setattr(cmd_run, "ensure_server", boom)
    result = CliRunner().invoke(main, ["run", "hi"])
    assert result.exit_code == 128 + signal.SIGTERM, result.output
    assert "terminated by a signal" in result.output


@posix_only
def test_run_exits_2_when_the_local_server_dies_during_startup(monkeypatch, tmp_path):
    """A server that cannot start is a tool failure (2), as `run --help` documents."""
    from graph_agents_cli.main import main
    from graph_agents_cli.run import _local_server

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GRAPH_AGENTS_CLI_NO_UPDATE_CHECK", "1")
    monkeypatch.setenv("GRAPH_AGENTS_CLI_DISABLE_OVERRIDES", "1")
    monkeypatch.setenv(_local_server.RUN_PORT_ENV, "18864")
    cfg = SimpleNamespace(agent_directory="app", runtime="fastapi", checkpointer="memory")
    monkeypatch.setattr(cmd_run, "chdir_project_root", lambda *a, **k: None)
    monkeypatch.setattr(cmd_run, "read_project_config", lambda *a, **k: cfg)
    monkeypatch.setattr(cmd_run, "require_agent_directory", lambda cfg: None)
    # The real server manager, with a "server" that exits at once (a missing key, say).
    monkeypatch.setattr(
        _local_server,
        "build_serve_command",
        lambda **kw: [sys.executable, "-c", "import sys; sys.exit(3)"],
    )
    result = CliRunner().invoke(main, ["run", "hi"])
    assert result.exit_code == 2, result.output
    assert "exited during startup (exit code 3)" in result.output
    assert not (tmp_path / _local_server.PID_DIR / _local_server.PID_FILENAME).exists()
