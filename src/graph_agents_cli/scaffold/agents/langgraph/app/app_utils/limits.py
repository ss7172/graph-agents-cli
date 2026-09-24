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

"""Run guardrails and request limits, read from the environment.

Every limit has a default in code, so an unset variable never means
"unlimited". A value that does not parse (or is out of range) stops the app
at startup (`check_settings()`, called by the lifespan) instead of silently
falling back, so a typo cannot quietly change a limit. The getters read the
environment on every call, which keeps them easy to override in tests.

| Variable                   | Default | Meaning                                              |
|----------------------------|---------|------------------------------------------------------|
| `RUN_TIMEOUT_S`            | 300     | wall-clock limit of one run; the run is cancelled    |
| `RECURSION_LIMIT`          | 50      | LangGraph super-steps per run (model + tool steps)   |
| `MAX_REQUEST_BYTES`        | 1048576 | request body cap (413 above it)                      |
| `MAX_METADATA_KEYS`        | 16      | keys in a `/chat` `metadata` object (422 above it)   |
| `MAX_METADATA_VALUE_CHARS` | 256     | characters per metadata key and string value (422)   |
| `SSE_HEARTBEAT_S`          | 15      | idle seconds before a `: keep-alive` SSE comment     |
| `RETENTION_DAYS`           | 0       | purge threads idle longer than this (0 = keep all)   |

A run of the template's agent needs two steps to answer at all and two more
for each tool call made after the previous one returned (the model step that
asks for it, then the tool step), so `RECURSION_LIMIT` = 50 leaves room for 24
sequential tool calls in one run (`sequential_tool_calls`). A run that
reaches it ends with a final message saying so (`message.end` status
`step_limit`) and keeps everything it did in the thread, so "continue" picks
up where it stopped; `chat.py` warns at startup when an API's
`limits.max_calls_per_run` cannot be reached within the limit.

Thread ids are 1-128 letters, digits or `_ . : -` (`THREAD_ID_PATTERN`).
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Callable, Mapping
from typing import Any

RUN_TIMEOUT_S = ("RUN_TIMEOUT_S", 300.0)
RECURSION_LIMIT = ("RECURSION_LIMIT", 50)
MAX_REQUEST_BYTES = ("MAX_REQUEST_BYTES", 1_048_576)
MAX_METADATA_KEYS = ("MAX_METADATA_KEYS", 16)
MAX_METADATA_VALUE_CHARS = ("MAX_METADATA_VALUE_CHARS", 256)
SSE_HEARTBEAT_S = ("SSE_HEARTBEAT_S", 15.0)
RETENTION_DAYS = ("RETENTION_DAYS", 0)

MAX_THREAD_ID_CHARS = 128
# Letters, digits and `_ . : -`: safe in URLs, logs and SQL parameters alike.
THREAD_ID_PATTERN = "^[A-Za-z0-9_.:-]{1," + str(MAX_THREAD_ID_CHARS) + "}$"
_THREAD_ID = re.compile(THREAD_ID_PATTERN)


class SettingsError(ValueError):
    """A limit variable is set to something that is not a valid value."""


def _raw(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def _int(spec: tuple[str, int], *, minimum: int) -> int:
    name, default = spec
    raw = _raw(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(f"{name}={raw!r} is not an integer.") from None
    if value < minimum:
        raise SettingsError(f"{name}={value} must be >= {minimum}.")
    return value


def _float(spec: tuple[str, float], *, minimum: float, exclusive: bool = True) -> float:
    name, default = spec
    raw = _raw(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SettingsError(f"{name}={raw!r} is not a number.") from None
    if not math.isfinite(value) or value < minimum or (exclusive and value == minimum):
        bound = ">" if exclusive else ">="
        raise SettingsError(f"{name}={raw} must be a finite number {bound} {minimum}.")
    return value


def run_timeout_s() -> float:
    """Seconds one run may take before it is cancelled and recorded as `timeout`."""
    return _float(RUN_TIMEOUT_S, minimum=0)


def recursion_limit() -> int:
    """LangGraph's per-run step limit (`GraphRecursionError` beyond it)."""
    return _int(RECURSION_LIMIT, minimum=1)


def sequential_tool_calls(steps: int) -> int:
    """How many tool calls, one after another, fit in `steps` graph steps with a final answer."""
    return max(0, (steps - 2) // 2)


def steps_for_tool_calls(calls: int) -> int:
    """The `RECURSION_LIMIT` that fits `calls` sequential tool calls and a final answer."""
    return 2 * calls + 2


def max_request_bytes() -> int:
    return _int(MAX_REQUEST_BYTES, minimum=1)


def max_metadata_keys() -> int:
    return _int(MAX_METADATA_KEYS, minimum=0)


def max_metadata_value_chars() -> int:
    return _int(MAX_METADATA_VALUE_CHARS, minimum=1)


def sse_heartbeat_s() -> float:
    return _float(SSE_HEARTBEAT_S, minimum=0)


def retention_days() -> int:
    """Days of inactivity after which a thread is purged; 0 keeps everything."""
    return _int(RETENTION_DAYS, minimum=0)


GETTERS: tuple[Callable[[], Any], ...] = (
    run_timeout_s,
    recursion_limit,
    max_request_bytes,
    max_metadata_keys,
    max_metadata_value_chars,
    sse_heartbeat_s,
    retention_days,
)


def check_settings(extra: tuple[Callable[[], Any], ...] = ()) -> None:
    """Parse every limit once; raise one `SettingsError` naming every bad variable."""
    problems: list[str] = []
    for getter in (*GETTERS, *extra):
        try:
            getter()
        except SettingsError as exc:
            problems.append(str(exc))
    if problems:
        raise SettingsError("Invalid settings: " + " ".join(problems))


def valid_thread_id(thread_id: str) -> bool:
    return isinstance(thread_id, str) and bool(_THREAD_ID.fullmatch(thread_id))


def check_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Validate `/chat` client metadata against the caps; return it as a plain dict.

    Metadata is a flat object of scalars (string, number, boolean or null),
    at most `MAX_METADATA_KEYS` keys, keys and string values at most
    `MAX_METADATA_VALUE_CHARS` characters. Anything else raises `ValueError`
    (a 422 for the caller) instead of being dropped silently.
    """
    max_keys = max_metadata_keys()
    max_chars = max_metadata_value_chars()
    if len(metadata) > max_keys:
        raise ValueError(f"metadata has {len(metadata)} keys; at most {max_keys} are allowed.")
    out: dict[str, Any] = {}
    for key, value in metadata.items():
        if not isinstance(key, str) or not key:
            raise ValueError("metadata keys must be non-empty strings.")
        if len(key) > max_chars:
            raise ValueError(f"metadata key is longer than {max_chars} characters.")
        if value is not None and not isinstance(value, str | int | float | bool):
            raise ValueError(
                f"metadata[{key[:40]!r}] must be a string, number, boolean or null "
                "(nested objects and lists are not accepted)."
            )
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"metadata[{key[:40]!r}] must be a finite number.")
        if isinstance(value, str) and len(value) > max_chars:
            raise ValueError(f"metadata[{key[:40]!r}] is longer than {max_chars} characters.")
        out[key] = value
    return out
