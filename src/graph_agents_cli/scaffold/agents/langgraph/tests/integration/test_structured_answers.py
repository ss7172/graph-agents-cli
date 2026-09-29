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

"""Structured final answers through the app, in-process (the fastapi runtime).

With a response schema (`RESPONSE_SCHEMA_PATH` here), a completed `/chat` run
ends with the answer in `message.end` (`structured_response`) after exactly
one `message.delta` holding its JSON text; the answer tool never shows as a
tool call; an answer that never fits ends the run with the error code
`invalid_structured_response` and leaves the thread usable. A run that pauses
for an approval gives its answer once resumed. Over A2A the reply's artifact
holds the JSON text and a data part with the answer, whether streamed or not,
and after an approval. Both strategies run on the fake model; every test runs
with the in-memory checkpointer, and on Postgres when `TEST_POSTGRES_DSN` is
set. `test_structured_server.py` runs the same under the langgraph-server
runtime.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
from a2a.client import ClientConfig, create_client
from a2a.types import GetTaskRequest, Message, Part, Role, SendMessageRequest, TaskState
from fastapi import HTTPException
from google.protobuf import json_format
from langchain.tools import ToolRuntime
from langchain_core.tools import tool
from starlette.requests import Request

from {{cookiecutter.agent_directory}}.app_utils import a2a as a2a_module
from {{cookiecutter.agent_directory}}.app_utils import auth as auth_module
from {{cookiecutter.agent_directory}}.app_utils.api_client import get_client, reset_policy_cache
from {{cookiecutter.agent_directory}}.app_utils.auth import ACTIONS, Principal
from {{cookiecutter.agent_directory}}.app_utils.structured import ANSWER_TOOL, validate
from {{cookiecutter.agent_directory}}.fast_api_app import app

A2A_PATH = "/a2a/{{cookiecutter.agent_directory}}"
A2A_URL = f"http://testserver{A2A_PATH}"
SCHEMA: dict[str, Any] = {
    "title": "Order answer",
    "type": "object",
    "properties": {
        # The fake answers with its text reply, which holds the request when no tool
        # ran: a request that says FAILME never fits.
        "answer": {"type": "string", "pattern": "^(?![\\s\\S]*FAILME)"},
        "done": {"type": "boolean"},
        "items": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["answer", "done"],
    "additionalProperties": False,
}
POLICY = """
apis:
  shop:
    base_url_env: SHOP_API_BASE_URL
    auth: none
    allowed_methods: [GET, POST]
    approval:
      required_for:
        operations:
          - operationId: cancelOrder
            path: /orders/{order_id}/cancel
            methods: [POST]
      approvers: [requester]
      timeout_s: 60
