# Copyright 2026 Google LLC
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

"""Click helpers: lazy command loading and source-path display in --help."""

from __future__ import annotations

import importlib
import inspect
from typing import Any

import click

from graph_agents_cli._experiments import resolve_experiment


class LazyGroup(click.Group):
    """Click group that defers importing subcommand modules until needed.

    Register subcommands with `add_lazy_command(name, "module.path:obj",
    short_help)`. The module is imported only when the command is actually
    invoked (e.g. `tool name ...`) or when its own help is requested
    (`tool name --help`). The parent group's --help (`tool --help`) renders
    the supplied `short_help` strings directly without triggering any imports.

    `short_help` must match the real command's docstring summary; the parity
    test in tests/unittests/cli/test_click.py enforces this.

    Pattern reference:
    https://click.palletsprojects.com/en/stable/complex/#lazily-loading-subcommands

    TODO: Python 3.15 introduces a native `lazy` import keyword (PEP 810). Once
    our minimum Python version reaches 3.15, the `add_lazy_command` mechanism
    here may become redundant — revisit and consider simplifying.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._lazy_commands: dict[str, tuple[str, str]] = {}
        # name -> experiment label
        self._experiment_gates: dict[str, str] = {}
        self._overrides: dict[str, click.Command] = {}

    def add_lazy_command(
        self,
        name: str,
        import_path: str,
        short_help: str,
        experiment: str | None = None,
    ) -> None:
        """Register a subcommand for lazy loading.

        If `experiment` is given, the command is only listed and invocable when
        that experiment resolves truthy; otherwise it behaves as if it were
        never registered (hidden from --help, "No such command" on invoke).

        The experiment gate is resolved per invocation in `list_commands`/`get_command`,
        not at this registration call: `main` is built once at import, so
        guarding registration would freeze visibility before a unit test could
        override the experiment.
        """
        self._lazy_commands[name] = (import_path, short_help)
        if experiment is not None:
            self._experiment_gates[name] = experiment

    def _is_visible(self, name: str) -> bool:
        """True if `name` is not hidden via experiment."""
        label = self._experiment_gates.get(name)
        if label is None:
            return True
        return bool(resolve_experiment(label))

    def list_commands(self, ctx: click.Context) -> list[str]:
        names = set(super().list_commands(ctx)) | set(self._lazy_commands) | set(self._overrides)
        return sorted(filter(self._is_visible, names))

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        # Overrides are consulted only after the visibility check, so an installed
        # extension cannot expose a command that is gated off.
        if not self._is_visible(cmd_name):
            return None
        if cmd_name in self._overrides:
            return self._overrides[cmd_name]
        if cmd_name in self._lazy_commands and cmd_name not in self.commands:
            import_path, _ = self._lazy_commands[cmd_name]
            module_path, attr = import_path.split(":")
            cmd = getattr(importlib.import_module(module_path), attr)
            patch_source_in_help(cmd)
            self.commands[cmd_name] = cmd
        return super().get_command(ctx, cmd_name)

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        rows: list[tuple[str, str]] = []
        for name in self.list_commands(ctx):
            if name in self._overrides:
                rows.append((name, self._overrides[name].get_short_help_str(limit=1000)))
                continue
            if name in self.commands:
                # Don't truncate — Click's default of 45 would cut our docstring summaries.
                rows.append((name, self.commands[name].get_short_help_str(limit=1000)))
                continue
            lazy = self._lazy_commands.get(name)
            if lazy is not None:
                rows.append((name, lazy[1]))
        if rows:
            with formatter.section("Commands"):
                formatter.write_dl(rows)


def _source_path(cmd: Any) -> str | None:
    """Resolve the absolute file path of a command's callback module."""
    cb = cmd.callback
    if cb is None:
        return None
    try:
        mod = importlib.import_module(cb.__module__)
        return inspect.getfile(mod)
    except Exception:
        return None


def patch_source_in_help(cmd: Any) -> None:
    """Recursively patch all commands to show source location in --help epilog.

    Idempotent: a command registered as lazy in two parents (e.g., the
    `create` alias) would otherwise get its `format_epilog` wrapped twice
    and render two `Source:` lines.
    """
    if getattr(cmd, "_source_patched", False):
        return

    original = cmd.format_epilog

    def _patched(ctx: click.Context, formatter: click.HelpFormatter) -> None:
        original(ctx, formatter)
        path = _source_path(cmd)
        if path:
            formatter.write("\n")
            formatter.write(f"Source: {path}\n")

    cmd.format_epilog = _patched
    cmd._source_patched = True

    if isinstance(cmd, click.Group):
        for sub in cmd.commands.values():
            patch_source_in_help(sub)
