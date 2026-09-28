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

"""The model settings against an OpenAI API over HTTP: Chat Completions and the Responses API.

The agent runs on `MODEL_PROVIDER=openai-compatible` with `OPENAI_BASE_URL`
naming a fake OpenAI server (`fake_openai.py`, loopback), through `/chat`, with
a test tool: the model calls it, reads its result and answers.

* `MODEL_USE_RESPONSES_API=true`: every request goes to `/v1/responses`, the
  reasoning effort as `reasoning.effort`, the tool as a function tool, its
  result as a `function_call_output`; the run records the usage the Responses
  API reports.
* `false` (and unset, for a model langchain-openai does not know to need the
  Responses API): `/v1/chat/completions`, the effort as `reasoning_effort`.
* A model that refuses function tools with a reasoning effort on Chat
  Completions (Track C's F15) fails there, naming `/v1/responses`, and works
  with the switch on or with `MODEL_REASONING_EFFORT=none`.
* The judge takes its own settings, or the agent's.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

os.environ.update(
    {
        "MODEL_PROVIDER": "fake",
        "MODEL_NAME": "fake",
        "CHECKPOINTER": "memory",
        "AUTH_POLICY": "shared-bearer",
        "API_KEY": "test-key",
        "APP_ENV": "dev",
        "TRACING_ENABLED": "false",
        "RUNTIME": "fastapi",
        "APP_URL": "http://testserver",
    }
)

import httpx
import pytest
from fake_openai import RESPONSES_USAGE, FakeOpenAI
from langchain_core.tools import tool

from {{cookiecutter.agent_directory}}.app_utils.model import get_judge_model, get_model
from {{cookiecutter.agent_directory}}.fast_api_app import app

AUTH = {"Authorization": "Bearer test-key"}
MODEL_ENV = ("MODEL_REASONING_EFFORT", "MODEL_USE_RESPONSES_API")
JUDGE_ENV = ("JUDGE_MODEL_PROVIDER", "JUDGE_MODEL_NAME", "JUDGE_BASE_URL", "JUDGE_API_KEY")


@tool
def probe(query: str) -> str:
    """Look up a city's probe reading."""
    return f"probe reading for {query}: 42"


@pytest.fixture(scope="module")
def server() -> Iterator[FakeOpenAI]:
    fake = FakeOpenAI()
    try:
        yield fake
    finally:
        fake.close()


@pytest.fixture
def openai_env(server: FakeOpenAI, monkeypatch: pytest.MonkeyPatch) -> FakeOpenAI:
    """The agent's model is an OpenAI-API model at the fake server."""
    monkeypatch.setenv("MODEL_PROVIDER", "openai-compatible")
    monkeypatch.setenv("MODEL_NAME", "gpt-test-terra")
    monkeypatch.setenv("OPENAI_BASE_URL", server.base_url())
    monkeypatch.setenv("MODEL_API_KEY", "test-model-key")
    monkeypatch.setenv("MODEL_MAX_RETRIES", "0")
    for name in (*MODEL_ENV, *JUDGE_ENV, "JUDGE_REASONING_EFFORT", "JUDGE_USE_RESPONSES_API"):
        monkeypatch.delenv(name, raising=False)
    server.requests.clear()
    server.effort_refuses_tools = False
    return server


@pytest.fixture
async def client(openai_env: FakeOpenAI) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", timeout=30
        ) as c:
            yield c


def _events(text: str) -> list[tuple[str, dict[str, Any]]]:
    events, event = [], None
    for line in text.splitlines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:") and event:
            events.append((event, json.loads(line[5:].strip())))
            event = None
    return events


async def _chat(client: httpx.AsyncClient, message: str) -> list[tuple[str, dict[str, Any]]]:
    r = await client.post(
        "/chat", json={"message": message, "thread_id": f"t-{uuid.uuid4().hex[:8]}"}, headers=AUTH
    )
    assert r.status_code == 200, r.text
    return _events(r.text)


def _reply(events: list[tuple[str, dict[str, Any]]]) -> str:
    return "".join(data.get("text", "") for name, data in events if name == "message.delta")


def _tool_round_trip(events: list[tuple[str, dict[str, Any]]]) -> None:
    """The model called the tool with the city, read its result and answered with it."""
    [call] = [data for name, data in events if name == "tool.call"]
    assert call["name"] == "probe" and call["args"] == {"query": "Paris"}, call
    [result] = [data for name, data in events if name == "tool.result"]
    assert result["is_error"] is False and "probe reading for Paris: 42" in result["result"]
    assert "Found:" in _reply(events) and "probe reading for Paris: 42" in _reply(events)
    assert events[-1][0] == "message.end" and events[-1][1]["status"] == "ok", events[-1]


