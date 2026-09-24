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

"""The langgraph-server branch of ChatRuntime against a fake SDK client (no server needed).

The loopback SDK client runs under the server's `/noauth` root path, so the
server's own auth filters never see the custom routes' calls: ownership has to
be enforced by the app from the thread metadata it wrote at creation.

The fake raises the SDK's real error types (`NotFoundError`, `ConflictError`),
whose messages do not contain the status code: the runtime must go by the
status, not the text.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

errors = pytest.importorskip("langgraph_sdk.errors")

from {{cookiecutter.agent_directory}}.app_utils.auth import Principal  # noqa: E402
from {{cookiecutter.agent_directory}}.app_utils.chat import (  # noqa: E402
    LANGGRAPH_SERVER,
    ChatRequest,
    ChatRuntime,
)
from {{cookiecutter.agent_directory}}.app_utils.db import Database, RunRecord, RunStore  # noqa: E402
from {{cookiecutter.agent_directory}}.app_utils.threads import ThreadLocks, ThreadStore  # noqa: E402

OWNER = Principal(
    id="A",
    roles=["viewer"],
    attributes={"tenant": "t1", "credentials": {"example": "secret-token-A"}},
)
STRANGER = Principal(id="B", roles=["viewer"])
AUDITOR = Principal(id="C", roles=["auditor"])
THREAD = "11111111-1111-1111-1111-111111111111"
OTHER = "44444444-4444-4444-4444-444444444444"


def sdk_error(cls: type[Exception], status: int, message: str) -> Exception:
    """An SDK error exactly as langgraph_sdk raises it (the text has no status code)."""
    request = httpx.Request("GET", "http://loopback/threads/x")
    response = httpx.Response(status, request=request, json={"detail": message})
    return cls(message, response=response, body={"detail": message})


class FakeSdk:
    """`langgraph_sdk.get_client` stand-in: one thread owned by A, recording every call."""

    def __init__(self) -> None:
        self.headers: list[dict[str, str] | None] = []
        self.created: list[dict[str, Any]] = []
        self.streams: list[dict[str, Any]] = []
        self.cancelled: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self.searches: list[dict[str, Any]] = []
        self.state_updates: list[dict[str, Any]] = []
        self.slow_stream = False
        self.stream_error: dict[str, Any] | None = None
        # The message types of the thread when each run stream started.
        self.history_at_stream: list[list[Any]] = []
        self.threads_by_id: dict[str, dict[str, Any]] = {
            THREAD: {
                "thread_id": THREAD,
                "metadata": {"principal_id": "A", "tenant": None},
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-02T00:00:00+00:00",
                "status": "idle",
            }
        }
        self.messages = [
            {"type": "human", "id": "m1", "content": "hello"},
            {
                "type": "ai",
                "id": "m2",
                "content": "",
                "tool_calls": [{"id": "c1", "name": "probe", "args": {"query": "SF"}}],
            },
            {
                "type": "tool",
                "id": "m3",
                "tool_call_id": "c1",
                "name": "probe",
                "content": "sunny",
            },
            {"type": "ai", "id": "m4", "content": "It is sunny."},
        ]

    def get_client(
        self, *, url: str | None = None, headers: dict[str, str] | None = None, **_: Any
    ):
        self.headers.append(headers)
        sdk = self

        class _Threads:
            async def get(self, thread_id: str, **_: Any) -> dict[str, Any]:
                if thread_id not in sdk.threads_by_id:
                    raise sdk_error(
                        errors.NotFoundError, 404, f"Thread with ID {thread_id} not found"
                    )
                return sdk.threads_by_id[thread_id]

            async def create(
                self,
                *,
                thread_id: str | None = None,
                metadata: Any = None,
                if_exists: str | None = None,
                **_: Any,
            ):
                thread_id = thread_id or "22222222-2222-2222-2222-222222222222"
                if thread_id in sdk.threads_by_id:
                    if if_exists != "do_nothing":
                        raise sdk_error(errors.ConflictError, 409, "Thread already exists")
                    return sdk.threads_by_id[thread_id]
                record = {"thread_id": thread_id, "metadata": dict(metadata or {})}
                sdk.threads_by_id[thread_id] = record
                sdk.created.append(record)
                return record

            async def get_state(self, thread_id: str, **_: Any) -> dict[str, Any]:
                if thread_id not in sdk.threads_by_id:
                    raise sdk_error(errors.NotFoundError, 404, "Thread not found")
                return {"values": {"messages": sdk.messages}}

            async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
                sdk.searches.append(kwargs)
                wanted = (kwargs.get("metadata") or {}).get("principal_id")
                found = [
                    t
                    for t in sdk.threads_by_id.values()
                    if wanted is None or (t.get("metadata") or {}).get("principal_id") == wanted
                ]
                found.sort(
                    key=lambda t: t.get("updated_at") or "",
                    reverse=kwargs.get("sort_order") == "desc",
                )
                return found

            async def delete(self, thread_id: str, **_: Any) -> None:
                sdk.deleted.append(thread_id)
                sdk.threads_by_id.pop(thread_id, None)

            async def update_state(self, thread_id: str, values: Any, **kwargs: Any) -> None:
                sdk.state_updates.append({"thread_id": thread_id, "values": values, **kwargs})
                # Apply it as the server's `add_messages` would: a remove-all
                # marker replaces the history, other messages are appended.
                for message in (values or {}).get("messages", []):
                    if message.get("type") == "remove" and message.get("id") == "__remove_all__":
                        sdk.messages = []
                    else:
                        sdk.messages = [*sdk.messages, message]

        class _Runs:
            async def stream(
                self, thread_id: str, assistant_id: str, **kwargs: Any
            ) -> AsyncIterator[Any]:
                sdk.streams.append({"thread_id": thread_id, "assistant_id": assistant_id, **kwargs})
                sdk.history_at_stream.append([m.get("type") for m in sdk.messages])
                yield SimpleNamespace(event="metadata", data={"run_id": "srv-run-1", "attempt": 1})
                if sdk.stream_error is not None:
                    yield SimpleNamespace(event="error", data=sdk.stream_error)
                    return
                if sdk.slow_stream:
                    await asyncio.sleep(30)
                yield SimpleNamespace(
                    event="updates",
                    data={
                        "agent": {
                            "messages": [
                                {
                                    "type": "ai",
                                    "id": "ai-1",
                                    "content": "",
                                    "tool_calls": [
                                        {"id": "c1", "name": "probe", "args": {"query": "SF"}}
                                    ],
                                    "usage_metadata": {"input_tokens": 3, "output_tokens": 4},
                                }
                            ]
                        }
                    },
                )
                yield SimpleNamespace(
                    event="updates",
                    data={
                        "tools": {
                            "messages": [
                                {
                                    "type": "tool",
                                    "tool_call_id": "c1",
                                    "name": "probe",
                                    "content": "sunny",
                                }
                            ]
                        }
                    },
                )
                yield SimpleNamespace(
                    event="messages",
                    data=[
                        {"type": "AIMessageChunk", "id": "ai-2", "content": "It is "},
                        {"langgraph_node": "agent"},
                    ],
                )
                yield SimpleNamespace(
                    event="messages",
                    data=[
                        {"type": "AIMessageChunk", "id": "ai-2", "content": "sunny."},
                        {"langgraph_node": "agent"},
                    ],
                )

            async def cancel(self, thread_id: str, run_id: str, **_: Any) -> None:
                sdk.cancelled.append((thread_id, run_id))

        class _Assistants:
            async def search(self, **_: Any) -> list[dict[str, Any]]:
                return [{"assistant_id": "agent"}]

        return SimpleNamespace(threads=_Threads(), runs=_Runs(), assistants=_Assistants())


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> tuple[ChatRuntime, FakeSdk]:
    sdk = FakeSdk()
    monkeypatch.setitem(sys.modules, "langgraph_sdk", SimpleNamespace(get_client=sdk.get_client))
    monkeypatch.setenv("RUNTIME", "langgraph-server")
    for name in ("AUTH_READ_ACROSS_ROLES", "TRACE_CAPTURE", "AUTH_FORWARD_HEADERS", "APP_ENV"):
        monkeypatch.delenv(name, raising=False)
    rt = ChatRuntime()
    assert rt.runtime == LANGGRAPH_SERVER
    # What start() sets up, without a server: in-memory run records and locks.
    rt.db = Database("memory", runs_table="agent_runs", with_threads=False)
    rt.runs = RunStore(rt.db)
    rt.threads = ThreadStore(rt.db)
    rt.locks = ThreadLocks()
    rt.started = True
    return rt, sdk


async def _events(rt: ChatRuntime, principal: Principal, req: ChatRequest, thread_id: str):
    return [(e, d) async for e, d in rt.stream(principal, req, thread_id)]


async def test_owner_continues_its_thread_and_the_stream_maps_the_contract_events(server) -> None:
    rt, sdk = server
    req = ChatRequest(
        message="probe?",
        thread_id=THREAD,
        metadata={"source": "web"},
        forward_headers={
            "authorization": "Bearer k",
            "cookie": "sid=1",
            "x-session-token": "not-forwarded-by-default",
            "x-request-id": "ignored",
        },
    )
    assert await rt.resolve_thread(OWNER, req) == THREAD
    events = await _events(rt, OWNER, req, THREAD)
    names = [e for e, _ in events]
    assert names == [
        "message.start",
        "tool.call",
        "tool.result",
        "message.delta",
        "message.delta",
        "message.end",
    ]
    assert events[1][1] == {"id": "c1", "name": "probe", "args": {"query": "SF"}}
    end = events[-1][1]
    assert end["usage"] == {"input_tokens": 3, "output_tokens": 4} and end["status"] == "ok"
    assert end["thread_id"] == THREAD and end["run_id"] == events[0][1]["run_id"]
    (stream,) = sdk.streams
    assert stream["assistant_id"] == "agent" and stream["stream_mode"] == [
        "messages-tuple",
        "updates",
    ]
    assert stream["multitask_strategy"] == "reject" and stream["on_disconnect"] == "cancel"
    assert stream["config"] == {"recursion_limit": 50}
    # The server persists run context and metadata: no credentials, no raw
    # principal id in metadata, no client metadata under metadata capture.
    assert stream["context"] == {
        "principal_id": "A",
        "roles": ["viewer"],
        "attributes": {"tenant": "t1"},
    }
    assert "secret-token-A" not in repr(stream)
    # `principal_id` overrides the raw owner id the server merges in from the
    # thread metadata (and copies into traces and checkpoint metadata).
    assert stream["metadata"] == {
        "thread_id": THREAD,
        "run_id": end["run_id"],
        "principal_hash": OWNER.hashed_id(),
        "principal_id": OWNER.hashed_id(),
    }
    # Only the configured credential headers reach the SDK client.
    assert sdk.headers[0] == {"authorization": "Bearer k", "cookie": "sid=1"}
    record = await rt.runs.get(end["run_id"])
    assert record is not None and record.status == "ok" and record.metadata == {"source": "web"}


async def test_client_metadata_reaches_traces_only_under_full_capture(server, monkeypatch) -> None:
    rt, sdk = server
    monkeypatch.setenv("TRACE_CAPTURE", "full")
    req = ChatRequest(message="x", thread_id=THREAD, metadata={"run_id": "SPOOFED"})
    await _events(rt, OWNER, req, THREAD)
    meta = sdk.streams[-1]["metadata"]
    assert meta["run_id"] != "SPOOFED" and meta["client_metadata"] == {"run_id": "SPOOFED"}


async def test_forwarded_headers_follow_auth_forward_headers(server, monkeypatch) -> None:
    rt, sdk = server
    headers = {"authorization": "Bearer k", "x-api-key": "k2", "cookie": "c"}
    monkeypatch.setenv("AUTH_FORWARD_HEADERS", "X-Api-Key")
    await rt.messages(OWNER, THREAD, headers)
    assert sdk.headers[-1] == {"x-api-key": "k2"}
    monkeypatch.setenv("AUTH_FORWARD_HEADERS", "")
    await rt.messages(OWNER, THREAD, headers)
    assert sdk.headers[-1] is None


async def test_stranger_cannot_continue_or_read_another_principals_thread(server) -> None:
    rt, sdk = server
    with pytest.raises(HTTPException) as exc:
        await rt.resolve_thread(STRANGER, ChatRequest(message="x", thread_id=THREAD))
    assert exc.value.status_code == 403
    assert sdk.streams == [] and sdk.created == []
    with pytest.raises(HTTPException) as exc:
        await rt.messages(STRANGER, THREAD)
    assert exc.value.status_code == 403


async def test_read_across_role_reads_without_tool_args_and_cannot_write(
    server, monkeypatch
) -> None:
    rt, _sdk = server
    monkeypatch.setenv("AUTH_READ_ACROSS_ROLES", "auditor")
    messages = await rt.messages(AUDITOR, THREAD)
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant"]
    assert messages[1]["tool_calls"] == [{"id": "c1", "name": "probe"}]  # args omitted
    monkeypatch.setenv("TRACE_CAPTURE", "full")
    messages = await rt.messages(AUDITOR, THREAD)
    assert messages[1]["tool_calls"][0]["args"] == {"query": "SF"}
    with pytest.raises(HTTPException) as exc:
        await rt.resolve_thread(AUDITOR, ChatRequest(message="x", thread_id=THREAD))
    assert exc.value.status_code == 403


async def test_owner_reads_tool_args_under_metadata_capture(server) -> None:
    rt, sdk = server
    messages = await rt.messages(OWNER, THREAD, {"authorization": "Bearer k"})
    assert messages[1]["tool_calls"][0]["args"] == {"query": "SF"}
    assert messages[2]["tool_call_id"] == "c1" and messages[2]["is_error"] is False
    assert sdk.headers[-1] == {"authorization": "Bearer k"}


async def test_unknown_thread_is_created_for_the_caller_and_404_on_read(server) -> None:
    """The SDK's NotFoundError (no '404' in its text) means 'absent', never 503."""
    rt, sdk = server
    new_id = "33333333-3333-3333-3333-333333333333"
    with pytest.raises(HTTPException) as exc:
        await rt.messages(STRANGER, new_id)
    assert exc.value.status_code == 404
    assert await rt.resolve_thread(STRANGER, ChatRequest(message="x", thread_id=new_id)) == new_id
    assert sdk.created == [{"thread_id": new_id, "metadata": {"principal_id": "B", "tenant": None}}]
    # A thread the server holds without ownership metadata fails closed.
    bare = "55555555-5555-5555-5555-555555555555"
    sdk.threads_by_id[bare] = {"thread_id": bare, "metadata": {}}
    with pytest.raises(HTTPException) as exc:
        await rt.resolve_thread(OWNER, ChatRequest(message="x", thread_id=bare))
    assert exc.value.status_code == 403


