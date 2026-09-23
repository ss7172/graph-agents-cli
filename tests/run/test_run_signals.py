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
from graph_agents_cli.run._signals import TerminationSignal, terminate_like_interrupt

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
