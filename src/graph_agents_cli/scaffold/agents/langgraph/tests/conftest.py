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

"""Shared test setup: the tests see neither `.env` nor the developer's app settings,
and the server tests bring their own tool.

`{{cookiecutter.agent_directory}}/agent.py` calls `load_dotenv()` when it is imported, which
would pull a developer's `.env` (a provider key, `AUTH_JWT_*`, ...) into the
test process: a test could then pass in CI and fail on a laptop, or the other
way round. This file is loaded before any test module imports the app, so it
switches `.env` loading off (in this process and in the subprocesses tests
start) and removes every app setting from the environment: each setting
`.env.example` documents, plus the provider, auth, tracing and LangChain
variables and the other settings the app reads (`A2A_NAME`, `RUNTIME`, ...).
Each test module then sets exactly what it needs. Opt-ins the tests read
themselves (`TEST_*`, such as `TEST_POSTGRES_DSN`) are kept.

The server tests exercise the plumbing (tool events, redaction, a run stopped
mid-call) with a test-only tool through the `use_test_tools` fixture, never
with the project's own tools, which are yours to replace or delete: while a
test module has imported the agent, each test serves a graph built like
`agent.py`'s but with no tools (the fake model would otherwise call a project
tool whose name a test prompt happens to mention). A test that needs the
project's own graph is marked `@pytest.mark.project_graph`.

`openai_compatible` is an OpenAI-compatible server in process (behind
`httpx.MockTransport`, no network) that refuses a chat history the way OpenAI
does: the tests that keep a thread valid for providers run the agent on
`ChatOpenAI` against it.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, ClassVar

import dotenv
import dotenv.main
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# Settings the app reads that a developer's shell may carry, beyond `.env.example`.
_SETTING_PREFIXES = (
    "AUTH_",
    "MODEL_",
    "JUDGE_",
    "LANGSMITH_",
    "LANGCHAIN_",
    "OTEL_",
    "TRACE_",
    "TRACING_",
)
_PROVIDER_VARIABLES = {"OPENAI_API_KEY", "OPENAI_BASE_URL", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"}
# Settings the app reads that `.env.example` leaves out (the runtime and the
# server set some of them; a developer's shell may carry any).
_APP_VARIABLES = {
    "A2A_NAME",
    "A2A_DESCRIPTION",
    "AGENT_VERSION",
    "DATABASE_URI",
    "REDIS_URI",
    "HOST",
    "LANGGRAPH_SERVER",
    "LANGGRAPH_SERVER_URL",
    "LANGSERVE_GRAPHS",
    "PGCONNECT_TIMEOUT",
    "RUNTIME",
}
_ASSIGNMENT = re.compile(r"^\s*#?\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=")


def _documented_settings() -> set[str]:
    """Every variable `.env.example` assigns, commented-out examples included."""
    try:
        text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    except OSError:
        return set()
    return {m.group(1) for line in text.splitlines() if (m := _ASSIGNMENT.match(line))}


def _is_app_setting(name: str) -> bool:
    if name.startswith("TEST_"):
        return False
    return (
        name in _PROVIDER_VARIABLES or name in _APP_VARIABLES or name.startswith(_SETTING_PREFIXES)
    )


def _no_dotenv(*args: Any, **kwargs: Any) -> bool:
    return False


# python-dotenv >= 1.2 honours PYTHON_DOTENV_DISABLED (inherited by subprocesses);
# replacing load_dotenv covers older versions in this process.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
dotenv.load_dotenv = _no_dotenv
dotenv.main.load_dotenv = _no_dotenv
for _name in _documented_settings() | {n for n in list(os.environ) if _is_app_setting(n)}:
    if not _name.startswith("TEST_") and _name != "PYTHON_DOTENV_DISABLED":
        os.environ.pop(_name, None)


def build_test_graph(tools: Sequence[Any]) -> Any:
    """The agent's graph as `agent.py` builds it, with `tools` instead of the project's."""
    from langchain.agents import create_agent

    from {{cookiecutter.agent_directory}} import agent
    from {{cookiecutter.agent_directory}}.app_utils.limits import recursion_limit
    from {{cookiecutter.agent_directory}}.app_utils.model import get_model

    return create_agent(
        model=get_model(),
        tools=list(tools),
        system_prompt=agent.SYSTEM_PROMPT,
        middleware=agent.middleware(),
        context_schema=agent.AgentContext,
        name="test-agent",
    ).with_config({"recursion_limit": recursion_limit()})


AGENT_MODULE = "{{cookiecutter.agent_directory}}.agent"
APP_MODULE = "{{cookiecutter.agent_directory}}.fast_api_app"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "project_graph: serve the project's own graph (its tools included)"
    )


@pytest.fixture(autouse=True)
def _no_project_tools(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Serve a graph without the project's tools (see the module docstring).

    Only where the test module already imported the agent or the app (with its
    settings); the app binds its checkpointer to whichever graph `agent.graph`
    is at startup, and reads `agent.graph` again for every run.
    """
    agent = sys.modules.get(AGENT_MODULE)
    if (agent is not None or APP_MODULE in sys.modules) and not os.environ.get("MODEL_PROVIDER"):
        # A module that imported the app without setting a model (a test of
        # its routes): the graph runs on the fake model.
        monkeypatch.setenv("MODEL_PROVIDER", "fake")
    if agent is None and APP_MODULE in sys.modules:
        import importlib

        agent = importlib.import_module(AGENT_MODULE)
    if agent is not None and request.node.get_closest_marker("project_graph") is None:
        graph = build_test_graph([])
        graph.checkpointer = agent.graph.checkpointer
        monkeypatch.setattr(agent, "graph", graph)
    yield