async def test_a_thread_created_in_between_by_someone_else_is_refused(server) -> None:
    """get() says absent, but another request creates the id first: its owner wins (403)."""
    rt, sdk = server
    racing = "66666666-6666-6666-6666-666666666666"
    original_get_client = sdk.get_client

    def get_client(**kwargs: Any):
        client = original_get_client(**kwargs)
        real_get = client.threads.get

        async def get_then_race(thread_id: str, **kw: Any):
            try:
                return await real_get(thread_id, **kw)
            finally:
                sdk.threads_by_id.setdefault(
                    thread_id, {"thread_id": thread_id, "metadata": {"principal_id": "B"}}
                )

        client.threads.get = get_then_race
        return client

    sys.modules["langgraph_sdk"].get_client = get_client
    with pytest.raises(HTTPException) as exc:
        await rt.resolve_thread(OWNER, ChatRequest(message="x", thread_id=racing))
    assert exc.value.status_code == 403


@pytest.mark.parametrize("thread_id", ["t-demo-1", "not a uuid", "x" * 200])
async def test_thread_ids_must_be_uuids_under_the_server_runtime(server, thread_id: str) -> None:
    rt, sdk = server
    for call in (
        rt.resolve_thread(OWNER, ChatRequest(message="x", thread_id=thread_id)),
        rt.messages(OWNER, thread_id),
        rt.delete_thread(OWNER, thread_id),
    ):
        with pytest.raises(HTTPException) as exc:
            await call
        assert exc.value.status_code == 422
    assert sdk.created == [] and sdk.deleted == []


