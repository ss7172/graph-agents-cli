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

"""Failures against a real Postgres: lost replicas, dropped sessions, outages, crashes.

Opt-in: set `TEST_POSTGRES_DSN` to the URL of a server where the user may
create databases (each test gets a fresh one, dropped afterwards). Outages are
made with a TCP proxy in front of that server, which the tests stop, restart
and cut; crashes with a real server process killed with SIGKILL.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

os.environ.update(
    {
        "MODEL_PROVIDER": "fake",
        "MODEL_NAME": "fake",
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

from {{cookiecutter.agent_directory}}.app_utils import chat as chat_module
from {{cookiecutter.agent_directory}}.app_utils import run_locks
from {{cookiecutter.agent_directory}}.app_utils.checkpointer import POSTGRES
from {{cookiecutter.agent_directory}}.app_utils.db import Database, RunRecord, RunStore
from {{cookiecutter.agent_directory}}.app_utils.threads import LeaseLost, ThreadBusy, ThreadLocks

ADMIN_DSN = os.environ.get("TEST_POSTGRES_DSN", "")
pytestmark = pytest.mark.skipif(not ADMIN_DSN, reason="TEST_POSTGRES_DSN is not set")
AUTH = {"Authorization": "Bearer test-key"}
PROJECT = Path(__file__).resolve().parents[2]
AGENT_DIR = "{{cookiecutter.agent_directory}}"
# Short leases, so a lost replica is noticed in seconds.
FAST_LEASES = {"ttl_s": 2.0, "renew_every_s": 0.2, "validity_s": 1.0}


def _with_database(dsn: str, name: str) -> str:
    return urlsplit(dsn)._replace(path=f"/{name}").geturl()


def _with_port(dsn: str, port: int) -> str:
    parts = urlsplit(dsn)
    userinfo = parts.netloc.rsplit("@", 1)[0] + "@" if "@" in parts.netloc else ""
    return parts._replace(netloc=f"{userinfo}127.0.0.1:{port}").geturl()


@pytest.fixture
async def dsn() -> AsyncIterator[str]:
    import psycopg

    name = f"gac_test_{uuid.uuid4().hex[:12]}"
    async with await psycopg.AsyncConnection.connect(ADMIN_DSN, autocommit=True) as admin:
        await admin.execute(f'CREATE DATABASE "{name}"')
    yield _with_database(ADMIN_DSN, name)
    async with await psycopg.AsyncConnection.connect(ADMIN_DSN, autocommit=True) as admin:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


async def _schema(dsn: str) -> None:
    db = Database(POSTGRES, dsn)
    await db.open()
    await db.close()


async def _sql(dsn: str, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    import psycopg

    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        cur = await conn.execute(sql, params)
        return list(await cur.fetchall()) if cur.description else []


async def _acquire_within(locks: ThreadLocks, thread_id: str, seconds: float) -> float:
    """Seconds until `locks` gets the thread (fails after `seconds`)."""
    start = time.monotonic()
    while True:
        try:
            lease = await locks.acquire(thread_id)
        except ThreadBusy:
            if time.monotonic() - start > seconds:
                raise
            await asyncio.sleep(0.1)
            continue
        await lease.release()
        return time.monotonic() - start


# --- run leases ----------------------------------------------------------------------------


async def test_a_replica_lost_without_closing_its_connection_frees_its_threads(
    dsn: str,
) -> None:
    """A frozen or partitioned replica: its session stays open, it just stops renewing."""
    await _schema(dsn)
    lost_replica, other = ThreadLocks(dsn, **FAST_LEASES), ThreadLocks(dsn, **FAST_LEASES)
    try:
        lease = await lost_replica.acquire("t1")
        stopped: list[str] = []
        lease.on_lost(lambda: stopped.append("stopped"))
        with pytest.raises(ThreadBusy):
            await other.acquire("t1")
        heartbeat = lost_replica._task
        assert heartbeat is not None
        heartbeat.cancel()  # frozen: no renewals, the connection stays open
        waited = await _acquire_within(other, "t1", 10)
        assert 1.0 < waited < 6.0  # the 2 s lease, not the OS's 2 h TCP keepalive
        # The lost replica may no longer write the thread, and knows it.
        with pytest.raises(LeaseLost):
            lost_replica.fence("t1")
        assert stopped == ["stopped"] and lease.lost
        # Back from the partition: its release does not free what it no longer holds.
        taken = await other.acquire("t1")
        await lease.release()
        third = ThreadLocks(dsn, **FAST_LEASES)
        try:
            with pytest.raises(ThreadBusy):
                await third.acquire("t1")
        finally:
            await third.close()
        await taken.release()
    finally:
        await lost_replica.close()
        await other.close()


async def test_replicas_racing_for_a_thread_get_it_once(dsn: str) -> None:
    await _schema(dsn)
    replicas = [ThreadLocks(dsn, **FAST_LEASES) for _ in range(8)]
    try:
        results = await asyncio.gather(*(r.acquire("t1") for r in replicas), return_exceptions=True)
        won = [r for r in results if not isinstance(r, BaseException)]
        assert len(won) == 1
        assert all(isinstance(r, ThreadBusy) for r in results if r not in won)
        tokens = await _sql(dsn, "SELECT token FROM thread_locks WHERE thread_id = 't1'")
        assert tokens == [(won[0].token,)]
        await won[0].release()
        # The next holder gets a new fencing token.
        again = await replicas[3].acquire("t1")
        assert again.token is not None and again.token > won[0].token
        await again.release()
    finally:
        for r in replicas:
            await r.close()


async def test_a_dropped_session_keeps_the_lease_and_its_run(dsn: str) -> None:
    """What a Postgres restart or failover, an admin kill or a proxy reset does to a session."""
    await _schema(dsn)
    holder, other = ThreadLocks(dsn, **FAST_LEASES), ThreadLocks(dsn, **FAST_LEASES)
    try:
        lease = await holder.acquire("t1")
        rows = await _sql(
            dsn,
            "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid()",
        )
        assert rows[0][0] >= 1
        with pytest.raises(ThreadBusy):  # the old advisory lock was gone at this point
            await other.acquire("t1")
        await asyncio.sleep(FAST_LEASES["validity_s"] * 2)  # renewals reconnect
        holder.fence("t1")
        assert not lease.lost
        with pytest.raises(ThreadBusy):
            await other.acquire("t1")
        await lease.release()
        assert await _acquire_within(other, "t1", 2) < 1.0
    finally:
        await holder.close()
        await other.close()


async def test_a_lease_whose_release_failed_is_released_in_the_background(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _schema(dsn)
    holder, other = ThreadLocks(dsn, **FAST_LEASES), ThreadLocks(dsn, **FAST_LEASES)
    try:
        execute = holder._execute
        fail_release = {"t1", "t2"}  # the first release of each fails

        async def flaky(sql: str, params: dict[str, Any], **kwargs: Any) -> Any:
            if sql.startswith("DELETE") and params.get("thread") in fail_release:
                fail_release.discard(params["thread"])
                raise OSError("connection refused")
            return await execute(sql, params, **kwargs)

        monkeypatch.setattr(holder, "_execute", flaky)
        lease = await holder.acquire("t1")
        await lease.release()
        assert "t1" in holder._unreleased and "t1" not in holder.held
        # Every replica gets it once the background retry went through...
        assert await _acquire_within(other, "t1", 5) < 4
        # ...and this process takes a leftover of its own back at once.
        lease = await holder.acquire("t2")
        await lease.release()
        assert "t2" in holder._unreleased
        again = await holder.acquire("t2")
        assert "t2" not in holder._unreleased
        await again.release()
        assert await _acquire_within(other, "t2", 2) < 1
    finally:
        await holder.close()
        await other.close()


async def test_runs_of_dead_processes_are_reconciled_as_interrupted(dsn: str) -> None:
    await _schema(dsn)
    db = Database(POSTGRES, dsn)
    await db.open()
    locks = ThreadLocks(dsn, **FAST_LEASES)
    try:
        runs = RunStore(db)
        old = "2000-01-01T00:00:00+00:00"
        for run_id, thread_id in (("dead", "t-dead"), ("alive", "t-alive")):
            await runs.start(
                RunRecord(
                    run_id=run_id,
                    thread_id=thread_id,
                    principal_hash="h",
                    model="m",
                    status="ok",
                    created_at=old,
                )
            )
        lease = await locks.acquire("t-alive")  # a run still holds its thread
        replicas = await asyncio.gather(runs.reconcile(0), runs.reconcile(0))
        assert sorted(replicas, key=len) == [[], ["dead"]]  # closed once across replicas
        dead, alive = await runs.get("dead"), await runs.get("alive")
        assert dead is not None and dead.status == "interrupted"
        assert dead.error_type == "ProcessLost"
        assert alive is not None and alive.status == "running"
        await lease.release()
        assert await runs.reconcile(0) == ["alive"]
    finally:
        await locks.close()
        await db.close()


# --- a TCP proxy that can take the database away ---------------------------------------------


class Proxy:
    """Forwards 127.0.0.1:<port> to the test server; `down()` refuses and cuts every connection."""

    def __init__(self, upstream: str) -> None:
        parts = urlsplit(upstream)
        self.host, self.upstream_port = parts.hostname or "127.0.0.1", parts.port or 5432
        self.port = 0
        self.server: asyncio.base_events.Server | None = None
        self.writers: set[asyncio.StreamWriter] = set()

    async def up(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        self.port = self.server.sockets[0].getsockname()[1]

    async def down(self) -> None:
        server, self.server = self.server, None
        if server is not None:
            server.close()  # refuse new connections
        for writer in list(self.writers):  # cut the open ones
            writer.close()
        self.writers.clear()
        if server is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(server.wait_closed(), 5)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            up_reader, up_writer = await asyncio.open_connection(self.host, self.upstream_port)
        except OSError:
            writer.close()
            return
        self.writers.update((writer, up_writer))

        async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            with contextlib.suppress(Exception):
                while data := await src.read(65536):
                    dst.write(data)
                    await dst.drain()
            dst.close()

        await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))
        self.writers.difference_update((writer, up_writer))


@pytest.fixture
async def proxy(dsn: str) -> AsyncIterator[Proxy]:
    p = Proxy(dsn)
    await p.up()
    yield p
    await p.down()


@pytest.fixture
def postgres_app(dsn: str, proxy: Proxy, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, str]:
    """The app configured for Postgres through the proxy (enter its lifespan in the test)."""
    from {{cookiecutter.agent_directory}}.fast_api_app import app

    for name in ("RETENTION_DAYS", "RECURSION_LIMIT", "RUN_TIMEOUT_S", "SSE_HEARTBEAT_S"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CHECKPOINTER", "postgres")
    monkeypatch.setenv("POSTGRES_DSN", _with_port(dsn, proxy.port))
    monkeypatch.setattr(run_locks, "LEASE_TTL_S", FAST_LEASES["ttl_s"])
    monkeypatch.setattr(run_locks, "RENEW_EVERY_S", FAST_LEASES["renew_every_s"])
    monkeypatch.setattr(run_locks, "LOCAL_VALIDITY_S", FAST_LEASES["validity_s"])
    monkeypatch.setattr(chat_module, "INIT_RETRY_MAX_S", 0.5)
    monkeypatch.setattr(chat_module, "RECONCILE_INTERVAL_S", 0.5)
    return app, dsn


def _client(app: Any) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver", timeout=30)


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


async def _ready_within(client: httpx.AsyncClient, seconds: float) -> float:
    start = time.monotonic()
    while (await client.get("/ready")).status_code != 200:
        assert time.monotonic() - start < seconds, "not ready in time"
        await asyncio.sleep(0.2)
    return time.monotonic() - start


async def test_the_app_starts_while_the_database_is_down_and_gets_ready_after_it(
    postgres_app: tuple[Any, str], proxy: Proxy
) -> None:
    app, _dsn = postgres_app
    await proxy.down()
    async with app.router.lifespan_context(app), _client(app) as client:
        assert (await client.get("/health")).status_code == 200  # alive: no crash loop
        assert (await client.get("/ready")).status_code == 503
        started = time.monotonic()
        r = await client.post("/chat", json={"message": "hello"}, headers=AUTH)
        assert r.status_code == 503 and "Reference: " in r.json()["detail"]
        assert time.monotonic() - started < 1
        assert (await client.get("/threads", headers=AUTH)).status_code == 503
        await asyncio.sleep(1.5)  # a few failed setup attempts
        await proxy.up()
        assert await _ready_within(client, 10) < 5
        r = await client.post("/chat", json={"message": "hello"}, headers=AUTH)
        assert parse_sse(r.text)[-1][0] == "message.end"


async def test_an_outage_fails_fast_logs_one_line_and_recovers_in_seconds(
    postgres_app: tuple[Any, str], proxy: Proxy, caplog: pytest.LogCaptureFixture
) -> None:
    app, _dsn = postgres_app
    async with app.router.lifespan_context(app), _client(app) as client:
        await _ready_within(client, 10)
        thread = str(uuid.uuid4())
        r = await client.post("/chat", json={"message": "hello", "thread_id": thread}, headers=AUTH)
        assert parse_sse(r.text)[-1][0] == "message.end"
        await proxy.down()  # the database restarts: every connection is cut
        caplog.clear()
        with caplog.at_level(logging.INFO):
            durations = []
            for _ in range(3):
                started = time.monotonic()
                r = await client.get("/threads", headers=AUTH)
                durations.append(time.monotonic() - started)
                assert r.status_code == 503, r.text
                assert r.json()["detail"].startswith("Database unavailable. Reference: ")
            assert (await client.get("/ready")).status_code == 503
        # The first request finds out (bounded by the pool timeout), the next ones fail fast.
        assert durations[0] < 7 and max(durations[1:]) < 3.5, durations
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors == [], [r.getMessage() for r in errors]
        assert not any(r.exc_info for r in caplog.records if "unavailable" in r.getMessage())
        await proxy.up()
        assert await _ready_within(client, 15) < 8
        r = await client.post("/chat", json={"message": "again", "thread_id": thread}, headers=AUTH)
        assert parse_sse(r.text)[-1][0] == "message.end"


async def test_a_run_that_loses_its_lease_stops_before_writing(
    postgres_app: tuple[Any, str], use_test_tools
) -> None:
    """Another replica took the thread over (the lease expired): the run must not write."""
    from langchain_core.tools import tool

    app, dsn = postgres_app
    in_tool = asyncio.Event()
    loop = asyncio.get_running_loop()

    @tool
    def slow_probe(query: str) -> str:
        """Test-only tool: answers after 3 s."""
        loop.call_soon_threadsafe(in_tool.set)
        time.sleep(3)
        return "late result"

    async with app.router.lifespan_context(app), _client(app) as client:
        await _ready_within(client, 10)
        use_test_tools(slow_probe)
        thread = str(uuid.uuid4())
        run = asyncio.create_task(
            client.post(
                "/chat",
                json={"message": "Run the slow probe for Paris", "thread_id": thread},
                headers=AUTH,
            )
        )
        await asyncio.wait_for(in_tool.wait(), 10)
        # What a replica taking over an expired lease does to the row.
        await _sql(
            dsn,
            "UPDATE thread_locks SET owner = 'other-replica', token = token + 1000 "
            "WHERE thread_id = %s",
            (thread,),
        )
        started = time.monotonic()
        events = parse_sse((await run).text)
        assert time.monotonic() - started < 2.5  # stopped, not waiting for the tool
        assert [e for e, _ in events] == ["message.start", "tool.call", "error"]
        assert events[-1][1]["code"] == "unavailable"
        await asyncio.sleep(3.5)  # the tool thread finishes: its result must not land
        rows = await _sql(
            dsn, "SELECT count(*) FROM checkpoint_writes WHERE thread_id = %s", (thread,)
        )
        writes_after = rows[0][0]
        from {{cookiecutter.agent_directory}}.agent import graph

        state = await graph.checkpointer.aget_tuple({"configurable": {"thread_id": thread}})
        messages = state.checkpoint["channel_values"]["messages"]
        assert [m.type for m in messages] == ["human", "ai"]  # no tool result, no repair
        rows = await _sql(dsn, "SELECT status FROM runs WHERE thread_id = %s", (thread,))
        assert rows == [("interrupted",)]
        # Nothing arrived later either.
        await asyncio.sleep(0.5)
        rows = await _sql(
            dsn, "SELECT count(*) FROM checkpoint_writes WHERE thread_id = %s", (thread,)
        )
        assert rows[0][0] == writes_after


async def test_a_database_outage_mid_tool_call_ends_the_run_fast_and_the_thread_recovers(
    postgres_app: tuple[Any, str], proxy: Proxy, use_test_tools
) -> None:
    from langchain_core.tools import tool

    app, dsn = postgres_app
    in_tool = asyncio.Event()
    loop = asyncio.get_running_loop()

    @tool
    def slow_probe(query: str) -> str:
        """Test-only tool: slow for Paris."""
        if "Paris" in query:
            loop.call_soon_threadsafe(in_tool.set)
            time.sleep(2)
        return "sunny"

    async with app.router.lifespan_context(app), _client(app) as client:
        await _ready_within(client, 10)
        use_test_tools(slow_probe)
        thread = str(uuid.uuid4())
        run = asyncio.create_task(
            client.post(
                "/chat",
                json={"message": "Run the slow probe for Paris", "thread_id": thread},
                headers=AUTH,
            )
        )
        await asyncio.wait_for(in_tool.wait(), 10)
        await proxy.down()
        started = time.monotonic()
        events = parse_sse((await run).text)
        assert time.monotonic() - started < 10  # not a minute of pool timeouts
        assert events[-1][0] == "error" and events[-1][1]["code"] == "unavailable"
        await proxy.up()
        await _ready_within(client, 15)
        deadline = time.monotonic() + 10
        while True:  # the lease of the stopped run is released in the background
            r = await client.post(
                "/chat", json={"message": "hello", "thread_id": thread}, headers=AUTH
            )
            if r.status_code != 409 or time.monotonic() > deadline:
                break
            await asyncio.sleep(0.3)
        assert parse_sse(r.text)[-1][0] == "message.end", r.text
        messages = (await client.get(f"/threads/{thread}/messages", headers=AUTH)).json()
        assert [m["role"] for m in messages] == ["user", "assistant", "tool", "user", "assistant"]
        assert (
            messages[2]["is_error"]
            and messages[2]["tool_call_id"] == (messages[1]["tool_calls"][0]["id"])
        )
        # Both runs are on record: the stopped one once the database was back.
        deadline = time.monotonic() + 10
        while True:
            rows = await _sql(
                dsn, "SELECT status FROM runs WHERE thread_id = %s ORDER BY created_at", (thread,)
            )
            if rows[:1] == [("interrupted",)] or time.monotonic() > deadline:
                break
            await asyncio.sleep(0.3)
        assert rows == [("interrupted",), ("ok",)]


# --- a crash mid tool call ---------------------------------------------------------------------

# The server serves a graph with one test-only tool (never the project's own),
# slow when asked about "slow".
SERVER_SCRIPT = """
import os, sys, time
from langchain.agents import create_agent
from langchain_core.tools import tool
from {agent} import agent
from {agent}.app_utils import chat, run_locks
run_locks.LEASE_TTL_S, run_locks.RENEW_EVERY_S, run_locks.LOCAL_VALIDITY_S = 2.0, 0.2, 1.0
chat.RECONCILE_INTERVAL_S, chat.RECONCILE_GRACE_S = 0.5, 0.0
@tool
def probe(query: str) -> str:
    \"\"\"Test-only tool: reports what it was asked about.\"\"\"
    if "slow" in query:
        time.sleep(60)
    return "probe reading for " + query
