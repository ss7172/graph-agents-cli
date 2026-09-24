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

"""A thread whose tool-call history a stopped run left invalid is repaired by the next run.

Model providers reject an assistant tool call that is not followed right away
by its result. A process that dies mid tool call (a crash, an OOM kill, a lost
node, a database outage) never writes the result; older versions also wrote
the repair after the next user turn. Each run puts the history right before it
appends its own turn (in-process, fake model, memory checkpointer).

A model's tool call whose arguments are not valid JSON needs a result too
(LangChain sends it back to the provider as a call): the agent answers it in
the same run, and the repair answers one an older version left open. Those
tests run the agent on `ChatOpenAI` against an in-process OpenAI-compatible
server that refuses invalid histories as OpenAI does (`openai_compatible`).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
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
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from {{cookiecutter.agent_directory}} import agent as agent_module
from {{cookiecutter.agent_directory}}.app_utils.chat import repair_tool_history
from {{cookiecutter.agent_directory}}.app_utils.content import INVALID_TOOL_CALL_RESULT
from {{cookiecutter.agent_directory}}.fast_api_app import app

AUTH = {"Authorization": "Bearer test-key"}


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    for name in ("RUN_TIMEOUT_S", "RECURSION_LIMIT", "RETENTION_DAYS", "TRACE_CAPTURE"):
        monkeypatch.delenv(name, raising=False)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", timeout=30
        ) as c:
            yield c


async def chat(client: httpx.AsyncClient, message: str, thread_id: str) -> str:
    r = await client.post("/chat", json={"message": message, "thread_id": thread_id}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert "event: message.end" in r.text, r.text
    return r.text


async def history(thread_id: str) -> list[Any]:
    state = await agent_module.graph.aget_state({"configurable": {"thread_id": thread_id}})
    return list(state.values.get("messages", []))


def roles(messages: list[Any]) -> list[str]:
    out = []
    for m in messages:
        if isinstance(m, ToolMessage):
            out.append("tool!" if m.status == "error" else "tool")
        elif isinstance(m, AIMessage):
            out.append("ai(tc)" if m.tool_calls else "ai")
        else:
            out.append("user")
    return out


def open_call(call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        id=f"ai-{call_id}",
        tool_calls=[{"id": call_id, "name": "get_weather", "args": {"query": "Paris"}}],
    )


async def seed(thread_id: str, messages: list[Any], as_node: str = "model") -> None:
    """Write `messages` into the thread as a stopped run would have left them."""
    await agent_module.graph.aupdate_state(
        {"configurable": {"thread_id": thread_id}}, {"messages": messages}, as_node=as_node
    )


async def test_a_tool_call_left_open_by_a_dead_run_is_answered_before_the_next_turn(
    client: httpx.AsyncClient,
) -> None:
    thread_id = str(uuid.uuid4())
    await chat(client, "hello", thread_id)  # the thread and its owner exist
    # The process died after the model asked for the tool, before its result.
    await seed(thread_id, [HumanMessage(content="weather?", id="u2"), open_call("c1")])
    assert roles(await history(thread_id)) == ["user", "ai", "user", "ai(tc)"]
    await chat(client, "are you there?", thread_id)
    messages = await history(thread_id)
    assert roles(messages) == ["user", "ai", "user", "ai(tc)", "tool!", "user", "ai"]
    assert messages[4].tool_call_id == "c1" and "interrupted" in messages[4].content
    assert repair_tool_history(messages, lambda call: None) is None  # valid for providers


async def test_a_result_written_after_the_next_turn_is_moved_back(
    client: httpx.AsyncClient,
) -> None:
    """The shape older versions left: every later turn failed with a provider 400."""
    thread_id = str(uuid.uuid4())
    await chat(client, "hello", thread_id)
    await seed(
        thread_id,
        [
            HumanMessage(content="weather?", id="u2"),
            open_call("c1"),
            HumanMessage(content="hello?", id="u3"),
            ToolMessage(content="stopped", tool_call_id="c1", status="error", id="t1"),
        ],
        as_node="tools",
    )
    await chat(client, "still there?", thread_id)
    messages = await history(thread_id)
    assert roles(messages) == ["user", "ai", "user", "ai(tc)", "tool!", "user", "user", "ai"]
    assert [m.id for m in messages[2:6]] == ["u2", "ai-c1", "t1", "u3"]
    assert repair_tool_history(messages, lambda call: None) is None
    # Nothing more to repair on the turn after.
    before = [m.id for m in await history(thread_id)]
    await chat(client, "and again", thread_id)
    assert [m.id for m in await history(thread_id)][: len(before)] == before


async def test_turns_that_reuse_a_tool_call_id_keep_every_result(
    client: httpx.AsyncClient, use_test_tools, caplog: pytest.LogCaptureFixture
) -> None:
    """The fake model calls `call_probe` every turn (models may number ids per message):
    a healthy thread is never rewritten, and each turn keeps its own result."""
    from langchain_core.tools import tool

    @tool
    def probe(query: str) -> str:
        """Test-only tool: reports what it was asked about."""
        return f"probe reading for {query}"

    use_test_tools(probe)
    thread_id = str(uuid.uuid4())
    with caplog.at_level("INFO"):
        await chat(client, "Run the probe for SF", thread_id)
        await chat(client, "Run the probe for Paris", thread_id)
        await chat(client, "thanks", thread_id)
    messages = await history(thread_id)
    assert roles(messages) == ["user", "ai(tc)", "tool", "ai"] * 2 + ["user", "ai"]
    calls = [m.tool_calls[0]["id"] for m in messages if isinstance(m, AIMessage) and m.tool_calls]
    assert calls[0] == calls[1]  # the same id in both turns
    results = [m.content for m in messages if isinstance(m, ToolMessage)]
    assert results == ["probe reading for SF", "probe reading for Paris"]
    assert not [r for r in caplog.records if "tool results" in r.getMessage()]
    assert not [r for r in caplog.records if "tool calls" in r.getMessage()]


# --- a provider that refuses invalid histories -----------------------------------------------


def serve_openai_compatible(openai_compatible: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve the agent's graph on `ChatOpenAI` against the in-process OpenAI-compatible fake."""
    from langchain.agents import create_agent
    from langchain_core.tools import tool

    @tool
    def probe(query: str) -> str:
        """Test-only tool: reports what it was asked about."""
        return f"probe reading for {query}"

    graph = create_agent(
        model=openai_compatible.model(),
        tools=[probe],
        system_prompt=agent_module.SYSTEM_PROMPT,
        middleware=agent_module.middleware(),
        context_schema=agent_module.AgentContext,
    )
    graph.checkpointer = agent_module.graph.checkpointer
    monkeypatch.setattr(agent_module, "graph", graph)