async def test_uuid_spellings_name_one_thread(server) -> None:
    """Canonical ids: the run lock and ownership cannot be split by upper/lower case."""
    rt, sdk = server
    upper = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
    resolved = await rt.resolve_thread(OWNER, ChatRequest(message="x", thread_id=upper))
    assert resolved == upper.lower() and sdk.created[-1]["thread_id"] == upper.lower()
    lease = await rt.acquire_thread(resolved)
    again = await rt.resolve_thread(OWNER, ChatRequest(message="x", thread_id=upper))
    events = await _events(rt, OWNER, ChatRequest(message="x"), again)
    assert events[-1][1]["code"] == "thread_busy"
    await lease.release()


async def test_server_failure_is_a_generic_503_not_403(server, monkeypatch) -> None:
    rt, _sdk = server

    def broken(**_: Any):
        class _Threads:
            async def get(self, thread_id: str, **_: Any):
                raise httpx.ConnectError("loopback down at 10.0.0.7:8123")

        return SimpleNamespace(threads=_Threads(), runs=None)

    monkeypatch.setitem(sys.modules, "langgraph_sdk", SimpleNamespace(get_client=broken))
    with pytest.raises(HTTPException) as exc:
        await rt.resolve_thread(OWNER, ChatRequest(message="x", thread_id=THREAD))
    assert exc.value.status_code == 503
    assert "10.0.0.7" not in exc.value.detail and "Reference:" in exc.value.detail


