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
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from {{cookiecutter.agent_directory}}.app_utils.auth import Principal
from {{cookiecutter.agent_directory}}.app_utils.chat import LANGGRAPH_SERVER, ChatRequest, ChatRuntime

OWNER = Principal(id="A", roles=["viewer"])
STRANGER = Principal(id="B", roles=["viewer"])
AUDITOR = Principal(id="C", roles=["auditor"])
THREAD = "11111111-1111-1111-1111-111111111111"


def _http_error(status: int, text: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://loopback/threads/x")
    response = httpx.Response(status, request=request, text=text)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    raise AssertionError("unreachable")


class FakeSdk:
    """`langgraph_sdk.get_client` stand-in: one thread owned by A, recording every call."""

    def __init__(self) -> None:
        self.headers: list[dict[str, str] | None] = []
        self.created: list[dict[str, Any]] = []
        self.streams: list[dict[str, Any]] = []
        self.threads_by_id: dict[str, dict[str, Any]] = {
            THREAD: {"thread_id": THREAD, "metadata": {"principal_id": "A", "tenant": None}}
        }
        self.messages = [
            {"type": "human", "id": "m1", "content": "hello"},
            {
                "type": "ai",
                "id": "m2",
                "content": "",
                "tool_calls": [{"id": "c1", "name": "get_weather", "args": {"query": "SF"}}],
            },
            {
                "type": "tool",
                "id": "m3",
                "tool_call_id": "c1",
                "name": "get_weather",
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
                    raise _http_error(404, '{"detail": "Thread not found"}')
                return sdk.threads_by_id[thread_id]

            async def create(self, *, thread_id: str | None = None, metadata: Any = None, **_: Any):
                thread_id = thread_id or "22222222-2222-2222-2222-222222222222"
                record = {"thread_id": thread_id, "metadata": dict(metadata or {})}
                sdk.threads_by_id[thread_id] = record
                sdk.created.append(record)
                return record

            async def get_state(self, thread_id: str, **_: Any) -> dict[str, Any]:
                if thread_id not in sdk.threads_by_id:
                    raise _http_error(404, '{"detail": "Thread not found"}')
                return {"values": {"messages": sdk.messages}}

        class _Runs:
            async def stream(
                self, thread_id: str, assistant_id: str, **kwargs: Any
            ) -> AsyncIterator[Any]:
                sdk.streams.append({"thread_id": thread_id, "assistant_id": assistant_id, **kwargs})
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
                                        {"id": "c1", "name": "get_weather", "args": {"query": "SF"}}
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
                                    "name": "get_weather",
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

        return SimpleNamespace(threads=_Threads(), runs=_Runs())


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> tuple[ChatRuntime, FakeSdk]:
    sdk = FakeSdk()
    monkeypatch.setitem(sys.modules, "langgraph_sdk", SimpleNamespace(get_client=sdk.get_client))
    monkeypatch.setenv("RUNTIME", "langgraph-server")
    monkeypatch.delenv("AUTH_READ_ACROSS_ROLES", raising=False)
    monkeypatch.delenv("TRACE_CAPTURE", raising=False)
    rt = ChatRuntime()
    assert rt.runtime == LANGGRAPH_SERVER
    return rt, sdk


async def test_owner_continues_its_thread_and_the_stream_maps_the_contract_events(server) -> None:
    rt, sdk = server
    req = ChatRequest(
        message="weather?",
        thread_id=THREAD,
        forward_headers={"authorization": "Bearer k", "x-request-id": "ignored"},
    )
    assert await rt.resolve_thread(OWNER, req) == THREAD
    events = [(e, d) async for e, d in rt.stream(OWNER, req, THREAD)]
    names = [e for e, _ in events]
    assert names == [
        "message.start",
        "tool.call",
        "tool.result",
        "message.delta",
        "message.delta",
        "message.end",
    ]
    assert events[1][1] == {"id": "c1", "name": "get_weather", "args": {"query": "SF"}}
    end = events[-1][1]
    assert end["usage"] == {"input_tokens": 3, "output_tokens": 4} and end["status"] == "ok"
    assert end["thread_id"] == THREAD and end["run_id"] == events[0][1]["run_id"]
    (stream,) = sdk.streams
    assert stream["assistant_id"] == "agent" and stream["stream_mode"] == [
        "messages-tuple",
        "updates",
    ]
    assert stream["context"]["principal_id"] == "A" and stream["metadata"]["principal_id"] == "A"
    # Only the credential headers are forwarded to the SDK client.
    assert sdk.headers[0] == {"authorization": "Bearer k"}


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
    assert messages[1]["tool_calls"] == [{"id": "c1", "name": "get_weather"}]  # args omitted
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
    rt, sdk = server
    new_id = "33333333-3333-3333-3333-333333333333"
    with pytest.raises(HTTPException) as exc:
        await rt.messages(STRANGER, new_id)
    assert exc.value.status_code == 404
    assert await rt.resolve_thread(STRANGER, ChatRequest(message="x", thread_id=new_id)) == new_id
    assert sdk.created == [{"thread_id": new_id, "metadata": {"principal_id": "B", "tenant": None}}]
    # A thread the server holds without ownership metadata fails closed.
    sdk.threads_by_id["bare"] = {"thread_id": "bare", "metadata": {}}
    with pytest.raises(HTTPException) as exc:
        await rt.resolve_thread(OWNER, ChatRequest(message="x", thread_id="bare"))
    assert exc.value.status_code == 403


async def test_server_failure_is_503_not_403(server, monkeypatch) -> None:
    rt, _sdk = server

    def broken(**_: Any):
        class _Threads:
            async def get(self, thread_id: str, **_: Any):
                raise httpx.ConnectError("loopback down")

        return SimpleNamespace(threads=_Threads(), runs=None)

    monkeypatch.setitem(sys.modules, "langgraph_sdk", SimpleNamespace(get_client=broken))
    with pytest.raises(HTTPException) as exc:
        await rt.resolve_thread(OWNER, ChatRequest(message="x", thread_id=THREAD))
    assert exc.value.status_code == 503


async def test_stream_error_part_becomes_an_error_event(server, monkeypatch) -> None:
    rt, _sdk = server

    def failing(**_: Any):
        class _Runs:
            async def stream(self, *a: Any, **k: Any) -> AsyncIterator[Any]:
                yield SimpleNamespace(event="error", data={"message": "model down"})

        return SimpleNamespace(threads=None, runs=_Runs())

    monkeypatch.setitem(sys.modules, "langgraph_sdk", SimpleNamespace(get_client=failing))
    events = [(e, d) async for e, d in rt.stream(OWNER, ChatRequest(message="x"), THREAD)]
    assert [e for e, _ in events] == ["message.start", "error"]
    assert events[1][1]["code"] == "RuntimeError" and "model down" in events[1][1]["message"]
