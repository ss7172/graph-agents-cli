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

"""The optimizer's Claude Code sessions: a per-call log and a timeout that fits opus at high effort.

SkillOpt's Claude Code chat backend (``codex_harness._run_claude_code_cli_chat_exec``) runs one
``claude -p`` session per optimizer call with ``timeout or 300``, and no caller passes a timeout,
so every analyst, merge and ranking call is cut at 300 s and then retried up to five times. Its
token tracker counts only calls that returned. ``instrument()`` wraps that function: a missing
timeout becomes ``GAC_SKILLOPT_OPTIMIZER_TIMEOUT`` (default 1200 s), and every session, including
failed ones, is appended to ``<out>/optimizer_calls.jsonl`` with its stage, wall time, usage and
error.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT_S = 1200


def optimizer_timeout() -> int:
    value = os.environ.get("GAC_SKILLOPT_OPTIMIZER_TIMEOUT", "")
    return int(value) if value.strip() else DEFAULT_TIMEOUT_S


def logged_call(
    fn: Callable[..., tuple[str, dict]],
    log: Path,
    *,
    timeout_s: int,
    stage: Callable[[], str | None] = lambda: None,
) -> Callable[..., tuple[str, dict]]:
    """``fn`` (keyword arguments ``system``, ``prompt``, ``model``, ``timeout``, ...) with a
    default timeout and one JSON line per call in ``log``."""
    lock = threading.Lock()

    def call(**kwargs: Any) -> tuple[str, dict]:
        kwargs["timeout"] = kwargs.get("timeout") or timeout_s
        rec: dict[str, Any] = {
            "stage": stage(),
            "start": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "model": kwargs.get("model"),
            "effort": kwargs.get("effort"),
            "timeout_s": kwargs["timeout"],
            "schema": kwargs.get("schema") is not None,
            "in_chars": len(kwargs.get("system") or "") + len(kwargs.get("prompt") or ""),
        }
        t0 = time.time()
        try:
            text, usage = fn(**kwargs)
            rec.update(ok=True, out_chars=len(text or ""), usage=usage)
            return text, usage
        except BaseException as exc:
            rec.update(ok=False, error=f"{type(exc).__name__}: {str(exc)[:300]}")
            raise
        finally:
            rec["wall_s"] = round(time.time() - t0, 1)
            with lock, log.open("a") as fh:
                fh.write(json.dumps(rec) + "\n")

    return call


def instrument(out: Path) -> Path:
    """Patch SkillOpt's Claude Code chat path for this process; return the log path."""
    from skillopt.model import claude_code_backend, codex_harness

    log = out / "optimizer_calls.jsonl"
    local = threading.local()
    impl = claude_code_backend._chat_messages_impl

    def staged(model, messages, max_completion_tokens, retries, stage, **kw):
        local.stage = stage
        try:
            return impl(model, messages, max_completion_tokens, retries, stage, **kw)
        finally:
            local.stage = None

    claude_code_backend._chat_messages_impl = staged
    codex_harness._run_claude_code_cli_chat_exec = logged_call(
        codex_harness._run_claude_code_cli_chat_exec,
        log,
        timeout_s=optimizer_timeout(),
        stage=lambda: getattr(local, "stage", None),
    )
    return log