async def test_stream_error_part_becomes_a_generic_error_event(server, monkeypatch) -> None:
    rt, _sdk = server

    def failing(**_: Any):
        class _Runs:
            async def stream(self, *a: Any, **k: Any) -> AsyncIterator[Any]:
                yield SimpleNamespace(
                    event="error", data={"error": "ValueError", "message": "db at 10.0.0.7 down"}
                )

        return SimpleNamespace(threads=None, runs=_Runs())

    monkeypatch.setitem(sys.modules, "langgraph_sdk", SimpleNamespace(get_client=failing))
    events = await _events(rt, OWNER, ChatRequest(message="x"), THREAD)
    assert [e for e, _ in events] == ["message.start", "error"]
    error = events[1][1]
    assert error["code"] == "run_failed" and error["error_id"] in error["message"]
    assert "10.0.0.7" not in repr(error) and "detail" not in error
    record = await rt.runs.get(events[0][1]["run_id"])
    assert record is not None and record.status == "error"


async def test_recursion_limit_error_part_maps_to_its_code(server, monkeypatch) -> None:
    rt, _sdk = server

    def failing(**_: Any):
        class _Runs:
            async def stream(self, *a: Any, **k: Any) -> AsyncIterator[Any]:
                yield SimpleNamespace(
                    event="error", data={"error": "GraphRecursionError", "message": "..."}
                )

        return SimpleNamespace(threads=None, runs=_Runs())

    monkeypatch.setitem(sys.modules, "langgraph_sdk", SimpleNamespace(get_client=failing))
    events = await _events(rt, OWNER, ChatRequest(message="x"), THREAD)
    assert events[-1][1]["code"] == "recursion_limit"


