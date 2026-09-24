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