"""
SENT: list[httpx.Request] = []


def _upstream(request: httpx.Request) -> httpx.Response:
    SENT.append(request)
    return httpx.Response(200, json={"cancelled": request.url.path})


@tool
async def cancel_order(order_id: str, runtime: ToolRuntime[Any]) -> str:
    """Cancel an order by its id."""
    context = getattr(runtime, "context", None)
    client = get_client("shop", context=context, transport=httpx.MockTransport(_upstream))
    data = await client.post(
        "/orders/{order_id}/cancel",
        operation_id="cancelOrder",
        path_params={"order_id": order_id},
        json_body={"reason": "asked"},
    )
    return json.dumps(data)


@tool
def probe(query: str) -> str:
    """Run the probe for a place."""
    return f"{query}: 42"


class HeaderPolicy:
    """Test policy: `X-User` is the principal id."""

    async def authenticate(self, request: Request) -> Principal:
        user = request.headers.get("x-user")
        if not user:
            raise HTTPException(401, "no user", headers={"WWW-Authenticate": "Bearer"})
        return Principal(id=user, roles=["user"], permissions=set(ACTIONS))

    async def authorize(self, principal: Principal, action: str, resource: str | None) -> None:
        return None


def _as(user: str) -> dict[str, str]:
    return {"X-User": user}


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    event = None
    for line in text.splitlines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:") and event:
            events.append((event, json.loads(line[5:].strip())))
            event = None
    return events


ADMIN_DSN = os.environ.get("TEST_POSTGRES_DSN", "")


@pytest.fixture(params=["memory", "postgres"])
async def database(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[str]:
    if request.param == "memory":
        yield "memory"
        return
    if not ADMIN_DSN:
        pytest.skip("TEST_POSTGRES_DSN is not set")
    import psycopg

    name = f"gac_test_{uuid.uuid4().hex[:12]}"
    async with await psycopg.AsyncConnection.connect(ADMIN_DSN, autocommit=True) as admin:
        await admin.execute(f'CREATE DATABASE "{name}"')
    monkeypatch.setenv("CHECKPOINTER", "postgres")
    monkeypatch.setenv("POSTGRES_DSN", urlsplit(ADMIN_DSN)._replace(path=f"/{name}").geturl())
    yield "postgres"
    async with await psycopg.AsyncConnection.connect(ADMIN_DSN, autocommit=True) as admin:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(params=["tool", "provider"])
async def client(
    request: pytest.FixtureRequest,
    database: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_test_tools,
) -> AsyncIterator[httpx.AsyncClient]:
    schema = tmp_path / "response_schema.json"
    schema.write_text(json.dumps(SCHEMA), encoding="utf-8")
    monkeypatch.setenv("RESPONSE_SCHEMA_PATH", str(schema))
    monkeypatch.setenv("RESPONSE_FORMAT_STRATEGY", request.param)
    policy = tmp_path / "api-policy.yaml"
    policy.write_text(POLICY, encoding="utf-8")
    reset_policy_cache()
    monkeypatch.setenv("API_POLICY_PATH", str(policy))
    monkeypatch.setenv("SHOP_API_BASE_URL", "http://shop.test")
    monkeypatch.setattr(auth_module, "get_policy", lambda: HeaderPolicy())
    SENT.clear()
    async with app.router.lifespan_context(app):
        use_test_tools(cancel_order, probe)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", timeout=30
        ) as c:
            started = asyncio.get_running_loop().time()
            while (await c.get("/ready")).status_code != 200:  # postgres: the schema set up
                assert asyncio.get_running_loop().time() - started < 30, "not ready"
                await asyncio.sleep(0.1)
            yield c
    reset_policy_cache()


async def _chat(
    client: httpx.AsyncClient, message: str, thread_id: str | None = None
) -> list[tuple[str, dict[str, Any]]]:
    body: dict[str, Any] = {"message": message}
    if thread_id:
        body["thread_id"] = thread_id
    r = await client.post("/chat", json=body, headers=_as("alice"))
    assert r.status_code == 200, r.text
    return parse_sse(r.text)


def _answered(events: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """The run's answer, checking how it was delivered: one delta, its JSON text, then the end."""
    names = [e for e, _ in events]
    assert names[-2:] == ["message.delta", "message.end"], names
    assert names.count("message.delta") == 1, names
    end = events[-1][1]
    assert end["status"] == "ok", end
    answer = end["structured_response"]
    assert json.loads(events[-2][1]["text"]) == answer
    assert validate(SCHEMA, answer) == []
    # The answer tool is how the model answers, never a tool call a client sees.
    assert ANSWER_TOOL not in {d.get("name") for e, d in events if e.startswith("tool.")}
    return answer


async def test_a_completed_run_ends_with_the_answer(client: httpx.AsyncClient) -> None:
    events = await _chat(client, "Run the probe for Paris")
    names = [e for e, _ in events]
    assert names[:3] == ["message.start", "tool.call", "tool.result"], names
    answer = _answered(events)
    assert answer["answer"] == "Here is what I found: Paris: 42" and answer["done"] is False


async def test_every_turn_of_a_thread_has_its_own_answer(client: httpx.AsyncClient) -> None:
    first = await _chat(client, "hello")
    thread_id = first[0][1]["thread_id"]
    assert _answered(first)["answer"] == "Hello! How can I help you today?"
    second = await _chat(client, "Run the probe for Oslo", thread_id)
    assert _answered(second)["answer"] == "Here is what I found: Oslo: 42"
    r = await client.get(f"/threads/{thread_id}/messages", headers=_as("alice"))
    assert r.status_code == 200 and [m["role"] for m in r.json()].count("user") == 2


async def test_an_answer_that_never_fits_fails_the_run_and_the_thread_goes_on(
    client: httpx.AsyncClient,
) -> None:
    events = await _chat(client, "FAILME please")
    names = [e for e, _ in events]
    assert names == ["message.start", "error"], events
    error = events[-1][1]
    assert error["code"] == "invalid_structured_response", error
    assert "did not fit the response schema" in error["message"] and error["error_id"]
    # Nothing of the failed step was kept: the thread takes the next message.
    again = await _chat(client, "hello", error.get("thread_id") or events[0][1]["thread_id"])
    assert _answered(again)["answer"] == "Hello! How can I help you today?"