async def test_server_conflict_is_reported_as_thread_busy(server, monkeypatch) -> None:
    """A run started through the native API holds the thread: the server rejects ours."""
    rt, _sdk = server

    def busy(**_: Any):
        class _Runs:
            async def stream(self, *a: Any, **k: Any) -> AsyncIterator[Any]:
                raise sdk_error(errors.ConflictError, 409, "Thread is busy")
                yield  # pragma: no cover

        return SimpleNamespace(threads=None, runs=_Runs())

    monkeypatch.setitem(sys.modules, "langgraph_sdk", SimpleNamespace(get_client=busy))
    events = await _events(rt, OWNER, ChatRequest(message="x"), THREAD)
    assert events[-1][0] == "error" and events[-1][1]["code"] == "thread_busy"


async def test_second_run_on_a_busy_thread_is_refused_before_the_server(server) -> None:
    rt, sdk = server
    lease = await rt.acquire_thread(THREAD)
    events = await _events(rt, OWNER, ChatRequest(message="x", thread_id=THREAD), THREAD)
    assert events == [
        ("error", {"code": "thread_busy", "message": "This thread already has a run in progress."})
    ]
    assert sdk.streams == []
    await lease.release()
    events = await _events(rt, OWNER, ChatRequest(message="x", thread_id=THREAD), THREAD)
    assert events[-1][0] == "message.end"