@pytest.fixture
def use_test_tools(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Any]:
    """Serve a graph with only the given tools for this test (call it once the app has started).

    The fake model calls a bound tool when the message mentions it (its name, or
    a distinctive word of it), so `probe` is called for "Run the probe for Paris"
    with `query="Paris"`. The graph keeps the checkpointer the app bound at startup.
    """

    def install(*tools: Any) -> Any:
        from {{cookiecutter.agent_directory}} import agent

        graph = build_test_graph(tools)
        graph.checkpointer = agent.graph.checkpointer
        monkeypatch.setattr(agent, "graph", graph)
        return graph

    return install


# --- an OpenAI-compatible server that checks the history, in process ----------------------


def openai_history_problem(messages: list[dict[str, Any]]) -> str | None:
    """Why OpenAI would refuse this chat history (a 400), or None.

    An assistant message with `tool_calls` must be followed by one tool message
    per call before anything else, and a tool message must answer such a call.
    """
    pending: list[str] = []
    for message in messages:
        role = message.get("role")
        if pending and role != "tool":
            return (
                "An assistant message with 'tool_calls' must be followed by tool messages "
                "responding to each 'tool_call_id'. The following tool_call_ids did not have "
                f"response messages: {', '.join(pending)}"
            )
        if role == "tool":
            if message.get("tool_call_id") not in pending:
                return "messages with role 'tool' must be a response to a preceding 'tool_calls'"
            pending.remove(message.get("tool_call_id"))
        elif role == "assistant":
            pending = [call.get("id") for call in message.get("tool_calls") or []]
    if pending:
        return f"tool_call_ids did not have response messages: {', '.join(pending)}"
    return None