async def test_a_run_paused_for_approval_answers_once_resumed(client: httpx.AsyncClient) -> None:
    paused = await _chat(client, "Cancel the order for 7")
    names = [e for e, _ in paused]
    assert "message.delta" not in names, names  # no answer yet
    end = paused[-1][1]
    assert end["status"] == "awaiting_approval" and "structured_response" not in end
    approval = end["approval"]
    r = await client.post(
        f"/threads/{end['thread_id']}/approvals/{approval['approval_id']}",
        json={"decision": "approve"},
        headers=_as("alice"),
    )
    assert r.status_code == 200, r.text
    resumed = parse_sse(r.text)
    answer = _answered(resumed)
    assert "/orders/7/cancel" in answer["answer"] and len(SENT) == 1


def _serve_model(monkeypatch: pytest.MonkeyPatch, model: Any, tools: list[Any]) -> None:
    """Serve a graph on `model` with `tools`, built as agent.py builds it.

    The answer check is last in the middleware even when the project's agent.py
    predates it: this tests the runtime, not the project's wiring.
    """
    from langchain.agents import create_agent

    from {{cookiecutter.agent_directory}} import agent
    from {{cookiecutter.agent_directory}}.app_utils.structured import (
        StructuredAnswer,
        response_format,
    )

    middleware = agent.middleware()
    if not any(isinstance(m, StructuredAnswer) for m in middleware):
        middleware.append(StructuredAnswer())
    graph = create_agent(
        model=model,
        tools=tools,
        system_prompt=agent.SYSTEM_PROMPT,
        middleware=middleware,
        context_schema=agent.AgentContext,
        response_format=response_format(model, tools),
    )
    graph.checkpointer = agent.graph.checkpointer
    monkeypatch.setattr(agent, "graph", graph)


