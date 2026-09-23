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
    @stub
    def _boom():
        raise RuntimeError("boom")

    result = CliRunner().invoke(main_module.main, ["stub-cmd"])
    assert result.exit_code == 1
    assert "Traceback" in result.output and "boom" in result.output
