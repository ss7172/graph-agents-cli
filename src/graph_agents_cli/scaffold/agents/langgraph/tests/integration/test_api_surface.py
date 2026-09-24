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

"""The API surface's input rules, what it returns, and what it logs, in-process.

/chat and A2A share one message cap and refuse bad input with a 4xx (or
invalid params) instead of an internal error; failed tool calls reach clients
as an error id; thread listing is the caller's own unless asked otherwise;
deleting a thread drops its A2A tasks; the A2A reply is one text part. Several
principals come from a header test policy (`X-User`, `X-Roles`).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
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
from a2a.client import ClientConfig, create_client
from a2a.types import (
    GetTaskRequest,
    Message,
    Part,
    Role,
    SendMessageRequest,
    Task,
    TaskState,
)
from fastapi import HTTPException
from starlette.requests import Request

from {{cookiecutter.agent_directory}}.app_utils import a2a as a2a_module
from {{cookiecutter.agent_directory}}.app_utils import auth as auth_module
from {{cookiecutter.agent_directory}}.app_utils.api_client import ApiPolicyError
from {{cookiecutter.agent_directory}}.app_utils.auth import ACTIONS, Principal
from {{cookiecutter.agent_directory}}.app_utils.chat import LANGGRAPH_SERVER, RUNTIME
from {{cookiecutter.agent_directory}}.app_utils.content import TOOL_ERROR_MESSAGE, tool_error_id
from {{cookiecutter.agent_directory}}.fast_api_app import app

PROJECT = Path(__file__).resolve().parents[2]
A2A_PATH = "/a2a/{{cookiecutter.agent_directory}}"
A2A_URL = f"http://testserver{A2A_PATH}"
PER_TEST_VARS = (
    "MAX_MESSAGE_CHARS",
    "AUTH_READ_ACROSS_ROLES",
    "A2A_DESCRIPTION",
    "PRINCIPAL_HASH_SALT",
    "A2A_TASK_TTL_S",
)


class HeaderPolicy:
    """Test policy: `X-User` is the principal id, `X-Roles` its comma-separated roles."""

    async def authenticate(self, request: Request) -> Principal:
        user = request.headers.get("x-user")
        if not user:
            raise HTTPException(401, "no user", headers={"WWW-Authenticate": "Bearer"})
        roles = [r for r in (request.headers.get("x-roles") or "user").split(",") if r]
        return Principal(id=user, roles=roles, permissions=set(ACTIONS))

    async def authorize(self, principal: Principal, action: str, resource: str | None) -> None:
        return None


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    for name in PER_TEST_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(auth_module, "get_policy", lambda: HeaderPolicy())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", timeout=30
        ) as c:
            yield c


def _as(user: str, roles: str = "user") -> dict[str, str]:
    return {"X-User": user, "X-Roles": roles}


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
    client: httpx.AsyncClient, user: str, message: str, thread_id: str | None = None
) -> list[tuple[str, dict[str, Any]]]:
    body: dict[str, Any] = {"message": message}
    if thread_id:
        body["thread_id"] = thread_id
    r = await client.post("/chat", json=body, headers=_as(user))
    assert r.status_code == 200, r.text
    return parse_sse(r.text)


async def rpc(client: httpx.AsyncClient, user: str, method: str, params: dict) -> dict:
    r = await client.post(
        A2A_PATH,
        json={"jsonrpc": "2.0", "id": "1", "method": method, "params": params},
        headers={**_as(user), "A2A-Version": "1.0"},
    )
    return r.json()


def _message(text: str, **fields: Any) -> dict[str, Any]:
    return {
        "message": {
            "messageId": f"m-{uuid.uuid4()}",
            "role": "ROLE_USER",
            "parts": [{"text": text}],
            **fields,
        }
    }


# --- input: surrogates, the message cap, empty A2A messages ------------------------


async def test_a_lone_surrogate_is_a_422_that_logs_nothing(client, caplog) -> None:
    bodies = (
        b'{"message": "hello \\ud800 SURR-MARK-1"}',
        b'{"message": "hi", "metadata": {"note": "x \\udfff SURR-MARK-2"}}',
        b'{"message": "hi", "metadata": {"k \\ud800 SURR-MARK-3": "v"}}',
        b'{"message": "hi", "thread_id": "t\\ud800 SURR-MARK-4"}',
    )
    with caplog.at_level(logging.DEBUG):
        for body in bodies:
            r = await client.post(
                "/chat", content=body, headers={**_as("alice"), "Content-Type": "application/json"}
            )
            assert r.status_code == 422, r.text
            assert "SURR-MARK" not in r.text and '"input"' not in r.text
    assert "SURR-MARK" not in caplog.text
    assert not [rec for rec in caplog.records if rec.levelno >= logging.ERROR]


async def test_a_422_never_echoes_the_message_back(client) -> None:
    r = await client.post("/chat", json={"message": "x" * 40_000}, headers=_as("alice"))
    assert r.status_code == 422 and "MAX_MESSAGE_CHARS" in r.text
    assert len(r.content) < 1000


async def test_one_message_cap_for_chat_and_a2a(client, monkeypatch) -> None:
    monkeypatch.setenv("MAX_MESSAGE_CHARS", "12")
    r = await client.post("/chat", json={"message": "x" * 13}, headers=_as("alice"))
    assert r.status_code == 422
    too_long = await rpc(client, "alice", "SendMessage", _message("x" * 13))
    assert (
        too_long["error"]["code"] == -32602 and "MAX_MESSAGE_CHARS" in too_long["error"]["message"]
    )
    # Two parts count together (the executor joins them with a newline).
    two = _message("x" * 6)
    two["message"]["parts"].append({"text": "y" * 6})
    assert (await rpc(client, "alice", "SendMessage", two))["error"]["code"] == -32602
    assert "result" in await rpc(client, "alice", "SendMessage", _message("hello"))
    events = await chat(client, "alice", "x" * 12)
    assert events[-1][0] == "message.end"


@pytest.mark.parametrize(
    ("parts", "role"),
    [
        ([{"text": ""}], "ROLE_USER"),
        ([{"text": "hi"}, {"text": ""}], "ROLE_USER"),
        ([{"data": {"a": 1}}], "ROLE_USER"),
        ([{"text": "hi"}], "ROLE_AGENT"),
    ],
)
async def test_an_a2a_message_without_usable_text_is_invalid_params(
    client, caplog, parts: list[dict], role: str
) -> None:
    user = f"dave-{uuid.uuid4()}"
    params = {"message": {"messageId": "m-1", "role": role, "parts": parts}}
    with caplog.at_level(logging.WARNING):
        answer = await rpc(client, user, "SendMessage", params)
    assert answer["error"]["code"] == -32602, answer
    assert not [rec for rec in caplog.records if rec.levelno >= logging.ERROR]
    # Nothing was created for it.
    listed = await rpc(client, user, "ListTasks", {})
    assert not listed["result"].get("tasks")


async def test_the_streaming_method_checks_the_message_too(client) -> None:
    r = await client.post(
        A2A_PATH,
        json={
            "jsonrpc": "2.0",
            "id": "1",
            "method": "SendStreamingMessage",
            "params": _message(""),
        },
        headers={**_as("alice"), "A2A-Version": "1.0"},
    )
    assert "-32602" in r.text and "Traceback" not in r.text


@pytest.mark.parametrize("method", ["message/send", "message/stream"])
@pytest.mark.parametrize("text", ["", "x" * 40_000])
async def test_a2a_0_3_clients_get_invalid_params_too(
    client, caplog, method: str, text: str
) -> None:
    params = {
        "message": {
            "kind": "message",
            "messageId": "m-03",
            "role": "user",
            "parts": [{"kind": "text", "text": text}],
        }
    }
    with caplog.at_level(logging.WARNING):
        r = await client.post(
            A2A_PATH,
            json={"jsonrpc": "2.0", "id": 7, "method": method, "params": params},
            headers={**_as("alice"), "A2A-Version": "0.3"},
        )
    assert r.status_code == 200
    assert r.json() == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {"code": -32602, "message": r.json()["error"]["message"]},
    }
    assert not [rec for rec in caplog.records if rec.levelno >= logging.ERROR]
    # A valid 0.3 message still runs, its reply in one part.
    params["message"]["parts"] = [{"kind": "text", "text": "hello"}]
    r = await client.post(
        A2A_PATH,
        json={"jsonrpc": "2.0", "id": 8, "method": "message/send", "params": params},
        headers={**_as("alice"), "A2A-Version": "0.3"},
    )
    parts = [p["text"] for a in r.json()["result"]["artifacts"] for p in a["parts"]]
    assert parts == ["Hello! How can I help you today?"]


# --- the A2A reply -------------------------------------------------------------------


async def _a2a_client(user: str, *, streaming: bool) -> Any:
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers=_as(user),
        timeout=30,
    )
    return http, await create_client(A2A_URL, ClientConfig(streaming=streaming, httpx_client=http))


async def test_message_send_returns_the_reply_as_one_text_part(client) -> None:
    http, alice = await _a2a_client("alice", streaming=False)
    try:
        request = SendMessageRequest(
            message=Message(
                message_id="m-1",
                role=Role.ROLE_USER,
                parts=[Part(text="What is the weather in Paris?")],
            )
        )
        task: Task | None = None
        async for chunk in alice.send_message(request):
            if chunk.HasField("task"):
                task = chunk.task
        assert task is not None and task.status.state == TaskState.TASK_STATE_COMPLETED
        (artifact,) = task.artifacts
        (part,) = artifact.parts  # the whole reply, not one part per streamed token
        assert part.text.startswith("Here is what I found:") and "sunny" in part.text
    finally:
        await http.aclose()


async def test_a_streamed_reply_ends_with_last_chunk_and_is_stored_whole(client) -> None:
    http, alice = await _a2a_client("alice", streaming=True)
    try:
        request = SendMessageRequest(
            message=Message(
                message_id="m-2",
                role=Role.ROLE_USER,
                parts=[Part(text="What is the weather in Paris?")],
            )
        )
        updates = []
        async for chunk in alice.send_message(request):
            if chunk.HasField("artifact_update"):
                updates.append(chunk.artifact_update)
        assert len(updates) > 1
        assert [u.last_chunk for u in updates] == [False] * (len(updates) - 1) + [True]
        assert [u.append for u in updates] == [False] + [True] * (len(updates) - 1)
        text = "".join(p.text for u in updates for p in u.artifact.parts)
        assert text.startswith("Here is what I found:") and "sunny" in text
        stored = await alice.get_task(GetTaskRequest(id=updates[0].task_id))
        assert [[p.text for p in a.parts] for a in stored.artifacts] == [[text]]
    finally:
        await http.aclose()


def test_the_card_describes_the_agent_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("A2A_DESCRIPTION", "Answers questions about orders.")
    monkeypatch.setenv("AGENT_VERSION", "2.4.0")
    card = a2a_module.agent_card()
    assert card.description == "Answers questions about orders." and card.version == "2.4.0"
    assert [s.description for s in card.skills] == ["Answers questions about orders."]
    monkeypatch.delenv("A2A_DESCRIPTION")
    card = a2a_module.agent_card()
    assert card.description == a2a_module.DEFAULT_DESCRIPTION
    assert [s.description for s in card.skills] == [a2a_module.DEFAULT_SKILL_DESCRIPTION]


# --- threads: server-made ids, listing scope, delete drops A2A tasks -------------------


async def test_threads_started_without_an_id_get_random_server_ids(client) -> None:
    first = (await chat(client, "alice", "hello"))[0][1]["thread_id"]
    second = (await chat(client, "alice", "hello"))[0][1]["thread_id"]
    assert first != second
    assert uuid.UUID(first).version == 4 and uuid.UUID(second).version == 4


async def test_listing_is_the_callers_own_unless_a_read_across_role_asks_for_all(
    client, monkeypatch
) -> None:
    monkeypatch.setenv("AUTH_READ_ACROSS_ROLES", "support")
    alice_threads = {(await chat(client, "alice", "hello"))[0][1]["thread_id"] for _ in range(2)}
    carol_thread = (await chat(client, "carol", "hello"))[0][1]["thread_id"]
    alice_hash = Principal(id="alice").hashed_id()

    own = (await client.get("/threads", headers=_as("alice"))).json()
    assert {t["thread_id"] for t in own} == alice_threads
    assert {t["owner"] for t in own} == {alice_hash}

    # A read-across role lists its own threads by default...
    carol_own = (await client.get("/threads", headers=_as("carol", "support"))).json()
    assert [t["thread_id"] for t in carol_own] == [carol_thread]
    # ...and every principal's only when it asks, each row naming its owner hashed.
    everything = (await client.get("/threads?scope=all", headers=_as("carol", "support"))).json()
    owners = {t["thread_id"]: t["owner"] for t in everything}
    assert alice_threads <= set(owners) and carol_thread in owners
    assert {owners[t] for t in alice_threads} == {alice_hash}
    assert "alice" not in json.dumps(everything)

    r = await client.get("/threads?scope=all", headers=_as("bob"))
    assert r.status_code == 403 and "AUTH_READ_ACROSS_ROLES" in r.json()["detail"]
    assert (await client.get("/threads?scope=everyone", headers=_as("alice"))).status_code == 422


async def test_under_langgraph_server_the_listing_reads_the_thread_metadata(
    client, monkeypatch
) -> None:
    monkeypatch.setenv("AUTH_READ_ACROSS_ROLES", "support")
    searches: list[dict[str, Any]] = []

    class Threads:
        async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
            searches.append(kwargs)
            return [
                {
                    "thread_id": "11111111-1111-4111-8111-111111111111",
                    "metadata": {"principal_id": "alice"},
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-02T00:00:00+00:00",
                }
            ]

    monkeypatch.setattr(RUNTIME, "runtime", LANGGRAPH_SERVER)
    monkeypatch.setattr(RUNTIME, "_sdk_client", lambda headers: SimpleNamespace(threads=Threads()))
    own = (await client.get("/threads", headers=_as("carol", "support"))).json()
    assert searches[-1]["metadata"] == {"principal_id": "carol"}  # a read-across role: its own
    assert own[0]["owner"] == Principal(id="carol").hashed_id()
    everything = (await client.get("/threads?scope=all", headers=_as("carol", "support"))).json()
    assert "metadata" not in searches[-1]
    assert everything[0]["owner"] == Principal(id="alice").hashed_id()
    assert (await client.get("/threads?scope=all", headers=_as("bob"))).status_code == 403


async def test_deleting_a_thread_drops_its_a2a_tasks(client) -> None:
    context_id = f"ctx-{uuid.uuid4()}"
    sent = await rpc(
        client, "alice", "SendMessage", _message("my pin is 9876", contextId=context_id)
    )
    task_id = sent["result"]["task"]["id"]
    got = await rpc(client, "alice", "GetTask", {"id": task_id})
    assert "9876" in json.dumps(got)

    r = await client.delete(f"/threads/{context_id}", headers=_as("alice"))
    assert r.status_code == 204
    gone = await rpc(client, "alice", "GetTask", {"id": task_id})
    assert "error" in gone and "9876" not in json.dumps(gone)
    listed = await rpc(client, "alice", "ListTasks", {"contextId": context_id})
    assert not listed["result"].get("tasks")


async def test_the_server_delete_hook_drops_the_tasks_of_any_spelling_of_the_id(
    client, monkeypatch
) -> None:
    """langgraph-server: the server's own DELETE succeeded; the app drops the A2A tasks."""
    from {{cookiecutter.agent_directory}}.fast_api_app import _server_thread_deleted

    monkeypatch.setenv("RUNTIME", "langgraph-server")  # context ids compare as UUIDs
    thread_id = str(uuid.uuid4())
    sent = await rpc(client, "alice", "SendMessage", _message("hi", contextId=thread_id.upper()))
    task_id = sent["result"]["task"]["id"]
    await _server_thread_deleted(thread_id)
    assert "error" in await rpc(client, "alice", "GetTask", {"id": task_id})


