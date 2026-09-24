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
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

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