async def test_timeout_cancels_the_server_run(server, monkeypatch) -> None:
    rt, sdk = server
    sdk.slow_stream = True
    monkeypatch.setenv("RUN_TIMEOUT_S", "0.2")
    events = await _events(rt, OWNER, ChatRequest(message="x", thread_id=THREAD), THREAD)
    assert events[-1][0] == "error" and events[-1][1]["code"] == "timeout"
    assert sdk.cancelled == [(THREAD, "srv-run-1")]
    record = await rt.runs.get(events[0][1]["run_id"])
    assert record is not None and record.status == "timeout"
    assert THREAD not in rt.locks.held
    assert sdk.state_updates == []  # every tool call of the thread has its result


async def test_a_stopped_run_gets_its_open_tool_calls_answered(server, monkeypatch) -> None:
    rt, sdk = server
    sdk.slow_stream = True
    sdk.messages = sdk.messages[:2]  # the assistant's tool call, no result yet
    monkeypatch.setenv("RUN_TIMEOUT_S", "0.2")
    await _events(rt, OWNER, ChatRequest(message="x", thread_id=THREAD), THREAD)
    # Answered before the run started, right after the call; nothing left for the end.
    (update,) = sdk.state_updates
    assert update["as_node"] == "tools"
    (patch,) = update["values"]["messages"]
    assert patch["tool_call_id"] == "c1" and patch["status"] == "error"
    assert "did not finish" in patch["content"]
    assert sdk.history_at_stream == [["human", "ai", "tool"]]


async def test_a_result_after_the_next_turn_is_moved_back_before_the_run(server) -> None:
    """The shape older versions left on the server: every later turn got a provider 400."""
    rt, sdk = server
    human, call = sdk.messages[:2]
    sdk.messages = [
        human,
        {**call, "usage_metadata": {"input_tokens": 1}, "invalid_tool_calls": []},
        {"type": "human", "id": "m9", "content": "hello?"},
        {"type": "tool", "id": "t1", "tool_call_id": "c1", "name": "get_weather", "content": "x"},
    ]
    events = await _events(rt, OWNER, ChatRequest(message="x", thread_id=THREAD), THREAD)
    assert events[-1][0] == "message.end"
    (update,) = sdk.state_updates
    rewritten = update["values"]["messages"]
    assert rewritten[0] == {"type": "remove", "id": "__remove_all__"}
    assert [m["id"] for m in rewritten[1:]] == ["m1", "m2", "t1", "m9"]
    assert "usage_metadata" not in rewritten[2]  # only keys the server reads back as-is
    assert sdk.history_at_stream == [["human", "ai", "tool", "human"]]


async def test_a_thread_busy_with_a_native_run_is_left_alone(server) -> None:
    """Its open tool call belongs to the run in progress: never answer it for that run."""
    rt, sdk = server
    sdk.messages = sdk.messages[:2]
    sdk.threads_by_id[THREAD]["status"] = "busy"
    await _events(rt, OWNER, ChatRequest(message="x", thread_id=THREAD), THREAD)
    assert sdk.state_updates == []


async def test_the_step_limit_ends_a_server_run_with_a_reply(server) -> None:
    rt, sdk = server
    sdk.stream_error = {"error": "GraphRecursionError", "message": "..."}
    events = await _events(rt, OWNER, ChatRequest(message="x", thread_id=THREAD), THREAD)
    assert [e for e, _ in events] == ["message.start", "message.delta", "message.end"]
    assert events[-1][1]["status"] == "step_limit"
    (update,) = sdk.state_updates
    assert update["as_node"] == "model"
    assert update["values"]["messages"][-1]["content"] == events[1][1]["text"]
    record = await rt.runs.get(events[0][1]["run_id"])
    assert record is not None and record.status == "step_limit"


