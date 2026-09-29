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

"""Structured final answers under the langgraph-server runtime, against a real LangGraph dev server.

The server runs this app, its auth handler and the graph of `approval_graph.py`
as `test_approvals_server.py` does, with `RESPONSE_SCHEMA_PATH` set, once per
strategy. The answer arrives through the server's own stream (JSON updates,
and its `error` part when no try fits): `/chat` ends with it in `message.end`
after one `message.delta` of its JSON text, a run that never fits ends with
`invalid_structured_response`, a run paused for an approval answers once
resumed, and the A2A reply holds a data part with it. Skipped when the
LangGraph CLI and its in-memory server are not installed.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_approvals_server import (
    BODY,
    POLICY,
    Upstream,
    _free_port,
    _server_env,
    _start_dev,
    _stop,
    _write_config,
    parse_sse,
    token,
)

LANGGRAPH = Path(sys.executable).with_name("langgraph")
pytestmark = pytest.mark.skipif(
    not LANGGRAPH.exists() or importlib.util.find_spec("langgraph_api") is None,
    reason="needs the LangGraph CLI with its in-memory server (langgraph-cli[inmem])",
)
A2A_PATH = "/a2a/{{cookiecutter.agent_directory}}"
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # A request that says FAILME never fits (the fake's reply repeats it).
        "answer": {"type": "string", "pattern": "^(?![\\s\\S]*FAILME)"},
        "done": {"type": "boolean"},
    },
    "required": ["answer", "done"],
}


@pytest.fixture(scope="module", params=["tool", "provider"])
def server(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Any]:
    work = tmp_path_factory.mktemp(f"structured-server-{request.param}")
    upstream = Upstream()
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    body_file = work / "body.json"
    body_file.write_text(json.dumps(BODY), encoding="utf-8")
    policy = work / "api-policy.yaml"
    policy.write_text(POLICY, encoding="utf-8")
    schema = work / "response_schema.json"
    schema.write_text(json.dumps(SCHEMA), encoding="utf-8")
    _write_config(work)
    env = {
        **_server_env(policy, body_file, upstream),
        "RESPONSE_SCHEMA_PATH": str(schema),
        "RESPONSE_FORMAT_STRATEGY": request.param,
    }
    port = _free_port()
    proc = None
    try:
        proc = _start_dev(work, env, port, work / "server.log")
        yield f"http://127.0.0.1:{port}", upstream
    finally:
        if proc is not None:
            _stop(proc)
        upstream.shutdown()
        upstream.server_close()


def _chat(url: str, message: str, user: str = "alice") -> list[tuple[str, dict[str, Any]]]:
    r = httpx.post(f"{url}/chat", json={"message": message}, headers=token(user), timeout=60)
    assert r.status_code == 200, r.text
    return parse_sse(r.text)


def _answered(events: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    names = [e for e, _ in events]
    assert names[-2:] == ["message.delta", "message.end"] and names.count("message.delta") == 1
    end = events[-1][1]
    assert end["status"] == "ok", end
    assert json.loads(events[-2][1]["text"]) == end["structured_response"]
    assert "final_answer" not in {d.get("name") for e, d in events if e.startswith("tool.")}
    return end["structured_response"]


def test_the_server_run_ends_with_the_answer(server: Any) -> None:
    url, _ = server
    assert _answered(_chat(url, "hello")) == {
        "answer": "Hello! How can I help you today?",
        "done": False,
    }


def test_an_answer_that_never_fits_fails_the_server_run(server: Any) -> None:
    url, _ = server
    events = _chat(url, "FAILME please")
    assert [e for e, _ in events] == ["message.start", "error"], events
    assert events[-1][1]["code"] == "invalid_structured_response", events


def test_a_server_run_paused_for_approval_answers_once_resumed(server: Any) -> None:
    url, upstream = server
    order = uuid.uuid4().hex[:6]
    paused = _chat(url, f"Cancel the order for {order}")
    end = paused[-1][1]
    assert end["status"] == "awaiting_approval" and "structured_response" not in end, paused
    r = httpx.post(
        f"{url}/threads/{end['thread_id']}/approvals/{end['approval']['approval_id']}",
        json={"decision": "approve"},
        headers=token("alice"),
        timeout=60,
    )
    assert r.status_code == 200, r.text
    answer = _answered(parse_sse(r.text))
    assert f"/orders/{order}/cancel" in answer["answer"]
    assert len(upstream.sent_to(f"/orders/{order}/cancel")) == 1


def test_the_server_a2a_reply_holds_the_answer_as_data(server: Any) -> None:
    url, _ = server
    r = httpx.post(
        f"{url}{A2A_PATH}",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": uuid.uuid4().hex,
                    "role": "ROLE_USER",
                    "parts": [{"text": "hello"}],
                }
            },
        },
        headers={**token("alice"), "A2A-Version": "1.0"},
        timeout=60,
    )
    assert r.status_code == 200, r.text
    task = r.json()["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED", task
    (artifact,) = task["artifacts"]
    text, data = artifact["parts"]
    assert data["mediaType"] == "application/json"
    assert (
        json.loads(text["text"])
        == data["data"]
        == {
            "answer": "Hello! How can I help you today?",
            "done": False,
        }
    )