async def events_of(client: httpx.AsyncClient, message: str, thread_id: str) -> list[str]:
    text = await chat(client, message, thread_id)
    return [line[7:] for line in text.splitlines() if line.startswith("event: ")]


@pytest.mark.parametrize("script", ["BADARGS", "TRAILINGCOMMA"])
async def test_a_tool_call_with_invalid_arguments_leaves_the_thread_usable(
    client: httpx.AsyncClient,
    openai_compatible: Any,
    monkeypatch: pytest.MonkeyPatch,
    script: str,
) -> None:
    """Arguments that are not valid JSON: the model is told and replies, every turn after works.

    Before, the run ended with no reply, and the provider refused every later
    turn (the call had no result).
    """
    serve_openai_compatible(openai_compatible, monkeypatch)
    thread_id = str(uuid.uuid4())
    text = await chat(client, f"probe {script}", thread_id)
    assert "event: tool.call" in text and "event: tool.result" in text
    assert "Found: " in text and '"status": "ok"' in text
    for message in ("thanks", "and again"):
        assert (await events_of(client, message, thread_id))[-1] == "message.end"
    assert openai_compatible.refusals == []
    messages = await history(thread_id)
    assert roles(messages)[:4] == ["user", "ai", "tool!", "ai"]
    assert messages[1].invalid_tool_calls and messages[2].content == INVALID_TOOL_CALL_RESULT
    assert repair_tool_history(messages, lambda call: None) is None


async def test_a_thread_an_invalid_call_broke_before_is_repaired_by_the_next_turn(
    client: httpx.AsyncClient, openai_compatible: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shape older versions left: an invalid call and no result, then more turns."""
    thread_id = str(uuid.uuid4())
    await chat(client, "hello", thread_id)
    broken = AIMessage(
        content="",
        id="ai-bad",
        invalid_tool_calls=[
            {"type": "invalid_tool_call", "id": "call_0", "name": "probe", "args": "query=SF"}
        ],
    )
    await seed(thread_id, [HumanMessage(content="probe?", id="u2"), broken])
    await seed(thread_id, [HumanMessage(content="hello?", id="u3")], as_node="tools")
    serve_openai_compatible(openai_compatible, monkeypatch)
    assert (await events_of(client, "still there?", thread_id))[-1] == "message.end"
    assert openai_compatible.refusals == []
    messages = await history(thread_id)
    assert roles(messages)[2:6] == ["user", "ai", "tool!", "user"]
    assert [messages[2].id, messages[3].id, messages[5].id] == ["u2", "ai-bad", "u3"]
    assert messages[4].tool_call_id == "call_0"
    assert messages[4].content == INVALID_TOOL_CALL_RESULT
    assert all(body.get("stream") for body in openai_compatible.requests)


async def test_one_id_for_two_calls_keeps_both_results_on_the_next_turn(
    client: httpx.AsyncClient, openai_compatible: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Some OpenAI-compatible servers give parallel calls one id: no result is dropped."""
    serve_openai_compatible(openai_compatible, monkeypatch)
    thread_id = str(uuid.uuid4())
    await chat(client, "probe DUPIDS", thread_id)
    before = [m.id for m in await history(thread_id)]
    assert (await events_of(client, "thanks", thread_id))[-1] == "message.end"
    assert openai_compatible.refusals == []
    messages = await history(thread_id)
    assert [m.id for m in messages[: len(before)]] == before  # nothing rewritten
    results = [m.content for m in messages if isinstance(m, ToolMessage)]
    assert sorted(results) == ["probe reading for Rome", "probe reading for SF"]