agent.graph = create_agent(
    model=agent.get_model(), tools=[probe], system_prompt=agent.SYSTEM_PROMPT,
    middleware=agent.middleware(), context_schema=agent.AgentContext, name="test-agent",
).with_config(dict(recursion_limit=agent.recursion_limit()))
import uvicorn
from {agent}.fast_api_app import app
uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_config=None)
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(dsn: str, port: int, log: Path) -> subprocess.Popen[bytes]:
    env = {**os.environ, "CHECKPOINTER": "postgres", "POSTGRES_DSN": dsn, "LOG_FORMAT": "text"}
    process = subprocess.Popen(
        [sys.executable, "-c", SERVER_SCRIPT.format(agent=AGENT_DIR), str(port)],
        cwd=PROJECT,
        env=env,
        stdout=log.open("ab"),
        stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with contextlib.suppress(httpx.HTTPError):
            if httpx.get(f"http://127.0.0.1:{port}/ready", timeout=1).status_code == 200:
                return process
        time.sleep(0.2)
    process.kill()
    raise AssertionError("the server did not get ready: " + log.read_text()[-2000:])


def test_a_crash_mid_tool_call_leaves_a_usable_thread_and_an_interrupted_run(
    dsn: str, tmp_path: Path
) -> None:
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    log = tmp_path / "server.log"
    thread = f"crash-{uuid.uuid4().hex[:8]}"
    server = _start_server(dsn, port, log)
    try:
        body = {"thread_id": thread, "message": "Run the probe for slow city"}
        with contextlib.suppress(httpx.HTTPError):
            with httpx.stream("POST", f"{base}/chat", json=body, headers=AUTH, timeout=30) as r:
                for line in r.iter_lines():
                    if line.startswith("event: tool.call"):
                        time.sleep(1.0)  # the model step's checkpoint lands
                        server.send_signal(signal.SIGKILL)  # OOM kill, node loss, ...
                        server.wait()
                        break
        server = _start_server(dsn, port, log)
        crashed = httpx.get(f"{base}/threads/{thread}/messages", headers=AUTH).json()
        assert [m["role"] for m in crashed] == ["user", "assistant"]
        for message in ("hello", "hello again"):
            deadline = time.monotonic() + 10
            while True:
                r = httpx.post(
                    f"{base}/chat", json={"thread_id": thread, "message": message}, headers=AUTH
                )
                # The dead process's lease holds the thread until it expires (2 s here).
                if r.status_code != 409 or time.monotonic() > deadline:
                    break
                time.sleep(0.3)
            events = parse_sse(r.text)
            assert events[-1][0] == "message.end", events
        messages = httpx.get(f"{base}/threads/{thread}/messages", headers=AUTH).json()
        roles = [m["role"] for m in messages]
        # The open call got its result right after it, before the next turn.
        assert roles == ["user", "assistant", "tool", "user", "assistant", "user", "assistant"]
        assert messages[2]["is_error"] and "interrupted" in messages[2]["content"]
        assert messages[2]["tool_call_id"] == messages[1]["tool_calls"][0]["id"]
        # The killed run is on record, as interrupted, once its lease has expired.
        deadline = time.monotonic() + 15
        while True:
            statuses = asyncio.run(
                _sql(
                    dsn,
                    "SELECT status FROM runs WHERE thread_id = %s ORDER BY created_at",
                    (thread,),
                )
            )
            if (statuses and statuses[0] == ("interrupted",)) or time.monotonic() > deadline:
                break
            time.sleep(0.5)
        assert statuses == [("interrupted",), ("ok",), ("ok",)], statuses
        metrics_text = httpx.get(f"{base}/metrics").text
        assert 'agent_runs_total{status="interrupted"} 1.0' in metrics_text
    finally:
        server.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            server.wait(10)
        if server.poll() is None:
            server.kill()
