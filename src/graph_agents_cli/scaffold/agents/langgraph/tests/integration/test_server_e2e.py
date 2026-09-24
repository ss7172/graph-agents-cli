# Copyright 2026 Google LLC
# Modifications Copyright 2026 graph-agents-cli contributors
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

"""End-to-end test of the server this project runs, in-process.

The app is started through its lifespan with `MODEL_PROVIDER=fake` and
`CHECKPOINTER=memory`, so no model key, database or network is needed, and
driven through `httpx.AsyncClient` over `ASGITransport`.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

# The environment must be in place before the app (and the graph) is imported.
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

from {{cookiecutter.agent_directory}} import agent as agent_module
from {{cookiecutter.agent_directory}}.app_utils.auth import Principal, reset_policy_cache
from {{cookiecutter.agent_directory}}.app_utils.chat import RUNTIME
from {{cookiecutter.agent_directory}}.fast_api_app import app

AUTH = {"Authorization": "Bearer test-key"}
A2A_PATH = "/a2a/{{cookiecutter.agent_directory}}"


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", timeout=30
        ) as c:
            yield c


async def _chat(
    client: httpx.AsyncClient, message: str, thread_id: str | None = None
) -> list[tuple[str, dict]]:
    body: dict = {"message": message}
    if thread_id:
        body["thread_id"] = thread_id
    events: list[tuple[str, dict]] = []
    async with client.stream(
        "POST", "/chat", json=body, headers={**AUTH, "Accept": "text/event-stream"}
    ) as r:
        assert r.status_code == 200, await r.aread()
        assert r.headers["content-type"].startswith("text/event-stream")
        event = None
        async for line in r.aiter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:") and event:
                import json

                events.append((event, json.loads(line[5:].strip())))
                event = None
    return events


async def test_health(client: httpx.AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "runtime": "fastapi", "checkpointer": "memory"}


async def test_chat_requires_the_bearer_key(client: httpx.AsyncClient) -> None:
    r = await client.post("/chat", json={"message": "hi"})
    assert r.status_code == 401
    r = await client.post("/chat", json={"message": "hi"}, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    r = await client.get("/threads/x/messages")
    assert r.status_code == 401


async def test_chat_sse_event_sequence_with_a_tool_call(client: httpx.AsyncClient) -> None:
    events = await _chat(client, "What's the weather in San Francisco?")
    names = [e for e, _ in events]
    assert names[0] == "message.start"
    assert names[-1] == "message.end"
    assert "tool.call" in names and "tool.result" in names
    assert names.index("tool.call") < names.index("tool.result") < names.index("message.delta")
    start, end = events[0][1], events[-1][1]
    assert start["thread_id"] and start["run_id"] == end["run_id"]
    call = next(d for e, d in events if e == "tool.call")
    assert call["name"] == "get_weather" and call["args"] == {"query": "San Francisco"}
    result = next(d for e, d in events if e == "tool.result")
    assert (
        result["id"] == call["id"] and result["is_error"] is False and "foggy" in result["result"]
    )
    text = "".join(d["text"] for e, d in events if e == "message.delta")
    assert "foggy" in text
    assert end["status"] == "ok" and end["usage"]["output_tokens"] > 0 and end["latency_ms"] >= 0


async def test_thread_continuity_and_messages_endpoint(client: httpx.AsyncClient) -> None:
    thread_id = str(uuid.uuid4())
    first = await _chat(client, "hello", thread_id)
    assert first[0][1]["thread_id"] == thread_id
    second = await _chat(client, "thanks", thread_id)
    assert second[0][1]["thread_id"] == thread_id
    r = await client.get(f"/threads/{thread_id}/messages", headers=AUTH)
    assert r.status_code == 200
    messages = r.json()
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[0]["content"] == "hello" and "Hello" in messages[1]["content"]
    r = await client.get(f"/threads/{uuid.uuid4()}/messages", headers=AUTH)
    assert r.status_code == 404


async def test_playground_only_in_dev(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = await client.get("/playground")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"] and "/chat" in r.text
    for other in ("prod", "DEV", " dev "):  # dev relaxes checks: only the exact value counts
        monkeypatch.setenv("APP_ENV", other)
        r = await client.get("/playground")
        assert r.status_code == 404, other


async def test_openapi_and_docs_exist_only_in_dev(client: httpx.AsyncClient) -> None:
    # This app was built under APP_ENV=dev: the schema and Swagger UI are on.
    assert (await client.get("/openapi.json")).status_code == 200
    assert (await client.get("/docs")).status_code == 200
    assert (await client.get("/redoc")).status_code == 404
    # Built with APP_ENV unset (the deployed default), they are gone.
    import importlib
    import os

    from {{cookiecutter.agent_directory}} import fast_api_app as module

    saved = os.environ.pop("APP_ENV", None)
    try:
        prod_app = importlib.reload(module).app
    finally:
        if saved is not None:
            os.environ["APP_ENV"] = saved
        importlib.reload(module)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=prod_app), base_url="http://testserver"
    ) as prod:
        assert (await prod.get("/openapi.json")).status_code == 404
        assert (await prod.get("/docs")).status_code == 404
    assert prod_app.openapi()["paths"]  # LangGraph Server still builds its spec from it


async def test_chat_error_event_replaces_message_end_and_records_the_run(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_astream(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        raise RuntimeError("model down")
        yield  # pragma: no cover - makes this an async generator

    # `_local_events` imports `graph` at call time, so patching the instance is enough.
    monkeypatch.setattr(agent_module.graph, "astream", failing_astream)
    events = await _chat(client, "hello")
    names = [e for e, _ in events]
    assert names == ["message.start", "error"], names
    error = events[1][1]
    run_id = events[0][1]["run_id"]
    # A generic message and an id naming the logged detail; the detail itself
    # only under APP_ENV=dev (this app runs in dev).
    assert error["code"] == "run_failed" and error["run_id"] == run_id
    assert error["message"] == f"The run failed. Reference: {error['error_id']}."
    assert error["detail"] == "RuntimeError: model down"
    monkeypatch.setenv("APP_ENV", "prod")
    error = (await _chat(client, "hello"))[1][1]
    assert "detail" not in error and "model down" not in str(error)
    assert RUNTIME.runs is not None
    record = await RUNTIME.runs.get(run_id)
    assert record is not None
    assert record.status == "error" and record.error_type == "RuntimeError"


@pytest.mark.parametrize(
    ("policy", "marker"),
    [("custom", "AUTH_POLICY=custom"), ("jwt", "AUTH_POLICY=jwt"), ("product-session", "custom")],
)
async def test_stub_policies_answer_503_on_every_surface(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, policy: str, marker: str
) -> None:
    """`custom` (and its retired name) and the `jwt` placeholder fail closed everywhere."""
    monkeypatch.setenv("AUTH_POLICY", policy)
    reset_policy_cache()
    try:
        assert (await client.get("/health")).status_code == 200
        for method, path, kwargs in (
            ("POST", "/chat", {"json": {"message": "hi"}, "headers": AUTH}),
            ("POST", "/chat", {"json": {"message": "hi"}, "headers": {"Cookie": "sid=1"}}),
            ("GET", "/threads/x/messages", {"headers": AUTH}),
            ("GET", f"{A2A_PATH}/.well-known/agent-card.json", {"headers": AUTH}),
            ("POST", A2A_PATH, {"json": {"jsonrpc": "2.0", "id": "1", "method": "SendMessage"}}),
        ):
            r = await client.request(method, path, **kwargs)
            assert r.status_code == 503, (method, path, r.text)
            assert marker in r.json()["detail"]
    finally:
        reset_policy_cache()


async def test_thread_ownership_and_tool_args_redaction(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-owners are refused; a read-across role reads without tool args under metadata capture."""
    thread_id = str(uuid.uuid4())
    await _chat(client, "What's the weather in San Francisco?", thread_id)
    owner = Principal(id="shared", roles=["shared"])
    messages = await RUNTIME.messages(owner, thread_id)
    assistant = next(m for m in messages if m.get("tool_calls"))
    assert assistant["tool_calls"][0]["args"] == {"query": "San Francisco"}

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await RUNTIME.messages(Principal(id="stranger"), thread_id)
    assert exc.value.status_code == 403
    assert RUNTIME.threads is not None
    with pytest.raises(HTTPException) as exc:
        await RUNTIME.threads.ensure(thread_id, Principal(id="stranger"))
    assert exc.value.status_code == 403

    monkeypatch.setenv("AUTH_READ_ACROSS_ROLES", "auditor")
    auditor = Principal(id="auditor", roles=["auditor"])
    messages = await RUNTIME.messages(auditor, thread_id)
    assistant = next(m for m in messages if m.get("tool_calls"))
    assert assistant["tool_calls"][0]["name"] == "get_weather"
    assert "args" not in assistant["tool_calls"][0]
    monkeypatch.setenv("TRACE_CAPTURE", "full")
    messages = await RUNTIME.messages(auditor, thread_id)
    assert next(m for m in messages if m.get("tool_calls"))["tool_calls"][0]["args"] == {
        "query": "San Francisco"
    }
    # Reading across is not writing: the auditor cannot continue the thread.
    with pytest.raises(HTTPException) as exc:
        await RUNTIME.threads.ensure(thread_id, auditor)
    assert exc.value.status_code == 403