class OpenAICompatibleFake:
    """A scripted OpenAI-compatible chat server behind `httpx.MockTransport`.

    It refuses a history OpenAI refuses (`openai_history_problem`, a 400), and
    answers the last message: a user message naming a script word gets that
    reply (see `SCRIPTS`: arguments that are not valid JSON, one id for two
    calls), a tool result gets `Found: <results>` (unless the user asked for
    `ALWAYSBAD`: invalid arguments again), anything else `ok`. Tool calls go
    to the first tool of the request. `requests` keeps every request body;
    `refusals` every 400.
    """

    SCRIPTS: ClassVar[dict[str, list[tuple[str, str]]]] = {
        # (call id, raw arguments) per tool call
        "BADARGS": [("call_0", "{'query': 'SF'}")],
        "TRAILINGCOMMA": [("call_0", '{"query": "SF",}')],
        "DUPIDS": [("dup", '{"query": "SF"}'), ("dup", '{"query": "Rome"}')],
        "BADANDGOOD": [("call_0", '{"query": "SF"}'), ("call_1", "query=Rome")],
    }

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.refusals: list[str] = []

    def model(self) -> Any:
        import httpx
        from langchain_openai import ChatOpenAI

        transport = httpx.MockTransport(self._handle)
        return ChatOpenAI(
            model="fake-gpt",
            api_key="test",
            base_url="http://openai-compatible.test/v1",
            max_retries=0,
            http_client=httpx.Client(transport=transport),
            http_async_client=httpx.AsyncClient(transport=transport),
        )

    def _reply(self, body: dict[str, Any]) -> tuple[str | None, list[dict[str, Any]]]:
        messages = body.get("messages") or []
        last = messages[-1] if messages else {}
        asked = next((str(m.get("content")) for m in reversed(messages) if m["role"] == "user"), "")
        tools = [t["function"]["name"] for t in body.get("tools") or []]
        if "ALWAYSBAD" in asked and tools:
            call_id = f"call_{sum(m['role'] == 'assistant' for m in messages)}"
            function = {"name": tools[0], "arguments": "{query: SF}"}
            return None, [{"index": 0, "id": call_id, "type": "function", "function": function}]
        if last.get("role") == "tool":
            results = []
            for message in reversed(messages):
                if message.get("role") != "tool":
                    break
                content = message.get("content")
                if isinstance(content, list):
                    content = "".join(str(b.get("text", "")) for b in content)
                results.append(str(content))
            return "Found: " + " | ".join(reversed(results)), []
        text = str(last.get("content") or "")
        for word, calls in self.SCRIPTS.items():
            if word in text and tools:
                return None, [
                    {
                        "index": i,
                        "id": call_id,
                        "type": "function",
                        "function": {"name": tools[0], "arguments": arguments},
                    }
                    for i, (call_id, arguments) in enumerate(calls)
                ]
        return "ok", []

    def _handle(self, request: Any) -> Any:
        import json

        import httpx

        body = json.loads(request.content)
        self.requests.append(body)
        problem = openai_history_problem(body.get("messages") or [])
        if problem:
            self.refusals.append(problem)
            error = {"message": problem, "type": "invalid_request_error", "param": "messages"}
            return httpx.Response(400, json={"error": error})
        text, calls = self._reply(body)
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        head = {"id": "chatcmpl-fake", "created": 0, "model": "fake-gpt"}
        if not body.get("stream"):
            message: dict[str, Any] = {"role": "assistant", "content": text}
            if calls:
                message["tool_calls"] = [
                    {k: v for k, v in c.items() if k != "index"} for c in calls
                ]
            choice = {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if calls else "stop",
            }
            return httpx.Response(
                200, json={**head, "object": "chat.completion", "choices": [choice], "usage": usage}
            )

        def chunk(delta: dict[str, Any], finish: str | None = None) -> str:
            choice = {"index": 0, "delta": delta, "finish_reason": finish}
            return "data: " + json.dumps(
                {**head, "object": "chat.completion.chunk", "choices": [choice]}
            )

        if calls:
            lines = [chunk({"role": "assistant", "content": None, "tool_calls": calls})]
            lines.append(chunk({}, "tool_calls"))
        else:
            lines = [chunk({"role": "assistant", "content": ""})]
            lines += [
                chunk({"content": (text or "")[i : i + 20]}) for i in range(0, len(text or ""), 20)
            ]
            lines.append(chunk({}, "stop"))
        final = {**head, "object": "chat.completion.chunk", "choices": [], "usage": usage}
        lines += ["data: " + json.dumps(final), "data: [DONE]"]
        return httpx.Response(
            200,
            content="\n\n".join(lines).encode() + b"\n\n",
            headers={"content-type": "text/event-stream"},
        )


@pytest.fixture
def openai_compatible() -> OpenAICompatibleFake:
    """An in-process OpenAI-compatible server that refuses invalid histories (see its class)."""
    return OpenAICompatibleFake()