async def test_the_responses_api_with_a_tool_and_a_reasoning_effort(
    client: httpx.AsyncClient,
    openai_env: FakeOpenAI,
    monkeypatch: pytest.MonkeyPatch,
    use_test_tools: Any,
) -> None:
    server = openai_env
    server.effort_refuses_tools = True  # a model that needs the Responses API for tools (F15)
    monkeypatch.setenv("MODEL_USE_RESPONSES_API", "true")
    monkeypatch.setenv("MODEL_REASONING_EFFORT", "high")
    use_test_tools(probe)
    events = await _chat(client, "Run the probe for Paris")
    _tool_round_trip(events)
    assert server.paths() == ["/v1/responses", "/v1/responses"]
    first, second = (r["body"] for r in server.requests)
    for body in (first, second):
        assert body["reasoning"] == {"effort": "high"} and body["stream"] is True
        assert "reasoning_effort" not in body and "stream_options" not in body
        assert body["model"] == "gpt-test-terra"
    [function] = first["tools"]
    assert (function["type"], function["name"]) == ("function", "probe")
    outputs = [i for i in second["input"] if i.get("type") == "function_call_output"]
    assert len(outputs) == 1 and "probe reading for Paris: 42" in outputs[0]["output"]
    # The run records the usage the Responses API reports, for both model calls.
    assert events[-1][1]["usage"] == {
        "input_tokens": 2 * RESPONSES_USAGE["input_tokens"],
        "output_tokens": 2 * RESPONSES_USAGE["output_tokens"],
    }


@pytest.mark.parametrize("switch", ["false", None])
async def test_chat_completions_with_a_tool_and_a_reasoning_effort(
    client: httpx.AsyncClient,
    openai_env: FakeOpenAI,
    monkeypatch: pytest.MonkeyPatch,
    use_test_tools: Any,
    switch: str | None,
) -> None:
    """`false`, or unset for a model langchain-openai does not know to need the Responses API."""
    server = openai_env
    if switch is not None:
        monkeypatch.setenv("MODEL_USE_RESPONSES_API", switch)
    monkeypatch.setenv("MODEL_REASONING_EFFORT", "low")
    use_test_tools(probe)
    events = await _chat(client, "Run the probe for Paris")
    _tool_round_trip(events)
    assert server.paths() == ["/v1/chat/completions", "/v1/chat/completions"]
    first, second = (r["body"] for r in server.requests)
    for body in (first, second):
        assert body["reasoning_effort"] == "low" and "reasoning" not in body
    assert [t["function"]["name"] for t in first["tools"]] == ["probe"]
    assert second["messages"][-1]["role"] == "tool"


async def test_a_model_that_refuses_tools_with_an_effort_on_chat_completions(
    client: httpx.AsyncClient,
    openai_env: FakeOpenAI,
    monkeypatch: pytest.MonkeyPatch,
    use_test_tools: Any,
) -> None:
    """Track C's F15: with Chat Completions the first call fails, naming the Responses API;
    `MODEL_REASONING_EFFORT=none` (or the switch, above) makes it work."""
    server = openai_env
    server.effort_refuses_tools = True
    use_test_tools(probe)
    failed = await _chat(client, "Run the probe for Paris")
    [error] = [data for name, data in failed if name == "error"]
    assert "/v1/responses" in json.dumps(error), error
    assert server.paths() == ["/v1/chat/completions"]
    server.requests.clear()
    monkeypatch.setenv("MODEL_REASONING_EFFORT", "none")
    use_test_tools(probe)  # the graph is built with the model the settings name now
    _tool_round_trip(await _chat(client, "Run the probe for Paris"))
    assert server.paths() == ["/v1/chat/completions", "/v1/chat/completions"]
    assert all(r["body"]["reasoning_effort"] == "none" for r in server.requests)


async def test_the_judge_takes_its_own_settings_or_the_agents(
    openai_env: FakeOpenAI, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = openai_env
    monkeypatch.setenv("MODEL_USE_RESPONSES_API", "true")
    monkeypatch.setenv("MODEL_REASONING_EFFORT", "medium")
    judge = get_judge_model()  # the agent's model and settings
    reply = await judge.ainvoke('Give a score in JSON: {"score": 5}')
    assert "ok" in str(reply.content)
    assert server.paths() == ["/v1/responses"]
    assert server.requests[-1]["body"]["reasoning"] == {"effort": "medium"}
    monkeypatch.setenv("JUDGE_USE_RESPONSES_API", "false")
    monkeypatch.setenv("JUDGE_REASONING_EFFORT", "minimal")
    await get_judge_model().ainvoke("score this")
    assert server.paths()[-1] == "/v1/chat/completions"
    assert server.requests[-1]["body"]["reasoning_effort"] == "minimal"
    # The agent keeps its own.
    await get_model().ainvoke("hello")
    assert server.paths()[-1] == "/v1/responses"
