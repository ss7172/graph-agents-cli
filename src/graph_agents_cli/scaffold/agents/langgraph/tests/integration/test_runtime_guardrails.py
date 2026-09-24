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

"""Production guardrails of the chat API, in-process (fastapi runtime, fake model, memory).

One run per thread (409), run timeout, recursion limit, heartbeats, client
disconnect, request and metadata caps, what reaches checkpoints and traces,
generic errors, thread list/delete, retention, readiness, metrics, request ids
and CORS.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing
from datetime import UTC, datetime, timedelta
from typing import Any

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
from langchain_core.messages import AIMessageChunk

from {{cookiecutter.agent_directory}} import agent as agent_module
from {{cookiecutter.agent_directory}}.app_utils.auth import Principal
from {{cookiecutter.agent_directory}}.app_utils.chat import RUNTIME, ChatRequest, sse_encode
from {{cookiecutter.agent_directory}}.app_utils.limits import SettingsError
from {{cookiecutter.agent_directory}}.app_utils.middleware import RunStreamingResponse
from {{cookiecutter.agent_directory}}.fast_api_app import app

AUTH = {"Authorization": "Bearer test-key"}
SHARED = Principal(id="shared", roles=["shared"])
LIMIT_VARS = (
    "RUN_TIMEOUT_S",
    "RECURSION_LIMIT",
    "MAX_REQUEST_BYTES",
    "MAX_METADATA_KEYS",
    "MAX_METADATA_VALUE_CHARS",
    "SSE_HEARTBEAT_S",
    "RETENTION_DAYS",
    "TRACE_CAPTURE",
    "METRICS_ENABLED",
    "CORS_ALLOW_ORIGINS",
    "AUTH_READ_ACROSS_ROLES",
    "METRICS_TOKEN",
    "PRINCIPAL_HASH_SALT",
)


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    for name in LIMIT_VARS:
        monkeypatch.delenv(name, raising=False)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", timeout=30
        ) as c:
            yield c


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


async def chat(
    client: httpx.AsyncClient, message: str, thread_id: str | None = None, **body: Any
) -> httpx.Response:
    payload: dict[str, Any] = {"message": message, **body}
    if thread_id:
        payload["thread_id"] = thread_id
    return await client.post("/chat", json=payload, headers=AUTH)


def slow_graph(
    monkeypatch: pytest.MonkeyPatch, *, pause: float, started: asyncio.Event | None = None
):
    """Make the graph emit one text chunk, then stall for `pause` seconds, then finish."""

    async def astream(*args: Any, **kwargs: Any):
        if started is not None:
            started.set()
        yield "messages", (AIMessageChunk(content="partial ", id="ai-slow"), {})
        await asyncio.sleep(pause)
        yield "messages", (AIMessageChunk(content="done", id="ai-slow"), {})

    monkeypatch.setattr(agent_module.graph, "astream", astream)


# --- one run per thread ---------------------------------------------------------


async def test_a_second_run_on_a_busy_thread_gets_409_and_nothing_is_lost(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    thread_id = str(uuid.uuid4())
    started = asyncio.Event()
    original = agent_module.graph.astream
    slow_graph(monkeypatch, pause=0.5, started=started)
    first = asyncio.create_task(chat(client, "first", thread_id))
    await started.wait()
    second = await chat(client, "second", thread_id)
    assert second.status_code == 409
    assert second.json() == {
        "code": "thread_busy",
        "detail": "This thread already has a run in progress.",
    }
    assert parse_sse((await first).text)[-1][0] == "message.end"
    monkeypatch.setattr(agent_module.graph, "astream", original)
    # The lock is released with the run: the thread takes the next turn, and
    # every accepted turn is in the history.
    assert parse_sse((await chat(client, "hello again", thread_id)).text)[-1][0] == "message.end"
    messages = (await client.get(f"/threads/{thread_id}/messages", headers=AUTH)).json()
    assert [m["content"] for m in messages if m["role"] == "user"] == ["hello again"]


async def test_concurrent_turns_are_serialised_not_interleaved(client: httpx.AsyncClient) -> None:
    thread_id = str(uuid.uuid4())
    await chat(client, "hello", thread_id)
    responses = await asyncio.gather(*(chat(client, f"turn {i}", thread_id) for i in range(8)))
    accepted = [r for r in responses if r.status_code == 200]
    assert accepted and all(r.status_code in (200, 409) for r in responses)
    messages = (await client.get(f"/threads/{thread_id}/messages", headers=AUTH)).json()
    users = [m["content"] for m in messages if m["role"] == "user"]
    # Every turn that got 200 is persisted, each followed by its reply.
    assert len(users) == 1 + len(accepted)
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant"] * len(users)


# --- guardrails --------------------------------------------------------------------


async def test_run_timeout_cancels_the_run_and_frees_the_thread(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUN_TIMEOUT_S", "0.3")
    slow_graph(monkeypatch, pause=30)
    thread_id = str(uuid.uuid4())
    events = parse_sse((await chat(client, "hello", thread_id)).text)
    assert [e for e, _ in events] == ["message.start", "message.delta", "error"]
    assert events[-1][1]["code"] == "timeout" and "0.3 s" in events[-1][1]["message"]
    timeout = events[-1][1]
    assert timeout["error_id"] in timeout["message"] and timeout["run_id"] == events[0][1]["run_id"]
    assert RUNTIME.runs is not None
    record = await RUNTIME.runs.get(events[0][1]["run_id"])
    assert record is not None and record.status == "timeout"
    assert thread_id not in RUNTIME.locks.held


async def test_a_run_stopped_mid_tool_call_leaves_a_usable_thread(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, use_test_tools
) -> None:
    """The open tool call gets an error result, so the next turn's history is valid."""
    import time

    from langchain_core.tools import tool

    @tool
    def slow_probe(query: str) -> str:
        """Test-only tool that outlives the run timeout."""
        time.sleep(1.0)
        return "late"

    use_test_tools(slow_probe)
    monkeypatch.setenv("RUN_TIMEOUT_S", "0.4")
    thread_id = str(uuid.uuid4())
    events = parse_sse((await chat(client, "Run the slow probe for Paris", thread_id)).text)
    assert [e for e, _ in events] == ["message.start", "tool.call", "error"]
    messages = (await client.get(f"/threads/{thread_id}/messages", headers=AUTH)).json()
    assert [m["role"] for m in messages] == ["user", "assistant", "tool"]
    assert messages[2]["is_error"] is True and "did not finish" in messages[2]["content"]
    assert messages[2]["tool_call_id"] == messages[1]["tool_calls"][0]["id"]
    monkeypatch.delenv("RUN_TIMEOUT_S")
    events = parse_sse((await chat(client, "hello", thread_id)).text)
    assert events[-1][0] == "message.end"


