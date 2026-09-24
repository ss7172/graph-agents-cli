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

"""Output helpers for agents CLI."""

import json
import os
import sys
from enum import IntEnum
from typing import Any

from rich.console import Console as _RichConsole


class ExitCode(IntEnum):
    OK = 0
    ERROR = 1


# The widest a table grows off a terminal (CI logs, a pipe, a file) when COLUMNS is
# unset: Rich would use 80 columns there and cut long cells with "…".
NON_TERMINAL_TABLE_WIDTH = 250


class Console(_RichConsole):
    """Rich Console configured with project-wide defaults (soft_wrap=True)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("soft_wrap", True)
        super().__init__(*args, **kwargs)
        self.explicit_width = kwargs.get("width") is not None


def print_table(console: _RichConsole, table: Any) -> None:
    """Print ``table`` whole: off a terminal it may grow past 80 columns to fit its cells.

    Only when the width was not chosen (no ``width``, no ``COLUMNS``); up to
    :data:`NON_TERMINAL_TABLE_WIDTH`, beyond which cells fold onto more lines.
    Rules and panels printed by the same console keep the normal width.
    """
    chosen = (
        getattr(console, "explicit_width", False) or (os.environ.get("COLUMNS") or "").isdigit()
    )
    if console.is_terminal or chosen:
        console.print(table)
        return
    from rich.measure import Measurement

    # Measured against the cap: the console's own options would clamp it to 80.
    options = console.options.update_width(NON_TERMINAL_TABLE_WIDTH)
    needed = Measurement.get(console, options, table).maximum
    if needed <= console.width:
        console.print(table)
        return
    original = console.width
    console.width = min(needed, NON_TERMINAL_TABLE_WIDTH)
    try:
        console.print(table)
    finally:
        console.width = original


def emit(data: dict) -> None:
    """Write structured data to stdout."""
    print(json.dumps(data), file=sys.stdout)


def emit_error(msg: str, code: int = ExitCode.ERROR) -> None:
    """Write error message to stderr and exit."""
    print(json.dumps({"error": msg}), file=sys.stderr)
    raise SystemExit(code)
