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

"""SSE parsing and the chat client against the fake contract server."""

from __future__ import annotations

import json

import httpx
import pytest

from graph_agents_cli import _chat_client
from graph_agents_cli._chat_client import (
    ChatHTTPError,
    SseEvent,
    _iter_stream_lines,
    get_health,
    get_thread_messages,
    iter_sse,
    post_chat,
)

from .conftest import contract_sequence

# ---------------------------------------------------------------------------
# iter_sse
# ---------------------------------------------------------------------------


def test_iter_sse_parses_event_data_pairs():
    lines = [
        "event: message.start",
        'data: {"thread_id": "t", "run_id": "r"}',
        "",
        "event: message.delta",
        'data: {"text": "hi"}',
        "",
    ]
    events = list(iter_sse(lines))
    assert events == [
        SseEvent(
            "message.start", {"thread_id": "t", "run_id": "r"}, '{"thread_id": "t", "run_id": "r"}'
        ),
        SseEvent("message.delta", {"text": "hi"}, '{"text": "hi"}'),
    ]


def test_iter_sse_joins_multi_line_data_and_skips_comments_and_keepalives():
    lines = [
        ": keep-alive",
        "",
        "event: message.delta",
        "data: {",
        'data:   "text": "two lines"',
        "data: }",
        ": another comment",
        "",
    ]
    events = list(iter_sse(lines))
    assert len(events) == 1
    assert events[0].event == "message.delta"
    assert events[0].data == {"text": "two lines"}
    assert events[0].raw == '{\n  "text": "two lines"\n}'


def test_iter_sse_accepts_crlf_and_missing_trailing_blank_line():
    lines = [
        "event: message.end\r\n",
        'data: {"status": "ok"}\r\n',
        "\r\n",
        "event: error",
        'data: {"code": "x", "message": "boom"}',
    ]
    events = list(iter_sse(lines))
    assert [e.event for e in events] == ["message.end", "error"]
    assert events[1].data == {"code": "x", "message": "boom"}


def test_iter_sse_defaults_event_name_keeps_id_and_non_json_data():
    lines = [
        "id: 7",
        "data: plain text",
        "",
        "event: only-name-no-data",
        "",
        "retry: 5",
        "data: x",
        "",
    ]
    events = list(iter_sse(lines))
    assert events[0] == SseEvent("message", "plain text", "plain text", "7")
    # An event without data is dropped, as the event-stream spec requires.
    assert [e.event for e in events] == ["message", "message"]
    assert events[1].data == "x"


def test_iter_sse_strips_single_leading_space_only():
    events = list(iter_sse(["data:  two spaces", ""]))
    assert events[0].data == " two spaces"


# ---------------------------------------------------------------------------
# post_chat / get_health / get_thread_messages against the fake server
# ---------------------------------------------------------------------------


def test_post_chat_streams_contract_sequence(chat_server):
    events = list(post_chat(chat_server.url, "hi", headers={"Authorization": "Bearer k"}))
    assert [e.event for e in events] == [
        "message.start",
        "message.delta",
        "tool.call",
        "tool.result",
        "message.delta",
        "message.end",
    ]
    assert events[0].data == {"thread_id": "thread-1", "run_id": "run-1"}
    assert events[-1].data["usage"] == {"input_tokens": 10, "output_tokens": 5}

    (req,) = chat_server.chat_requests
    assert req["headers"]["accept"] == "text/event-stream"
    assert req["headers"]["authorization"] == "Bearer k"
    assert req["headers"]["content-type"].startswith("application/json")
    assert req["body"] == {"message": "hi", "metadata": {}}


def test_post_chat_sends_thread_id_and_metadata(chat_server):
    list(post_chat(chat_server.url + "/", "again", thread_id="thread-9", metadata={"k": "v"}))
    (req,) = chat_server.chat_requests
    assert req["body"] == {"message": "again", "metadata": {"k": "v"}, "thread_id": "thread-9"}