async def test_the_retention_purge_drops_a2a_tasks_too(client) -> None:
    context_id = f"ctx-{uuid.uuid4()}"
    sent = await rpc(client, "alice", "SendMessage", _message("keep me", contextId=context_id))
    task_id = sent["result"]["task"]["id"]
    assert RUNTIME.threads is not None
    await RUNTIME.threads.delete(context_id)  # what the retention purge calls per thread
    assert "error" in await rpc(client, "alice", "GetTask", {"id": task_id})


# --- failed tool calls reach clients as an error id ---------------------------------------

DETAIL = (
    "orders: GET getOrder refused by the API policy: "
    "limits.max_calls_per_run (20) reached: this run already made 20 call(s) to this API"
)


@pytest.fixture
def refusing_weather(monkeypatch: pytest.MonkeyPatch) -> None:
    from {{cookiecutter.agent_directory}}.tools import weather

    def refused(query: str) -> str:
        raise ApiPolicyError(DETAIL)

    monkeypatch.setattr(weather.get_weather, "func", refused)


async def test_a_failed_tool_call_is_an_error_id_for_clients(
    client, monkeypatch, refusing_weather, caplog
) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    with caplog.at_level(logging.INFO):
        events = await chat(client, "alice", "What is the weather in Paris?")
    thread_id = events[0][1]["thread_id"]
    (result,) = [data for event, data in events if event == "tool.result"]
    error_id = tool_error_id(thread_id, result["id"])
    assert result["is_error"] is True and result["error_id"] == error_id
    assert result["result"] == f"{TOOL_ERROR_MESSAGE} Reference: {error_id}."
    warned = [r for r in caplog.records if "tool call failed" in r.getMessage()]
    assert warned and error_id in warned[0].getMessage()
    assert all(
        "max_calls_per_run" not in r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.INFO
    )

    messages = (await client.get(f"/threads/{thread_id}/messages", headers=_as("alice"))).json()
    (tool,) = [m for m in messages if m["role"] == "tool"]
    assert tool["is_error"] is True and tool["error_id"] == error_id
    assert tool["content"] == f"{TOOL_ERROR_MESSAGE} Reference: {error_id}."


