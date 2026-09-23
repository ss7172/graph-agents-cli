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

"""Output helpers for agents CLI."""

import json
import sys
from enum import IntEnum
from typing import Any

from rich.console import Console as _RichConsole


class ExitCode(IntEnum):
    OK = 0
    ERROR = 1


class Console(_RichConsole):
    """Rich Console configured with project-wide defaults (soft_wrap=True)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("soft_wrap", True)
        super().__init__(*args, **kwargs)


def emit(data: dict) -> None:
    """Write structured data to stdout."""
    print(json.dumps(data), file=sys.stdout)


def emit_error(msg: str, code: int = ExitCode.ERROR) -> None:
    """Write error message to stderr and exit."""
    print(json.dumps({"error": msg}), file=sys.stderr)
    raise SystemExit(code)
