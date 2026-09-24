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

"""Every lazy command loads, answers --help, and its short_help matches its docstring.

`main --help` prints the `short_help` strings registered in `main.py` without
importing the command modules, so a drifted string would only show up to a
user. This test imports each command (and each subcommand of the lazy groups)
and checks that the registered short help equals the first line of the
command's docstring.
"""

from __future__ import annotations

import importlib

import click
import pytest
from click.testing import CliRunner

from graph_agents_cli.main import main

ENV = {"GRAPH_AGENTS_CLI_NO_UPDATE_CHECK": "1", "GRAPH_AGENTS_CLI_DISABLE_OVERRIDES": "1"}


def _load(import_path: str) -> click.Command:
    module_path, attr = import_path.split(":")
    return getattr(importlib.import_module(module_path), attr)


def _lazy_entries(
    group: click.Group, prefix: tuple[str, ...] = ()
) -> list[tuple[tuple[str, ...], str | None, str | None]]:
    """``(path, import_path, short_help)`` for every command, recursively.

    Lazy commands carry the ``short_help`` registered with ``add_lazy_command``.
    Eagerly registered subcommands (``secrets apply``, ``infra check``) carry
    ``import_path=None`` and the explicit ``short_help`` of their decorator, if any.
    """
    entries: list[tuple[tuple[str, ...], str | None, str | None]] = []
    lazy = getattr(group, "_lazy_commands", {})
    for name, (import_path, short_help) in sorted(lazy.items()):
        path = (*prefix, name)
        entries.append((path, import_path, short_help))
        cmd = _load(import_path)
        if isinstance(cmd, click.Group):
            entries.extend(_lazy_entries(cmd, path))
    for name, cmd in sorted(group.commands.items()):
        if name in lazy:
            continue
        path = (*prefix, name)
        entries.append((path, None, cmd.short_help))
        if isinstance(cmd, click.Group):
            entries.extend(_lazy_entries(cmd, path))
    return entries


ENTRIES = _lazy_entries(main)
IDS = [" ".join(path) for path, _, _ in ENTRIES]


def _first_doc_line(cmd: click.Command) -> str:
    assert cmd.help, f"{cmd.name} has no docstring"
    return cmd.help.strip().splitlines()[0].strip()


def test_every_command_is_registered() -> None:
    top_level = {path[0] for path, _, _ in ENTRIES if len(path) == 1}
    expected = {
        "setup",
        "update",
        "login",
        "create",
        "scaffold",
        "playground",
        "run",
        "install",
        "lint",
        "api",
        "build",
        "eval",
        "deploy",
        "secrets",
        "infra",
        "extension",
        "info",
    }
    assert expected <= top_level, sorted(expected - top_level)
    nested = {path for path, _, _ in ENTRIES if len(path) > 1}
    for group, subs in {
        "scaffold": ("create", "enhance", "upgrade"),
        "eval": ("run", "generate", "grade", "compare", "analyze", "submit", "metric"),
        "secrets": ("apply", "status"),
        "infra": ("check",),
        "api": ("add", "remove", "access", "allow", "deny", "revoke", "limits", "show", "check"),
        "extension": ("add", "list", "remove", "update"),
    }.items():
        for sub in subs:
            assert (group, sub) in nested, f"{group} {sub} is not a subcommand"
    assert ("eval", "metric", "list") in nested


def _resolve(path: tuple[str, ...]) -> click.Command:
    cmd: click.Command = main
    ctx = click.Context(main)
    for name in path:
        assert isinstance(cmd, click.Group), f"{' '.join(path)}: {cmd.name} is not a group"
        nxt = cmd.get_command(ctx, name)
        assert nxt is not None, f"{' '.join(path)}: {name} not found"
        cmd = nxt
    return cmd


@pytest.mark.parametrize(("path", "import_path", "short_help"), ENTRIES, ids=IDS)
def test_short_help_matches_docstring(path, import_path, short_help) -> None:
    cmd = _load(import_path) if import_path else _resolve(path)
    assert isinstance(cmd, click.Command)
    if short_help is None:
        # Eager subcommand without an explicit short_help: Click derives it.
        return
    assert _first_doc_line(cmd) == short_help, (
        f"`{' '.join(path)}` short_help {short_help!r} != docstring first line "
        f"{_first_doc_line(cmd)!r}"
    )


@pytest.mark.parametrize(("path", "import_path", "short_help"), ENTRIES, ids=IDS)
def test_help_succeeds(path, import_path, short_help) -> None:
    result = CliRunner().invoke(main, [*path, "--help"], env=ENV)
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output
    assert "Source:" in result.output


def test_root_help_lists_every_lazy_command() -> None:
    # A wide terminal keeps each short_help on one line.
    result = CliRunner().invoke(main, ["--help"], env=ENV, terminal_width=200)
    assert result.exit_code == 0, result.output
    for path, _, short_help in ENTRIES:
        if len(path) == 1:
            assert path[0] in result.output
            assert short_help in result.output