async def test_under_dev_the_client_sees_the_tool_error_text(
    client, monkeypatch, refusing_weather
) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    events = await chat(client, "alice", "What is the weather in Paris?")
    (result,) = [data for event, data in events if event == "tool.result"]
    assert result["is_error"] is True and "max_calls_per_run" in result["result"]
    assert result["error_id"]


# --- .env is read before the app is built -------------------------------------------------


def test_env_file_settings_apply_to_what_the_app_fixes_at_import(tmp_path: Path) -> None:
    """The auth scheme the A2A card advertises, the docs switch and the card text come
    from `.env` even though they are fixed when the app module is imported."""
    shutil.copytree(PROJECT / "{{cookiecutter.agent_directory}}", tmp_path / "{{cookiecutter.agent_directory}}")
    (tmp_path / ".env").write_text(
        "APP_ENV=dev\n"
        "AUTH_POLICY=jwt\n"
        "AUTH_JWT_JWKS_URL=http://127.0.0.1:9/jwks\n"
        "AUTH_JWT_ISSUER=https://issuer.test\n"
        "AUTH_JWT_AUDIENCE=agent\n"
        "A2A_DESCRIPTION=Answers questions about orders.\n"
        "MODEL_PROVIDER=fake\n"
        "MODEL_NAME=fake\n"
        "APP_URL=http://testserver\n"
        "TRACING_ENABLED=false\n"
        "LOG_LEVEL=WARNING\n"
    )
    code = (
        "import {{cookiecutter.agent_directory}}.fast_api_app as f\n"
        "from {{cookiecutter.agent_directory}}.app_utils import a2a\n"
        "card = a2a.agent_card()\n"
        "print(card.description)\n"
        "print(card.security_schemes['bearer'].http_auth_security_scheme.bearer_format)\n"
        "print(f.app.docs_url)\n"
    )
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["Answers questions about orders.", "JWT", "/docs"]
