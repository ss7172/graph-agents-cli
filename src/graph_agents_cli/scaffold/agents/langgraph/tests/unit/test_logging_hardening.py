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

"""What the logs never hold: query strings, outbound URLs, raw warnings, a run
context's credential."""

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

PROJECT = Path(__file__).resolve().parents[2]


@pytest.fixture
def json_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    quiet = {name: logging.getLogger(name).level for name in telemetry.QUIET_LOGGERS}
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    telemetry.setup_logging()
    yield
    logging.captureWarnings(False)
    root.handlers = handlers
    root.setLevel(level)
    for name, value in quiet.items():
        logging.getLogger(name).setLevel(value)


def _records(err: str) -> list[dict]:
    return [json.loads(line) for line in err.splitlines() if line.startswith("{")]


def test_access_lines_keep_the_path_and_drop_the_query(json_logging, capsys) -> None:
    access = logging.getLogger("uvicorn.access")
    access.info(
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:5000",
        "GET",
        "/threads?access_token=eyJ.canary-token-4411&limit=1",
        "1.1",
        401,
    )
    access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET", "/health", "1.1", 200)
    err = capsys.readouterr().err
    assert "canary-token-4411" not in err and "limit=1" not in err
    messages = [r["message"] for r in _records(err)]
    assert messages == [
        '127.0.0.1:5000 - "GET /threads HTTP/1.1" 401',
        '127.0.0.1:5000 - "GET /health HTTP/1.1" 200',
    ]


def test_http_client_url_lines_stay_out_of_info_logs(json_logging, capsys) -> None:
    logging.getLogger("httpx").info(
        'HTTP Request: GET http://orders.test/orders?customer=alice "HTTP/1.1 200 OK"'
    )
    logging.getLogger("httpcore.connection").info("connect_tcp.started host='orders.test'")
    logging.getLogger("httpx").warning("kept: a warning")
    err = capsys.readouterr().err
    assert "customer=alice" not in err and "connect_tcp" not in err
    assert [r["message"] for r in _records(err)] == ["kept: a warning"]


def test_setup_is_idempotent(json_logging) -> None:
    telemetry.setup_logging()
    access = logging.getLogger("uvicorn.access")
    assert sum(isinstance(f, telemetry.AccessLogFilter) for f in access.filters) == 1
    warns = logging.getLogger("py.warnings")
    assert sum(isinstance(f, telemetry.WarningRedactionFilter) for f in warns.filters) == 1


def test_the_value_a_serializer_warning_echoes_is_redacted() -> None:
    record = logging.LogRecord(
        "py.warnings",
        logging.WARNING,
        __file__,
        1,
        "%s",
        (
            "pydantic/main.py:475: UserWarning: Pydantic serializer warnings:\n"
            "  PydanticSerializationUnexpectedValue(Expected `none` - serialized value may "
            "not be as expected [field_name='context', input_value=AgentContext(principal_id"
            "...'secret-TAIL', input_type=AgentContext], input_type=AgentContext])\n"
            "  return self.__pydantic_serializer__.to_python(\n",
        ),
        None,
    )
    assert telemetry.WarningRedactionFilter().filter(record)
    message = record.getMessage()
    assert "secret-TAIL" not in message and "principal_id" not in message
    assert "input_value=<redacted>, input_type=AgentContext]" in message


# A tool with the unparameterised `ToolRuntime` makes pydantic warn with the run
# context's repr on every call. The run context holds the caller's forwarded
# credential; the process's stderr must stay JSON and must not hold it.
_TOOL_RUN = """
import asyncio
from dataclasses import dataclass, field
from typing import Any

from langchain.agents import create_agent
from langchain.tools import ToolRuntime
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from {{cookiecutter.agent_directory}}.app_utils import telemetry


@dataclass
class Context:
    principal_id: str = "anonymous"
    roles: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)


class Scripted(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@tool
async def lookup(item: str, runtime: ToolRuntime) -> str:
    \"\"\"Look up ITEM.\"\"\"
    return "found " + item


async def main() -> None:
    telemetry.setup_logging()
    model = Scripted(responses=[
        AIMessage(content="", tool_calls=[{"name": "lookup", "args": {"item": "x"}, "id": "c1"}]),
        AIMessage(content="done"),
    ])
    graph = create_agent(model=model, tools=[lookup], context_schema=Context)
    context = Context(
        principal_id="alice@example.com",
        roles=["user"],
        attributes={"credentials": {"orders": "FWD-SECRET-abcdefghijklmnop-TAILMARK"}},
    )
    async for _ in graph.astream(
        {"messages": [{"role": "user", "content": "go"}]},
        context=context,
        stream_mode=["updates"],
    ):
        pass


asyncio.run(main())
"""


def test_warnings_are_json_records_without_the_context_repr() -> None:
    env = dict(os.environ)
    env.update({"LOG_FORMAT": "json", "LOG_LEVEL": "INFO", "PYTHONWARNINGS": "default"})
    result = subprocess.run(
        [sys.executable, "-c", _TOOL_RUN],
        env=env,
        cwd=PROJECT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "TAILMARK" not in result.stderr and "alice@example.com" not in result.stderr
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    records = [json.loads(line) for line in lines]  # every line is one JSON record
    # The warning itself is kept (a JSON record); only the value it echoes goes.
    warned = [r["message"] for r in records if r["logger"] == "py.warnings"]
    assert warned and all("input_value=<redacted>" in m for m in warned if "input_value" in m)