async def test_idle_streams_get_heartbeat_comments(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SSE_HEARTBEAT_S", "0.05")
    slow_graph(monkeypatch, pause=0.3)
    text = (await chat(client, "hello")).text
    assert ": keep-alive\n\n" in text
    assert parse_sse(text)[-1][0] == "message.end"


async def test_recursion_limit_stops_a_looping_run(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, use_test_tools
) -> None:
    from langchain_core.tools import tool

    @tool
    def probe(query: str) -> str:
        """Test-only tool."""
        return "ok"

    monkeypatch.setenv("RECURSION_LIMIT", "1")
    use_test_tools(probe)
    events = parse_sse((await chat(client, "Run the probe for San Francisco")).text)
    assert events[-1][0] == "error"
    assert events[-1][1]["code"] == "recursion_limit" and "(1 steps)" in events[-1][1]["message"]


async def test_the_run_uses_the_configured_recursion_limit(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}
    original = agent_module.graph.astream

    def capture(*args: Any, **kwargs: Any):
        seen.update(kwargs["config"])
        return original(*args, **kwargs)

    monkeypatch.setattr(agent_module.graph, "astream", capture)
    await chat(client, "hello")
    assert seen["recursion_limit"] == 25
    assert agent_module.graph.config["recursion_limit"] == 25  # native default too


async def test_a_disconnected_client_cancels_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """The run is cancelled when the client goes away, recorded, and the thread freed."""
    started = asyncio.Event()
    slow_graph(monkeypatch, pause=30, started=started)
    async with app.router.lifespan_context(app):
        req = ChatRequest(message="hello")
        thread_id = await RUNTIME.resolve_thread(SHARED, req)
        lease = await RUNTIME.acquire_thread(thread_id)
        run_ids: list[str] = []

        async def events() -> AsyncIterator[str]:
            async with aclosing(RUNTIME.stream(SHARED, req, thread_id, lease=lease)) as stream:
                async for event, data in stream:
                    if event == "message.start":
                        run_ids.append(data["run_id"])
                    yield sse_encode(event, data)

        response = RunStreamingResponse(
            events(), on_close=lease.release, media_type="text/event-stream"
        )
        sent: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        async def receive() -> dict[str, Any]:
            await started.wait()
            await asyncio.sleep(0.05)
            return {"type": "http.disconnect"}

        scope = {"type": "http", "asgi": {"spec_version": "2.3"}}
        await asyncio.wait_for(response(scope, receive, send), timeout=5)
        assert RUNTIME.runs is not None
        record = await RUNTIME.runs.get(run_ids[0])
        assert record is not None and record.status == "cancelled"
        assert lease.released and thread_id not in RUNTIME.locks.held


# --- request limits ------------------------------------------------------------------


async def test_request_bodies_over_the_cap_get_413(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAX_REQUEST_BYTES", "2000")
    big = {"message": "x" * 3000}
    r = await client.post("/chat", json=big, headers=AUTH)
    assert r.status_code == 413 and "MAX_REQUEST_BYTES" in r.json()["detail"]

    async def chunks() -> AsyncIterator[bytes]:  # no Content-Length: counted while read
        body = json.dumps(big).encode()
        for i in range(0, len(body), 500):
            yield body[i : i + 500]

    r = await client.post(
        "/chat", content=chunks(), headers={**AUTH, "content-type": "application/json"}
    )
    assert r.status_code == 413
    # Unauthenticated callers are refused just the same, before any parsing.
    assert (await client.post("/chat", json=big)).status_code == 413


async def test_deeply_nested_json_is_refused_not_a_crash(client: httpx.AsyncClient) -> None:
    body = '{"message": "hi", "metadata": {"a": ' + "[" * 100_000 + "]" * 100_000 + "}}"
    r = await client.post(
        "/chat", content=body.encode(), headers={**AUTH, "content-type": "application/json"}
    )
    assert 400 <= r.status_code < 500


async def test_a2a_style_callers_get_thread_busy_as_an_event(client: httpx.AsyncClient) -> None:
    """`stream()` without a lease (the A2A path) takes the lock itself."""
    thread_id = await RUNTIME.resolve_thread(SHARED, ChatRequest(message="x"))
    lease = await RUNTIME.acquire_thread(thread_id)
    events = [e async for e in RUNTIME.stream(SHARED, ChatRequest(message="x"), thread_id)]
    assert events == [
        ("error", {"code": "thread_busy", "message": "This thread already has a run in progress."})
    ]
    await lease.release()
    events = [e async for e in RUNTIME.stream(SHARED, ChatRequest(message="x"), thread_id)]
    assert events[-1][0] == "message.end"


async def test_a_database_failure_is_a_generic_503(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert RUNTIME.threads is not None

    async def down(*args: Any, **kwargs: Any) -> Any:
        raise OSError("connection to server at 10.0.0.7, port 5432 failed")

    monkeypatch.setattr(RUNTIME.threads, "ensure", down)
    r = await chat(client, "hello")
    assert r.status_code == 503 and "10.0.0.7" not in r.text
    assert r.json()["detail"].startswith("Database unavailable. Reference: ")


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
async def test_non_finite_numbers_get_422_not_500(client: httpx.AsyncClient, value: str) -> None:
    """Python's JSON parser accepts NaN/Infinity; the 422 that echoes them must stay valid JSON."""
    for body in (
        '{"message": "hi", "metadata": {"k": VALUE}}',
        '{"message": VALUE}',
        '{"message": "hi", "metadata": {"k": [VALUE]}}',
    ):
        r = await client.post(
            "/chat",
            content=body.replace("VALUE", value).encode(),
            headers={**AUTH, "content-type": "application/json"},
        )
        assert r.status_code == 422, r.text

        def refuse(constant: str) -> None:
            raise AssertionError(f"non-standard JSON constant {constant} in the response")

        detail = json.loads(r.text, parse_constant=refuse)["detail"]
        assert detail and all({"type", "loc", "msg"} <= set(error) for error in detail)


@pytest.mark.parametrize(
    "metadata",
    [
        {f"k{i}": i for i in range(17)},
        {"k": "v" * 257},
        {"k" * 257: "v"},
        {"nested": {"a": 1}},
        {"list": [1, 2]},
    ],
)
async def test_metadata_outside_the_caps_gets_422(
    client: httpx.AsyncClient, metadata: dict[str, Any]
) -> None:
    r = await chat(client, "hello", metadata=metadata)
    assert r.status_code == 422, r.text


async def test_metadata_at_the_caps_is_accepted(client: httpx.AsyncClient) -> None:
    metadata = {f"k{i}": "v" * 256 for i in range(15)} | {"n": None}
    assert (await chat(client, "hello", metadata=metadata)).status_code == 200


@pytest.mark.parametrize("thread_id", ["bad id", "a/b", "x" * 129, "ümlaut"])
async def test_invalid_thread_ids_get_422_everywhere(
    client: httpx.AsyncClient, thread_id: str
) -> None:
    assert (await chat(client, "hello", thread_id)).status_code == 422
    quoted = httpx.URL(f"/threads/{thread_id.replace('/', '%2F')}/messages")
    r = await client.get(quoted, headers=AUTH)
    assert r.status_code in (404, 422)  # an unroutable path never reaches a store
    with pytest.raises(Exception) as exc:
        await RUNTIME.resolve_thread(SHARED, ChatRequest(message="x", thread_id=thread_id))
    assert getattr(exc.value, "status_code", None) == 422


# --- what is persisted and traced --------------------------------------------------


@pytest.mark.parametrize("capture", ["metadata", "full"])
async def test_client_metadata_stays_out_of_checkpoints_and_cannot_spoof_ids(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, capture: str
) -> None:
    monkeypatch.setenv("TRACE_CAPTURE", capture)
    seen: dict[str, Any] = {}
    original = agent_module.graph.astream

    def capture_config(*args: Any, **kwargs: Any):
        seen.update(kwargs["config"])
        return original(*args, **kwargs)

    monkeypatch.setattr(agent_module.graph, "astream", capture_config)
    thread_id = str(uuid.uuid4())
    client_meta = {
        "thread_id": "SPOOFED",
        "run_id": "SPOOFED",
        "principal_hash": "SPOOFED",
        "email": "pii@example.com",
    }
    events = parse_sse((await chat(client, "hello", thread_id, metadata=client_meta)).text)
    run_id = events[0][1]["run_id"]
    trace_meta = seen["metadata"]
    assert trace_meta["thread_id"] == thread_id and trace_meta["run_id"] == run_id
    assert trace_meta["principal_hash"] == SHARED.hashed_id()
    if capture == "full":
        assert trace_meta["client_metadata"] == client_meta
    else:
        assert "client_metadata" not in trace_meta and "pii@example.com" not in str(trace_meta)
    # No checkpoint of the thread carries the client's metadata.
    checkpoints = [
        c
        async for c in agent_module.graph.checkpointer.alist(
            {"configurable": {"thread_id": thread_id}}
        )
    ]
    assert checkpoints
    assert all("pii@example.com" not in str(c.metadata) for c in checkpoints)
    assert all(c.metadata.get("run_id") == run_id for c in checkpoints)
    # The run record keeps it.
    assert RUNTIME.runs is not None
    record = await RUNTIME.runs.get(run_id)
    assert record is not None and record.metadata == client_meta


# --- threads API and retention -----------------------------------------------------


async def test_threads_can_be_listed_and_deleted_by_their_owner(client: httpx.AsyncClient) -> None:
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    await chat(client, "hello", first)
    await chat(client, "hello", second)
    listed = (await client.get("/threads", headers=AUTH)).json()
    ids = [t["thread_id"] for t in listed]
    assert ids.index(second) < ids.index(first)  # most recently active first
    assert set(listed[0]) == {"thread_id", "created_at", "updated_at"}
    assert (await client.get("/threads?limit=1", headers=AUTH)).json()[0]["thread_id"] == second
    assert (await client.get("/threads?limit=0", headers=AUTH)).status_code == 422

    r = await client.delete(f"/threads/{first}", headers=AUTH)
    assert r.status_code == 204
    assert (await client.get(f"/threads/{first}/messages", headers=AUTH)).status_code == 404
    assert first not in [
        t["thread_id"] for t in (await client.get("/threads", headers=AUTH)).json()
    ]
    state = await agent_module.graph.aget_state({"configurable": {"thread_id": first}})
    assert not state.values  # checkpoints gone
    assert RUNTIME.runs is not None and await RUNTIME.runs.list_for_thread(first) == []
    assert (await client.delete(f"/threads/{first}", headers=AUTH)).status_code == 404
    assert (await client.delete("/threads/bad%20id", headers=AUTH)).status_code == 422
    assert (await client.delete(f"/threads/{second}")).status_code == 401


async def test_only_the_owner_deletes_and_never_during_a_run(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    thread_id = str(uuid.uuid4())
    await chat(client, "hello", thread_id)
    monkeypatch.setenv("AUTH_READ_ACROSS_ROLES", "auditor")
    for other in (Principal(id="stranger"), Principal(id="auditor", roles=["auditor"])):
        with pytest.raises(Exception) as exc:
            await RUNTIME.delete_thread(other, thread_id)
        assert getattr(exc.value, "status_code", None) == 403
    lease = await RUNTIME.acquire_thread(thread_id)
    r = await client.delete(f"/threads/{thread_id}", headers=AUTH)
    assert r.status_code == 409 and r.json()["code"] == "thread_busy"
    await lease.release()
    assert (await client.delete(f"/threads/{thread_id}", headers=AUTH)).status_code == 204


async def test_a_delete_racing_a_chat_leaves_no_ownerless_state(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DELETE that lands after /chat checked ownership, before it took the run lock.

    The turn must not be written to a thread without an owner row, where another
    principal could claim the id and read it.
    """
    thread_id = str(uuid.uuid4())
    await chat(client, "first", thread_id)
    resolve = RUNTIME.resolve_thread

    async def resolve_then_delete(principal: Principal, req: ChatRequest) -> str:
        resolved = await resolve(principal, req)
        if req.message == "secret second turn":
            await RUNTIME.delete_thread(principal, resolved)  # the racing DELETE
        return resolved

    monkeypatch.setattr(RUNTIME, "resolve_thread", resolve_then_delete)
    events = parse_sse((await chat(client, "secret second turn", thread_id)).text)
    assert events[-1][0] == "message.end"
    assert RUNTIME.threads is not None
    record = await RUNTIME.threads.get(thread_id)
    # The turn started the thread afresh under its sender, never ownerless.
    assert record is not None and record.principal_id == "shared"
    bob = Principal(id="bob")
    with pytest.raises(Exception) as exc:
        await RUNTIME.resolve_thread(bob, ChatRequest(message="mine now", thread_id=thread_id))
    assert getattr(exc.value, "status_code", None) == 403


async def test_a_delete_rechecks_the_owner_under_the_lock(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    thread_id = str(uuid.uuid4())
    await chat(client, "hello", thread_id)
    acquire = RUNTIME.acquire_thread
    assert RUNTIME.threads is not None

    async def acquire_after_a_reclaim(tid: str, principal: Principal | None = None) -> Any:
        # Between the owner check and the lock, the thread goes and bob claims the id.
        await RUNTIME._delete_thread_data(tid)
        await RUNTIME.threads.claim(tid, Principal(id="bob"))
        return await acquire(tid, principal)

    monkeypatch.setattr(RUNTIME, "acquire_thread", acquire_after_a_reclaim)
    assert (await client.delete(f"/threads/{thread_id}", headers=AUTH)).status_code == 403
    record = await RUNTIME.threads.get(thread_id)
    assert record is not None and record.principal_id == "bob"
    assert thread_id not in RUNTIME.locks.held


async def test_a2a_style_runs_recheck_ownership_under_the_lock(client: httpx.AsyncClient) -> None:
    """`stream()` taking the lock itself re-checks the owner too (the A2A path)."""
    thread_id = str(uuid.uuid4())
    await chat(client, "first", thread_id)
    req = ChatRequest(message="second", thread_id=thread_id)
    resolved = await RUNTIME.resolve_thread(SHARED, req)
    assert RUNTIME.threads is not None
    await RUNTIME.delete_thread(SHARED, resolved)
    # Someone else claims the id in between: the run is refused, nothing is written.
    await RUNTIME.threads.claim(thread_id, Principal(id="bob"))
    events = [e async for e in RUNTIME.stream(SHARED, req, resolved)]
    assert events == [
        ("error", {"code": "forbidden", "message": "This thread belongs to another principal."})
    ]
    state = await agent_module.graph.aget_state({"configurable": {"thread_id": thread_id}})
    assert not state.values
    assert thread_id not in RUNTIME.locks.held


async def test_a_thread_id_with_state_but_no_owner_cannot_be_claimed(
    client: httpx.AsyncClient,
) -> None:
    """Checkpoints without an owner row (e.g. left by an older version) never pass to a new owner."""
    thread_id = str(uuid.uuid4())
    await agent_module.graph.ainvoke(
        {"messages": [{"role": "user", "content": "orphaned secret"}]},
        {"configurable": {"thread_id": thread_id}},
    )
    r = await chat(client, "let me read that", thread_id)
    assert r.status_code == 403
    assert RUNTIME.threads is not None and await RUNTIME.threads.get(thread_id) is None
    assert (await client.get(f"/threads/{thread_id}/messages", headers=AUTH)).status_code == 404


async def test_retention_rechecks_idleness_under_the_lock(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A thread resumed after the purge listed it (and before it was locked) is kept."""
    thread_id = str(uuid.uuid4())
    await chat(client, "hello", thread_id)
    assert RUNTIME.threads is not None
    RUNTIME.threads._memory[thread_id].updated_at = (
        datetime.now(tz=UTC) - timedelta(days=40)
    ).isoformat()
    idle_before = RUNTIME.threads.idle_before

    async def list_then_resume(cutoff_iso: str, *, limit: int = 500) -> list[str]:
        candidates = await idle_before(cutoff_iso, limit=limit)
        assert thread_id in candidates
        await chat(client, "resumed", thread_id)  # the owner comes back mid-round
        return candidates

    monkeypatch.setattr(RUNTIME.threads, "idle_before", list_then_resume)
    assert await RUNTIME.purge_expired(30) == 0
    assert await RUNTIME.threads.get(thread_id) is not None
    messages = (await client.get(f"/threads/{thread_id}/messages", headers=AUTH)).json()
    assert [m["content"] for m in messages if m["role"] == "user"] == ["hello", "resumed"]


async def test_retention_purges_idle_threads_only(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    idle, busy, fresh = (str(uuid.uuid4()) for _ in range(3))
    for thread_id in (idle, busy, fresh):
        await chat(client, "hello", thread_id)
    assert RUNTIME.threads is not None
    old = (datetime.now(tz=UTC) - timedelta(days=40)).isoformat()
    RUNTIME.threads._memory[idle].updated_at = old
    RUNTIME.threads._memory[busy].updated_at = old
    assert await RUNTIME.purge_expired(0) == 0  # 0 keeps everything
    lease = await RUNTIME.acquire_thread(busy)
    assert await RUNTIME.purge_expired(30) == 1
    await lease.release()
    assert await RUNTIME.threads.get(idle) is None
    assert await RUNTIME.threads.get(busy) is not None and await RUNTIME.threads.get(fresh)
    state = await agent_module.graph.aget_state({"configurable": {"thread_id": idle}})
    assert not state.values
    # A continued thread is active again: its idle clock restarts.
    RUNTIME.threads._memory[fresh].updated_at = old
    await chat(client, "again", fresh)
    assert await RUNTIME.purge_expired(30) == 1  # only `busy`, now idle and free


# --- errors, readiness, metrics, request ids, CORS ----------------------------------


async def test_unhandled_errors_answer_a_generic_500_with_an_error_id(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("connection to 10.0.0.7:5432 refused")

    monkeypatch.setattr(RUNTIME, "list_threads", boom)
    r = await client.get("/threads", headers={**AUTH, "X-Request-ID": "req-500"})
    assert r.status_code == 500
    body = r.json()
    assert "10.0.0.7" not in r.text and body["error_id"] in body["detail"]
    # The 500 comes from outside the request-id middleware; it still names the request.
    assert r.headers["x-request-id"] == "req-500"
    r = await client.get("/threads", headers=AUTH)
    assert r.status_code == 500 and len(r.headers["x-request-id"]) == 32


async def test_ready_reflects_the_database(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = await client.get("/ready")
    assert r.status_code == 200 and r.json() == {"status": "ready"}
    assert RUNTIME.db is not None

    async def down() -> None:
        raise OSError("database unreachable")

    monkeypatch.setattr(RUNTIME.db, "ping", down)
    r = await client.get("/ready")
    assert r.status_code == 503 and r.json() == {"status": "not_ready"}
    assert (await client.get("/health")).status_code == 200  # liveness is process-only


async def test_metrics_count_requests_and_runs(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await chat(client, "hello")
    await client.get(f"/threads/{uuid.uuid4()}/messages", headers=AUTH)
    r = await client.get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    text = r.text
    assert 'agent_runs_total{status="ok"}' in text
    assert 'http_requests_total{method="POST",route="/chat",status="200"}' in text
    # Route templates, never raw paths (no series per thread id).
    assert 'route="/threads/{thread_id}/messages"' in text
    assert "agent_active_runs" in text and "agent_tokens_total" in text
    monkeypatch.setenv("METRICS_ENABLED", "false")
    assert (await client.get("/metrics")).status_code == 404


async def test_metrics_token_protects_metrics_only(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("METRICS_TOKEN", "scrape-secret-0123456789")
    for headers in (
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": "Basic scrape-secret-0123456789"},
        AUTH,  # the API key is not the metrics token
    ):
        r = await client.get("/metrics", headers=headers)
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
        assert "agent_runs_total" not in r.text
    r = await client.get("/metrics", headers={"Authorization": "Bearer scrape-secret-0123456789"})
    assert r.status_code == 200 and "http_requests_total" in r.text
    # Probes stay open.
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/ready")).status_code == 200
    monkeypatch.setenv("METRICS_TOKEN", "  ")  # blank = not set
    assert (await client.get("/metrics")).status_code == 200


async def test_request_ids_are_echoed_or_generated(client: httpx.AsyncClient) -> None:
    r = await client.get("/health", headers={"X-Request-ID": "req-123.abc"})
    assert r.headers["x-request-id"] == "req-123.abc"
    r = await client.get("/health", headers={"X-Request-ID": "bad id\r\nx"[:6]})
    assert r.headers["x-request-id"] != "bad id" and len(r.headers["x-request-id"]) == 32
    r = await client.get("/health")
    assert len(r.headers["x-request-id"]) == 32


async def test_bad_settings_stop_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    bad = {
        "RUN_TIMEOUT_S": "five minutes",
        "MAX_METADATA_KEYS": "-1",
        "TRACE_CAPTURE": "everything",
        "A2A_TASK_TTL_S": "an hour",
    }
    for name, value in bad.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(SettingsError) as exc:
        async with app.router.lifespan_context(app):
            pass
    for name in bad:
        assert name in str(exc.value)


async def test_cors_is_off_by_default_and_follows_cors_allow_origins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    from {{cookiecutter.agent_directory}} import fast_api_app as module

    preflight = {
        "Origin": "https://app.example.com",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=module.app), base_url="http://testserver"
    ) as c:
        r = await c.options("/chat", headers=preflight)
        assert "access-control-allow-origin" not in r.headers
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://app.example.com")
    try:
        cors_app = importlib.reload(module).app
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=cors_app), base_url="http://testserver"
        ) as c:
            r = await c.options("/chat", headers=preflight)
            assert r.status_code == 200
            assert r.headers["access-control-allow-origin"] == "https://app.example.com"
            assert r.headers["access-control-allow-credentials"] == "true"
            r = await c.options("/chat", headers={**preflight, "Origin": "https://evil.example"})
            assert "access-control-allow-origin" not in r.headers
    finally:
        monkeypatch.delenv("CORS_ALLOW_ORIGINS")
        importlib.reload(module)