async def test_list_and_delete_threads_through_the_server(server, monkeypatch) -> None:
    rt, sdk = server
    await rt.resolve_thread(STRANGER, ChatRequest(message="x", thread_id=OTHER))
    listed = await rt.list_threads(OWNER, limit=10, offset=0)
    assert [t["thread_id"] for t in listed] == [THREAD]
    assert sdk.searches[-1]["metadata"] == {"principal_id": "A"}
    assert sdk.searches[-1]["sort_by"] == "updated_at"
    monkeypatch.setenv("AUTH_READ_ACROSS_ROLES", "auditor")
    listed = await rt.list_threads(AUDITOR, limit=10, offset=0)
    assert {t["thread_id"] for t in listed} == {THREAD, OTHER}
    with pytest.raises(HTTPException) as exc:
        await rt.delete_thread(AUDITOR, THREAD)  # read-across is read-only
    assert exc.value.status_code == 403
    await _events(rt, OWNER, ChatRequest(message="x", thread_id=THREAD), THREAD)
    assert await rt.runs.list_for_thread(THREAD)
    await rt.delete_thread(OWNER, THREAD)
    assert sdk.deleted == [THREAD] and await rt.runs.list_for_thread(THREAD) == []
    with pytest.raises(HTTPException) as exc:
        await rt.delete_thread(OWNER, THREAD)
    assert exc.value.status_code == 404


async def test_retention_purges_idle_server_threads(server) -> None:
    rt, sdk = server
    fresh = "77777777-7777-7777-7777-777777777777"
    gone = "88888888-8888-8888-8888-888888888888"  # deleted through the native API
    sdk.threads_by_id[fresh] = {
        "thread_id": fresh,
        "metadata": {"principal_id": "A"},
        "updated_at": "2999-01-01T00:00:00+00:00",
    }
    old = "2000-01-01T00:00:00+00:00"
    for run_id, thread_id in (("r-fresh", fresh), ("r-gone", gone)):
        await rt.runs.record(
            RunRecord(
                run_id=run_id,
                thread_id=thread_id,
                principal_hash="h",
                model="m",
                status="ok",
                created_at=old,
            )
        )
    assert await rt.purge_expired(30) == 1
    assert sdk.deleted == [THREAD] and fresh in sdk.threads_by_id
    assert await rt.runs.get("r-gone") is None and await rt.runs.get("r-fresh") is not None


async def test_run_metadata_hides_the_raw_owner_id_the_server_merges_in(
    server, monkeypatch
) -> None:
    """What the server would store: thread metadata merged under the run's own metadata."""
    rt, sdk = server
    monkeypatch.setenv("PRINCIPAL_HASH_SALT", "pepper")
    email = Principal(id="alice@example.com")
    thread = "99999999-9999-9999-9999-999999999999"
    await rt.resolve_thread(email, ChatRequest(message="x", thread_id=thread))
    await _events(rt, email, ChatRequest(message="x", thread_id=thread), thread)
    thread_metadata = sdk.threads_by_id[thread]["metadata"]
    assert thread_metadata["principal_id"] == "alice@example.com"  # the ownership stamp
    merged = {**thread_metadata, **sdk.streams[-1]["metadata"]}
    assert "alice@example.com" not in repr(merged)
    assert merged["principal_id"] == email.hashed_id() == merged["principal_hash"]


async def test_retention_rechecks_idleness_under_the_lock_on_the_server(server) -> None:
    """A server thread continued after the purge listed it is kept."""
    rt, sdk = server
    idle_threads = rt._server_idle_threads

    async def list_then_resume(cutoff: Any, batch: int) -> list[str]:
        candidates = await idle_threads(cutoff, batch)
        assert THREAD in candidates
        sdk.threads_by_id[THREAD]["updated_at"] = "2999-01-01T00:00:00+00:00"
        return candidates

    rt._server_idle_threads = list_then_resume  # type: ignore[method-assign]
    assert await rt.purge_expired(30) == 0
    assert sdk.deleted == [] and THREAD in sdk.threads_by_id


async def test_orphaned_run_records_are_swept_page_by_page(server) -> None:
    """Old run records of live threads never hide those of deleted ones on later pages."""
    rt, sdk = server
    old = "2000-01-01T00:00:00+00:00"
    live = [f"00000000-0000-0000-0000-00000000000{i}" for i in range(5)]
    for thread_id in live:
        sdk.threads_by_id[thread_id] = {"thread_id": thread_id, "metadata": {"principal_id": "A"}}
    gone = "ffffffff-ffff-ffff-ffff-ffffffffffff"  # sorts after every live id
    for n, thread_id in enumerate([*live, gone]):
        await rt.runs.record(
            RunRecord(
                run_id=f"r{n}",
                thread_id=thread_id,
                principal_hash="h",
                model="m",
                status="ok",
                created_at=old,
            )
        )
    assert await rt.sweep_orphaned_runs(30, batch=2) == 1
    assert await rt.runs.list_for_thread(gone) == []
    assert all([await rt.runs.list_for_thread(t) for t in live])
    assert await rt.sweep_orphaned_runs(0) == 0  # RETENTION_DAYS=0 keeps everything