def test_post_chat_raises_chat_http_error_with_body(chat_server):
    chat_server.status = 401
    chat_server.error_body = json.dumps({"detail": "bad key"})
    with pytest.raises(ChatHTTPError) as excinfo:
        list(post_chat(chat_server.url, "hi"))
    assert excinfo.value.status_code == 401
    assert "bad key" in excinfo.value.body
    assert excinfo.value.url.endswith("/chat")


def test_post_chat_handles_raw_keepalives_and_error_event(chat_server):
    chat_server.raw_body = (
        b": ping\n\n"
        b'event: message.start\r\ndata: {"thread_id": "t", "run_id": "r"}\r\n\r\n'
        b'event: error\ndata: {"code": "upstream",\ndata:  "message": "model down"}\n\n'
    )
    events = list(post_chat(chat_server.url, "hi"))
    assert [e.event for e in events] == ["message.start", "error"]
    assert events[1].data == {"code": "upstream", "message": "model down"}


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 100])
def test_iter_stream_lines_splits_only_on_cr_lf_crlf(size: int):
    text = "data: x\r\ndata: y\rdata: z\n\ndata: tail"
    chunks = [text[i : i + size] for i in range(0, len(text), size)]
    assert list(_iter_stream_lines(chunks)) == ["data: x", "data: y", "data: z", "", "data: tail"]


def test_post_chat_keeps_unicode_line_separators_inside_a_payload(chat_server):
    """httpx.iter_lines() would split on U+2028/U+2029/U+0085 (str.splitlines semantics)."""
    payload = {"text": "a\u2028b\u2029c\u0085d\x0be"}
    chat_server.raw_body = (
        "event: message.delta\ndata: "
        + json.dumps(payload, ensure_ascii=False)
        + '\n\nevent: message.end\ndata: {"status": "ok"}\n\n'
    ).encode("utf-8")
    events = list(post_chat(chat_server.url, "hi"))
    assert [e.event for e in events] == ["message.delta", "message.end"]
    assert events[0].data == payload


def test_post_chat_uses_the_stream_timeout_by_default(monkeypatch):
    seen: list = []

    class _Client:
        def __init__(self, *, timeout):
            seen.append(timeout)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, *a, **k):
            raise RuntimeError("stop here")

    monkeypatch.setattr(_chat_client.httpx, "Client", _Client)
    with pytest.raises(RuntimeError):
        list(post_chat("http://x", "hi"))
    assert seen == [_chat_client.STREAM_TIMEOUT]
    assert isinstance(_chat_client.STREAM_TIMEOUT, httpx.Timeout)
    assert _chat_client.STREAM_TIMEOUT.connect == 10.0 and _chat_client.STREAM_TIMEOUT.read >= 300


def test_get_health_and_thread_messages(chat_server):
    assert get_health(chat_server.url) == {
        "status": "ok",
        "runtime": "fastapi",
        "checkpointer": "memory",
    }
    chat_server.threads["t1"] = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "yo"},
    ]
    assert get_thread_messages(chat_server.url, "t1") == chat_server.threads["t1"]
    with pytest.raises(ChatHTTPError) as excinfo:
        get_thread_messages(chat_server.url, "missing")
    assert excinfo.value.status_code == 404


def test_delete_thread_deletes_the_owners_thread_and_its_approvals(chat_server):
    chat_server.book.pause("t 1/x", "alice")
    alice = {"Authorization": "Bearer alice-token"}
    with pytest.raises(ChatHTTPError) as excinfo:  # not the owner's
        _chat_client.delete_thread(
            chat_server.url, "t 1/x", headers={"Authorization": "Bearer bob-token"}
        )
    assert excinfo.value.status_code == 404
    assert chat_server.book.approvals
    _chat_client.delete_thread(chat_server.url, "t 1/x", headers=alice)
    assert chat_server.book.approvals == {}
    # The thread id is one path segment, whatever it holds.
    assert chat_server.requests[-1]["path"] == "/threads/t%201%2Fx"
    with pytest.raises(ChatHTTPError):
        _chat_client.delete_thread(chat_server.url, "t 1/x", headers=alice)


def test_contract_sequence_helper_is_complete():
    names = [e for e, _ in contract_sequence()]
    assert names[0] == "message.start" and names[-1] == "message.end"
    assert "tool.call" in names and "tool.result" in names