async def test_an_answer_given_beside_a_gated_call_waits_for_the_decision(
    client: httpx.AsyncClient, openai_compatible: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A model answers and calls a gated API in one message (parallel tool calls, on by
    # default at OpenAI): nothing is sent before the decision, and the resumed run answers.
    fake = openai_compatible
    _serve_model(monkeypatch, fake.model(), [cancel_order, probe])
    tool_strategy = os.environ["RESPONSE_FORMAT_STRATEGY"] == "tool"
    early = json.dumps({"answer": "I cancelled order 7", "done": True})
    cancel = ("call_cancel", "cancel_order", '{"order_id": "7"}')
    if tool_strategy:
        # The try goes back (its cancel did not run); the model then calls the tool alone.
        fake.queue = [
            (None, [("call_answer", ANSWER_TOOL, early), cancel]),
            (None, [("call_again", "cancel_order", '{"order_id": "7"}')]),
        ]
    else:
        # The provider strategy: a reply with tool calls is no answer, and its calls run.
        fake.queue = [(early, [cancel])]
    paused = await _chat(client, "Cancel the order for 7")
    end = paused[-1][1]
    assert end["status"] == "awaiting_approval" and "structured_response" not in end, paused
    assert [e for e, _ in paused].count("message.delta") == 0 and SENT == []
    done = {"answer": "Cancelled order 7", "done": True}
    fake.queue = [(None, [("call_done", ANSWER_TOOL, json.dumps(done))])]
    if not tool_strategy:
        fake.queue = [(json.dumps(done), [])]
    r = await client.post(
        f"/threads/{end['thread_id']}/approvals/{end['approval']['approval_id']}",
        json={"decision": "approve"},
        headers=_as("alice"),
    )
    assert r.status_code == 200, r.text
    assert _answered(parse_sse(r.text)) == done
    # The cancel was sent once, and every call the model made got a result.
    assert len(SENT) == 1 and fake.refusals == [] and fake.queue == []


async def test_an_agent_not_built_for_its_schema_ends_every_run_with_the_error(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A project that added the schema file but not `response_format=` in agent.py (one
    # created before structured answers, say): the runtime still expects an answer.
    from langchain.agents import create_agent

    from {{cookiecutter.agent_directory}} import agent
    from {{cookiecutter.agent_directory}}.app_utils.model import get_model

    graph = create_agent(
        model=get_model(),
        tools=[probe],
        system_prompt=agent.SYSTEM_PROMPT,
        middleware=agent.middleware(),
        context_schema=agent.AgentContext,
    )
    graph.checkpointer = agent.graph.checkpointer
    monkeypatch.setattr(agent, "graph", graph)
    events = await _chat(client, "Run the probe for Kyiv")
    # The tool ran and shows; the model's text is not the answer, so nothing else is sent.
    assert [e for e, _ in events] == ["message.start", "tool.call", "tool.result", "error"]
    error = events[-1][1]
    assert error["code"] == "invalid_structured_response", error
    assert "ended without an answer" in error["message"]


async def test_an_unchecked_answer_that_does_not_fit_is_never_delivered(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A project whose agent.py passes `response_format=` but has no `StructuredAnswer()`
    # in its middleware (half wired, an upgraded one say): nothing sends a bad answer
    # back to the model, and the runtime refuses to deliver it.
    from langchain.agents import create_agent

    from {{cookiecutter.agent_directory}} import agent
    from {{cookiecutter.agent_directory}}.app_utils.model import get_model
    from {{cookiecutter.agent_directory}}.app_utils.structured import (
        StructuredAnswer,
        response_format,
    )

    model = get_model()
    graph = create_agent(
        model=model,
        tools=[probe],
        system_prompt=agent.SYSTEM_PROMPT,
        middleware=[m for m in agent.middleware() if not isinstance(m, StructuredAnswer)],
        context_schema=agent.AgentContext,
        response_format=response_format(model, [probe]),
    )
    graph.checkpointer = agent.graph.checkpointer
    monkeypatch.setattr(agent, "graph", graph)
    events = await _chat(client, "FAILME please")
    assert [e for e, _ in events] == ["message.start", "error"], events
    error = events[-1][1]
    assert error["code"] == "invalid_structured_response", error
    assert "did not fit the response schema" in error["message"] and error["error_id"]
    # An answer that fits is delivered as ever.
    again = await _chat(client, "hello", events[0][1]["thread_id"])
    assert _answered(again)["answer"] == "Hello! How can I help you today?"


# --- A2A ------------------------------------------------------------------------------


async def _a2a(streaming: bool) -> tuple[httpx.AsyncClient, Any]:
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers=_as("alice"),
        timeout=30,
    )
    return http, await create_client(A2A_URL, ClientConfig(streaming=streaming, httpx_client=http))


def _answer_parts(parts: Any) -> tuple[str, dict[str, Any]]:
    """The reply's JSON text and its data part (the answer), checking the pair."""
    text, data = parts
    assert text.HasField("text") and data.HasField("data")
    assert data.media_type == "application/json"
    answer = json_format.MessageToDict(data.data)
    assert json.loads(text.text) == answer
    return text.text, answer


async def test_a2a_send_message_returns_the_answer_as_text_and_data(
    client: httpx.AsyncClient,
) -> None:
    http, alice = await _a2a(streaming=False)
    try:
        request = SendMessageRequest(
            message=Message(
                message_id=uuid.uuid4().hex, role=Role.ROLE_USER, parts=[Part(text="hello")]
            )
        )
        task = None
        async for chunk in alice.send_message(request):
            if chunk.HasField("task"):
                task = chunk.task
        assert task is not None and task.status.state == TaskState.TASK_STATE_COMPLETED
        (artifact,) = task.artifacts
        _, answer = _answer_parts(artifact.parts)
        assert answer == {"answer": "Hello! How can I help you today?", "done": False, "items": []}
    finally:
        await http.aclose()


async def test_a2a_streamed_reply_ends_with_the_answer_and_is_stored_whole(
    client: httpx.AsyncClient,
) -> None:
    http, alice = await _a2a(streaming=True)
    try:
        request = SendMessageRequest(
            message=Message(
                message_id=uuid.uuid4().hex,
                role=Role.ROLE_USER,
                parts=[Part(text="Run the probe for Lima")],
            )
        )
        updates = [
            chunk.artifact_update
            async for chunk in alice.send_message(request)
            if chunk.HasField("artifact_update")
        ]
        assert updates and updates[-1].last_chunk
        text, answer = _answer_parts(updates[-1].artifact.parts)
        assert answer["answer"] == "Here is what I found: Lima: 42"
        stored = await alice.get_task(GetTaskRequest(id=updates[0].task_id))
        (artifact,) = stored.artifacts
        assert _answer_parts(artifact.parts) == (text, answer)
    finally:
        await http.aclose()


async def test_a2a_task_paused_for_approval_completes_with_the_answer(
    client: httpx.AsyncClient,
) -> None:
    async def rpc(params: dict[str, Any]) -> dict[str, Any]:
        r = await client.post(
            A2A_PATH,
            json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": params},
            headers={**_as("alice"), "A2A-Version": "1.0"},
        )
        assert r.status_code == 200, r.text
        return r.json()["result"]["task"]

    message = {"messageId": uuid.uuid4().hex, "role": "ROLE_USER"}
    task = await rpc({"message": {**message, "parts": [{"text": "Cancel the order for 9"}]}})
    assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED", task
    [request] = [p["data"] for p in task["status"]["message"]["parts"] if "data" in p]
    [approval] = json.loads(request["approval_json"])
    decision = {"approval_id": approval["approval_id"], "decision": "approve"}
    done = await rpc(
        {
            "message": {
                "messageId": uuid.uuid4().hex,
                "role": "ROLE_USER",
                "taskId": task["id"],
                "contextId": task["contextId"],
                "parts": [{"data": decision}],
            }
        }
    )
    assert done["status"]["state"] == "TASK_STATE_COMPLETED", done
    # The paused run's reply (empty) and the resumed run's: the answer is the last one.
    text, data = done["artifacts"][-1]["parts"]
    assert data["mediaType"] == "application/json"
    assert json.loads(text["text"]) == data["data"]
    assert "/orders/9/cancel" in data["data"]["answer"] and len(SENT) == 1


async def test_a2a_task_decided_over_http_completes_with_the_whole_answer(
    client: httpx.AsyncClient, openai_compatible: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The person decides outside the task, over HTTP with their own credentials (what
    # `decide_with: direct` asks of an agent's request): the task that waited takes the
    # answer whole, as its last `response` artifact, like a decision sent on the task.
    fake = openai_compatible
    _serve_model(monkeypatch, fake.model(), [cancel_order, probe])
    tool_strategy = os.environ["RESPONSE_FORMAT_STRATEGY"] == "tool"

    async def rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
        r = await client.post(
            A2A_PATH,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            headers={**_as("alice"), "A2A-Version": "1.0"},
        )
        assert r.status_code == 200, r.text
        return r.json()["result"]

    fake.queue = [(None, [("call_cancel", "cancel_order", '{"order_id": "12"}')])]
    message = {"messageId": uuid.uuid4().hex, "role": "ROLE_USER"}
    sent = await rpc("SendMessage", {"message": {**message, "parts": [{"text": "Cancel 12"}]}})
    task = sent["task"]
    assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED", task
    [request] = [p["data"] for p in task["status"]["message"]["parts"] if "data" in p]
    [approval] = json.loads(request["approval_json"])
    # Longer than the 2,000 characters of a reply a followed task's status would quote.
    long = {"answer": "Cancelled order 12. " + "x" * 3000, "done": True, "items": ["12"]}
    fake.queue = [(None, [("call_done", ANSWER_TOOL, json.dumps(long))])]
    if not tool_strategy:
        fake.queue = [(json.dumps(long), [])]
    r = await client.post(
        f"/threads/{task['contextId']}/approvals/{approval['approval_id']}",
        json={"decision": "approve"},
        headers=_as("alice"),
    )
    assert r.status_code == 200, r.text
    assert _answered(parse_sse(r.text)) == long and len(SENT) == 1
    followed = await rpc("GetTask", {"id": task["id"]})
    assert followed["status"]["state"] == "TASK_STATE_COMPLETED", followed
    [note] = followed["status"]["message"]["parts"]
    assert note["text"] == (
        f"Approval {approval['approval_id']} was approved outside this task; "
        "the run continued there."
    )
    text, data = followed["artifacts"][-1]["parts"]
    assert followed["artifacts"][-1]["name"] == "response"
    assert data["mediaType"] == "application/json"
    assert json.loads(text["text"]) == data["data"] == long


async def test_the_card_lists_json_among_the_output_modes(client: httpx.AsyncClient) -> None:
    card = a2a_module.agent_card()
    assert list(card.default_output_modes) == ["text/plain", "application/json"]