async def test_the_native_thread_delete_drops_the_run_records(server) -> None:
    """The server's own DELETE /threads/{id} (not the app's) removes the app's run records."""
    rt, _sdk = server
    from {{cookiecutter.agent_directory}}.app_utils.middleware import ThreadDeleteHookMiddleware

    for run_id, thread_id in (("r1", THREAD), ("r2", OTHER)):
        await rt.runs.record(
            RunRecord(
                run_id=run_id, thread_id=thread_id, principal_hash="h", model="m", status="ok"
            )
        )
    answers = {THREAD: 204, OTHER: 404}

    async def native_api(scope: Any, receive: Any, send: Any) -> None:
        status = answers.get(scope["path"].rsplit("/", 1)[-1].lower(), 404)
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = ThreadDeleteHookMiddleware(native_api, on_deleted=rt.forget_thread_runs)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://server"
    ) as client:
        assert (await client.get(f"/threads/{THREAD}")).status_code == 204  # not a delete
        assert await rt.runs.get("r1") is not None
        assert (await client.delete(f"/threads/{OTHER}")).status_code == 404  # refused
        assert await rt.runs.get("r2") is not None
        # Any spelling of the id the server accepts names the canonical records.
        assert (await client.delete(f"/threads/{THREAD.upper()}")).status_code == 204
    assert await rt.runs.get("r1") is None and await rt.runs.get("r2") is not None


def test_the_server_runtime_leaves_thread_deletion_to_the_native_api(monkeypatch) -> None:
    """A DELETE /threads/{id} route of the app would shadow the server's own (and its loopback)."""
    import importlib

    from {{cookiecutter.agent_directory}} import fast_api_app as module

    def routes(app: Any) -> set[tuple[str, str]]:
        return {(r.path, m) for r in app.routes for m in (getattr(r, "methods", None) or ())}

    assert ("/threads/{thread_id}", "DELETE") in routes(module.app)
    assert not {"AuthErrorMiddleware", "ThreadDeleteHookMiddleware"} & {
        m.cls.__name__ for m in module.app.user_middleware
    }
    monkeypatch.setenv("RUNTIME", "langgraph-server")
    try:
        server_app = importlib.reload(module).app
        server_routes = routes(server_app)
        assert ("/threads/{thread_id}", "DELETE") not in server_routes
        assert {("/threads", "GET"), ("/ready", "GET"), ("/chat", "POST")} <= server_routes
        # The native API's auth errors and thread deletes pass through the app.
        assert {"AuthErrorMiddleware", "ThreadDeleteHookMiddleware"} <= {
            m.cls.__name__ for m in server_app.user_middleware
        }
    finally:
        monkeypatch.setenv("RUNTIME", "fastapi")
        importlib.reload(module)


def test_persistence_kind_follows_database_uri(server, monkeypatch) -> None:
    rt, _sdk = server
    monkeypatch.setenv("DATABASE_URI", ":memory:")  # what `langgraph dev` sets
    assert rt.checkpointer_kind() == "memory" and not Database.for_server().is_postgres
    monkeypatch.setenv("DATABASE_URI", "postgresql://u:p@db:5432/agent")
    assert rt.checkpointer_kind() == "postgres"
    db = Database.for_server()
    assert db.is_postgres and db.runs_table == "agent_runs" and not db.with_threads


async def test_ready_checks_the_server(server, monkeypatch) -> None:
    rt, _sdk = server
    assert await rt.ready() is True

    def broken(**_: Any):
        class _Assistants:
            async def search(self, **_: Any):
                raise httpx.ConnectError("down")

        return SimpleNamespace(assistants=_Assistants())

    monkeypatch.setitem(sys.modules, "langgraph_sdk", SimpleNamespace(get_client=broken))
    assert await rt.ready() is False
