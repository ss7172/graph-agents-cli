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

"""The root group's error presentation: a user cancel is not a traceback."""

from __future__ import annotations

import click
import pytest
from click.testing import CliRunner

from graph_agents_cli import main as main_module


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch):
    """Register a throwaway subcommand on the real root group."""
    monkeypatch.setenv("GRAPH_AGENTS_CLI_NO_UPDATE_CHECK", "1")
    monkeypatch.setenv("GRAPH_AGENTS_CLI_DISABLE_OVERRIDES", "1")

    def _register(fn):
        command = click.command("stub-cmd")(fn)
        main_module.main.add_command(command)
        return command

    yield _register
    main_module.main.commands.pop("stub-cmd", None)


def test_click_abort_prints_aborted_without_a_traceback(stub) -> None:
    @stub
    def _raise_abort():
        raise click.Abort()

    result = CliRunner().invoke(main_module.main, ["stub-cmd"])
    assert result.exit_code == 1
    assert result.output.strip() == "Aborted!"
    assert "Traceback" not in result.output


def test_declined_confirm_is_a_clean_abort(stub) -> None:
    @stub
    def _confirm():
        click.confirm("Continue without backup?", abort=True)

    result = CliRunner().invoke(main_module.main, ["stub-cmd"], input="n\n")
    assert result.exit_code == 1
    assert result.output.rstrip().endswith("Aborted!")
    assert "Traceback" not in result.output
    # EOF at a prompt (Ctrl-D) is the same clean abort.
    result = CliRunner().invoke(main_module.main, ["stub-cmd"], input="")
    assert result.exit_code == 1 and "Traceback" not in result.output


def test_unexpected_exception_still_prints_the_traceback(stub) -> None:
    """A bug keeps its traceback; exit 2 (tool failure), never 1 (a failed gate)."""

    @stub
    def _boom():
        raise RuntimeError("boom")

    result = CliRunner().invoke(main_module.main, ["stub-cmd"])
    assert result.exit_code == 2
    assert "Traceback" in result.output and "boom" in result.output


class LangSmithConnectionError(Exception):
    """Named like the SDK's class: matched by name, the SDK is never imported."""


@pytest.mark.parametrize(
    ("exc", "code", "fragment"),
    [
        (ConnectionRefusedError(61, "Connection refused"), 2, "network error"),
        (LangSmithConnectionError("POST /datasets failed\nmore"), 2, "network error"),
        (PermissionError(13, "Permission denied"), 2, "PermissionError"),
        (__import__("json").JSONDecodeError("Expecting value", "x", 0), 3, "invalid JSON"),
    ],
)
def test_environment_errors_are_one_line(stub, monkeypatch, exc, code, fragment) -> None:
    monkeypatch.delenv(main_module.DEBUG_ENV, raising=False)

    @stub
    def _fail():
        raise exc

    result = CliRunner().invoke(main_module.main, ["stub-cmd"])
    assert result.exit_code == code, result.output
    assert fragment in result.output
    assert "Traceback" not in result.output
    assert f"set {main_module.DEBUG_ENV}=1" in result.output
    assert "more" not in result.output.split("Error:", 1)[1].splitlines()[0]


def test_yaml_errors_are_configuration_errors(stub, monkeypatch) -> None:
    import yaml

    @stub
    def _fail():
        yaml.safe_load("a: [unclosed")

    result = CliRunner().invoke(main_module.main, ["stub-cmd"])
    assert result.exit_code == 3, result.output
    assert "invalid YAML" in result.output and "Traceback" not in result.output


def test_debug_env_shows_the_traceback(stub, monkeypatch) -> None:
    monkeypatch.setenv(main_module.DEBUG_ENV, "1")

    @stub
    def _fail():
        raise ConnectionResetError("reset")

    result = CliRunner().invoke(main_module.main, ["stub-cmd"])
    assert result.exit_code == 2
    assert "Traceback" in result.output


def test_log_warnings_are_not_printed_as_warning_root(monkeypatch, capsys) -> None:
    """Without a configured handler, logging.warning printed 'WARNING:root:...'."""
    import logging

    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", logging.WARNING)
    main_module._configure_logging()
    try:
        logging.warning("graph-agents-cli: applying %d extension command(s)", 1)
        logging.error("broken")
        logging.info("quiet")
    finally:
        root.handlers = []
    err = capsys.readouterr().err
    assert "Warning: graph-agents-cli: applying 1 extension command(s)" in err
    assert "Error: broken" in err
    assert "WARNING:root" not in err and "quiet" not in err
