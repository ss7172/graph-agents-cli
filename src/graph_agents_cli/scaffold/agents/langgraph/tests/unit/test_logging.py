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

"""Structured logging: JSON records with correlation ids, levels, formats."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from {{cookiecutter.agent_directory}}.app_utils import telemetry
from {{cookiecutter.agent_directory}}.app_utils.limits import SettingsError


@pytest.fixture
def root_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    for name in ("LOG_LEVEL", "LOG_FORMAT", "APP_ENV"):
        monkeypatch.delenv(name, raising=False)
    yield
    root.handlers = handlers
    root.setLevel(level)
    telemetry.bind_log_context(request_id=None, run_id=None, thread_id=None, principal_hash=None)


def _records(err: str) -> list[dict]:
    return [json.loads(line) for line in err.splitlines() if line.startswith("{")]


def test_json_records_carry_the_correlation_ids(root_logging, capsys) -> None:
    telemetry.setup_logging()
    telemetry.setup_logging()  # idempotent: still one handler
    telemetry.bind_log_context(request_id="req-1", run_id="run-1", thread_id="t-1")
    log = logging.getLogger("app.test")
    log.info("run finished", extra={"status": "ok", "latency_ms": 12})
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        log.exception("failed (error_id=%s)", "abc")
    records = _records(capsys.readouterr().err)
    assert len(records) == 2
    first, second = records
    assert first["level"] == "INFO" and first["logger"] == "app.test"
    assert first["message"] == "run finished" and first["status"] == "ok"
    assert first["latency_ms"] == 12
    assert (first["request_id"], first["run_id"], first["thread_id"]) == ("req-1", "run-1", "t-1")
    assert "principal_hash" not in first  # unset ids are left out, not "None"
    assert second["exc_type"] == "RuntimeError" and "boom" in second["exception"]


def test_level_and_text_format(root_logging, monkeypatch, capsys) -> None:
    monkeypatch.setenv("LOG_LEVEL", "warning")
    monkeypatch.setenv("LOG_FORMAT", "text")
    telemetry.setup_logging()
    telemetry.bind_log_context(request_id="req-2")
    logging.getLogger("app.test").info("hidden")
    logging.getLogger("app.test").warning("shown")
    err = capsys.readouterr().err
    assert "hidden" not in err and "WARNING app.test [request_id=req-2]: shown" in err


def test_dev_defaults_to_text_and_other_envs_to_json(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    assert telemetry.log_format() == "text"
    monkeypatch.setenv("APP_ENV", "prod")
    assert telemetry.log_format() == "json"


def test_lines_logged_before_the_lifespan_follow_log_format_too() -> None:
    """Import-time warnings and uvicorn's startup lines are JSON in JSON mode.

    Run as uvicorn does: its logging config first, then the app import, then
    its startup lines (before the app's lifespan runs).
    """
    code = (
        "import logging, logging.config\n"
        "import uvicorn.config\n"
        "logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)\n"
        "import {{cookiecutter.agent_directory}}.fast_api_app\n"
        "logging.getLogger('uvicorn.error').info('Started server process [1]')\n"
    )
    env = dict(os.environ)
    env.update(
        {
            # Set (not removed) so a project .env cannot fill them in.
            "APP_URL": "",
            "LOG_FORMAT": "json",
            "LOG_LEVEL": "INFO",
            "APP_ENV": "prod",
            "RUNTIME": "fastapi",
            "AUTH_POLICY": "shared-bearer",
            "MODEL_PROVIDER": "fake",
            "MODEL_NAME": "fake",
            "CHECKPOINTER": "memory",
            "TRACING_ENABLED": "false",
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    for needle in ("APP_URL is not set", "Started server process [1]"):
        matching = [line for line in lines if needle in line]
        assert matching, result.stderr
        assert all(json.loads(line)["message"] for line in matching), matching


@pytest.mark.parametrize(("name", "value"), [("LOG_LEVEL", "LOUD"), ("LOG_FORMAT", "xml")])
def test_bad_logging_settings_are_refused(monkeypatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(SettingsError):
        telemetry.log_level() if name == "LOG_LEVEL" else telemetry.log_format()