async def test_a2a_card_and_message(client: httpx.AsyncClient) -> None:
    r = await client.get(f"{A2A_PATH}/.well-known/agent-card.json")
    assert r.status_code == 401
    r = await client.get(f"{A2A_PATH}/.well-known/agent-card.json", headers=AUTH)
    assert r.status_code == 200
    card = r.json()
    assert card["name"] == "{{cookiecutter.agent_directory}}"
    assert "bearer" in card["securitySchemes"]
    assert card["supportedInterfaces"][0]["url"] == f"http://testserver{A2A_PATH}"

    r = await client.post(A2A_PATH, json={"jsonrpc": "2.0", "id": "1", "method": "SendMessage"})
    assert r.status_code == 401

    from a2a.client import ClientConfig, create_client
    from a2a.types import Message, Part, Role, SendMessageRequest, TaskState

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", headers=AUTH, timeout=30
    ) as http:
        a2a_client = await create_client(
            f"http://testserver{A2A_PATH}", ClientConfig(streaming=True, httpx_client=http)
        )
        message = Message(
            message_id=f"msg-{uuid.uuid4()}", role=Role.ROLE_USER, parts=[Part(text="Hi!")]
        )
        responses = [
            chunk async for chunk in a2a_client.send_message(SendMessageRequest(message=message))
        ]
    assert responses, "No responses received from the A2A stream"

    def _completed(chunk: object) -> bool:
        if chunk.HasField("status_update"):
            return chunk.status_update.status.state == TaskState.TASK_STATE_COMPLETED
        if chunk.HasField("task"):
            return chunk.task.status.state == TaskState.TASK_STATE_COMPLETED
        return False

    text = "".join(
        part.text
        for chunk in responses
        if chunk.HasField("artifact_update")
        for part in chunk.artifact_update.artifact.parts
    )
    assert any(_completed(chunk) for chunk in responses), responses
    assert "Hello" in text, responses
