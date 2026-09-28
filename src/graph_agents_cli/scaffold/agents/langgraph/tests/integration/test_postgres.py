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

"""Postgres behaviour: schema setup across replicas, run locks, atomic claims, pool healing,
and the A2A task store (shared by replicas, kept across restarts).

Opt-in: set `TEST_POSTGRES_DSN` to the URL of a server where the user may
create databases (each test gets a fresh one, dropped afterwards), for example
`postgresql://agent:agent@localhost:5432/postgres`.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from a2a.server.context import ServerCallContext
from a2a.types import ListTasksRequest, Task, TaskState
from google.protobuf import json_format

from {{cookiecutter.agent_directory}}.app_utils import chat as chat_module
from {{cookiecutter.agent_directory}}.app_utils.a2a import (
    INTERRUPTED_TASK_TEXT,
    ExpiringTaskStore,
    PostgresTaskStore,
    PrincipalUser,
)
from {{cookiecutter.agent_directory}}.app_utils.auth import Principal
from {{cookiecutter.agent_directory}}.app_utils.checkpointer import POSTGRES, get_checkpointer
from {{cookiecutter.agent_directory}}.app_utils.db import Database, RunRecord, RunStore
from {{cookiecutter.agent_directory}}.app_utils.threads import ThreadBusy, ThreadLocks, ThreadStore

ADMIN_DSN = os.environ.get("TEST_POSTGRES_DSN", "")
pytestmark = pytest.mark.skipif(not ADMIN_DSN, reason="TEST_POSTGRES_DSN is not set")


def _with_database(dsn: str, name: str) -> str:
    """The same server URL, pointing at database `name`."""
    from urllib.parse import urlsplit

    return urlsplit(dsn)._replace(path=f"/{name}").geturl()


@pytest.fixture
async def dsn(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    import psycopg

    name = f"gac_test_{uuid.uuid4().hex[:12]}"
    async with await psycopg.AsyncConnection.connect(ADMIN_DSN, autocommit=True) as admin:
        await admin.execute(f'CREATE DATABASE "{name}"')
    url = _with_database(ADMIN_DSN, name)
    monkeypatch.setenv("CHECKPOINTER", "postgres")
    monkeypatch.setenv("POSTGRES_DSN", url)
    yield url
    async with await psycopg.AsyncConnection.connect(ADMIN_DSN, autocommit=True) as admin:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


async def _terminate_other_sessions(dsn: str) -> int:
    import psycopg

    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        cur = await conn.execute(
            "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid()"
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0


async def test_replicas_starting_together_set_up_the_schema_once(dsn: str) -> None:
    async def replica() -> None:
        db = Database(POSTGRES, dsn)
        await db.open()
        try:
            async with get_checkpointer(db.pool) as saver:
                await saver.setup()
        finally:
            await db.close()

    await asyncio.gather(*(replica() for _ in range(4)))
    db = Database(POSTGRES, dsn)
    await db.open()
    try:
        row = await db.fetchone("SELECT count(*) AS n FROM checkpoint_migrations")
        assert row is not None and row["n"] > 0
    finally:
        await db.close()


async def test_an_older_schema_is_migrated_in_place(dsn: str) -> None:
    import psycopg

    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, "
            "principal_hash TEXT NOT NULL, model TEXT, status TEXT NOT NULL, "
            "input_tokens INTEGER, output_tokens INTEGER, latency_ms INTEGER, "
            "error_type TEXT, payload JSONB, created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        await conn.execute(
            "CREATE TABLE threads (thread_id TEXT PRIMARY KEY, principal_id TEXT NOT NULL, "
            "tenant TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        await conn.execute("INSERT INTO threads (thread_id, principal_id) VALUES ('old', 'a')")
    db = Database(POSTGRES, dsn)
    await db.open()
    try:
        store = ThreadStore(db)
        record = await store.get("old")
        assert record is not None and record.updated_at
        runs = RunStore(db)
        await runs.record(
            RunRecord(
                run_id="r1",
                thread_id="old",
                principal_hash="h",
                model="m",
                status="ok",
                metadata={"source": "web"},
            )
        )
        stored = await runs.get("r1")
        assert stored is not None and stored.metadata == {"source": "web"}
    finally:
        await db.close()


async def test_the_run_lock_spans_replicas(dsn: str) -> None:
    db = Database(POSTGRES, dsn)
    await db.open()  # creates the lease table
    await db.close()
    replica_a, replica_b = ThreadLocks(dsn), ThreadLocks(dsn)
    try:
        lease = await replica_a.acquire("t1")
        with pytest.raises(ThreadBusy):
            await replica_b.acquire("t1")
        other = await replica_b.acquire("t2")
        await lease.release()
        again = await replica_b.acquire("t1")
        await again.release()
        await other.release()
    finally:
        await replica_a.close()
        await replica_b.close()


async def test_claims_are_atomic_across_connections(dsn: str) -> None:
    db = Database(POSTGRES, dsn)
    await db.open()
    try:
        store = ThreadStore(db)
        alice, bob = Principal(id="alice"), Principal(id="bob")
        for i in range(20):
            thread_id = f"race-{i}"
            results = await asyncio.gather(
                store.claim(thread_id, alice), store.claim(thread_id, bob)
            )
            assert sorted(created for _, created in results) == [False, True]
            owners = {record.principal_id for record, _ in results}
            assert len(owners) == 1  # both calls see the same, single owner
    finally:
        await db.close()


async def test_pool_and_run_locks_heal_after_connections_are_killed(dsn: str) -> None:
    """What a Postgres restart or failover does to live connections, without the restart."""
    db = Database(POSTGRES, dsn)
    await db.open()
    locks = ThreadLocks(dsn)
    try:
        await asyncio.gather(*(db.fetchone("SELECT pg_sleep(0.05)") for _ in range(5)))
        lease = await locks.acquire("t1")
        await lease.release()
        assert await _terminate_other_sessions(dsn) >= 2
        for _ in range(5):
            assert await db.fetchone("SELECT 1 AS ok") == {"ok": 1}
        lease = await locks.acquire("t1")
        await lease.release()
    finally:
        await locks.close()
        await db.close()


async def test_server_runtime_run_records_use_their_own_table(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URI", dsn)
    db = Database.for_server()
    await db.open()
    try:
        tables = await db.fetchall(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
        names = {t["table_name"] for t in tables}
        assert "agent_runs" in names and "threads" not in names and "runs" not in names
        runs = RunStore(db)
        await runs.record(
            RunRecord(run_id="r1", thread_id="t", principal_hash="h", model="m", status="ok")
        )
        await runs.delete_for_thread("t")
        assert await runs.get("r1") is None
    finally:
        await db.close()


async def test_run_record_pages_reach_every_thread(dsn: str) -> None:
    """`thread_ids_before` pages in id order, so a sweep reaches ids past the first page."""
    db = Database(POSTGRES, dsn)
    await db.open()
    try:
        runs = RunStore(db)
        old = "2000-01-01T00:00:00+00:00"
        ids = [f"t-{i:02d}" for i in range(7)]
        for n, thread_id in enumerate(ids + ids):  # two records per thread
            await runs.record(
                RunRecord(
                    run_id=f"r{n}",
                    thread_id=thread_id,
                    principal_hash="h",
                    model="m",
                    status="ok",
                    created_at=old,
                )
            )
        await runs.record(
            RunRecord(run_id="new", thread_id="t-new", principal_hash="h", model="m", status="ok")
        )
        pages: list[list[str]] = []
        after = None
        while True:
            page = await runs.thread_ids_before("2020-01-01T00:00:00+00:00", after=after, limit=3)
            pages.append(page)
            if len(page) < 3:
                break
            after = page[-1]
        assert [t for page in pages for t in page] == ids  # each once, in order, no new one
    finally:
        await db.close()


async def test_an_id_with_state_but_no_owner_row_is_never_claimed(dsn: str) -> None:
    db = Database(POSTGRES, dsn)
    await db.open()
    try:
        store = ThreadStore(db)
        alice, bob = Principal(id="alice"), Principal(id="bob")

        async def orphan_only(thread_id: str) -> bool:
            return thread_id == "orphan"

        async def always(thread_id: str) -> bool:
            return True

        with pytest.raises(Exception) as exc:
            await store.ensure("orphan", bob, has_state=orphan_only)
        assert getattr(exc.value, "status_code", None) == 403
        assert await store.get("orphan") is None
        assert (await store.ensure("fresh", alice, has_state=orphan_only)).principal_id == "alice"
        # A thread with an owner row is its owner's to continue, state or not.
        assert (await store.ensure("fresh", alice, has_state=always)).principal_id == "alice"
        with pytest.raises(Exception) as exc:
            await store.ensure("fresh", bob, has_state=always)
        assert getattr(exc.value, "status_code", None) == 403
    finally:
        await db.close()


# --- A2A tasks ----------------------------------------------------------------------------


def _ctx(owner: str) -> ServerCallContext:
    return ServerCallContext(user=PrincipalUser(owner))


def _a2a_task(
    task_id: str, context_id: str = "c1", state: int = TaskState.TASK_STATE_COMPLETED, **extra: Any
) -> Task:
    task = Task(id=task_id, context_id=context_id)
    task.status.state = state
    if "second" in extra:
        task.status.timestamp.FromSeconds(1_800_000_000 + extra["second"])
    if "text" in extra:
        artifact = task.artifacts.add()
        artifact.artifact_id = "a1"
        artifact.parts.add().text = extra["text"]
    return task


async def _opened(dsn: str) -> Database:
    db = Database(POSTGRES, dsn)
    await db.open()  # creates the tables, the A2A task table included
    return db


async def test_a2a_tasks_are_shared_by_replicas_and_kept_across_restarts(dsn: str) -> None:
    replica_a, replica_b = await _opened(dsn), await _opened(dsn)
    try:
        store_a = PostgresTaskStore(3600, replica_a)
        store_b = PostgresTaskStore(3600, replica_b)
        task = _a2a_task("t1", "orders-thread.billing" * 3, second=5, text="Order 1 is on its way.")
        await store_a.save(task, _ctx("alice"))
        seen = await store_b.get("t1", _ctx("alice"))
        assert seen is not None
        assert json_format.MessageToDict(seen) == json_format.MessageToDict(task)
        assert await store_b.get("t1", _ctx("bob")) is None  # another principal: not found
        assert (await store_b.list(ListTasksRequest(), _ctx("alice"))).total_size == 1
        assert (await store_b.list(ListTasksRequest(), _ctx("bob"))).total_size == 0
    finally:
        await replica_a.close()
    restarted = await _opened(dsn)
    try:
        assert await PostgresTaskStore(3600, restarted).get("t1", _ctx("alice")) is not None
        await replica_b.close()
    finally:
        await restarted.close()


async def test_a2a_task_listing_matches_the_in_memory_store(dsn: str) -> None:
    """Filters, order (newest status first, no timestamp last), pages and totals as the SDK's."""
    db = await _opened(dsn)
    try:
        postgres = PostgresTaskStore(0, db)
        memory = ExpiringTaskStore(0)
        tasks = [
            _a2a_task("t1", "c1", TaskState.TASK_STATE_COMPLETED, second=10),
            _a2a_task("t2", "c1", TaskState.TASK_STATE_INPUT_REQUIRED, second=30),
            _a2a_task("t3", "c2", TaskState.TASK_STATE_COMPLETED, second=20),
            _a2a_task("t4", "c2", TaskState.TASK_STATE_FAILED),
            _a2a_task("t5", "c1", TaskState.TASK_STATE_COMPLETED, second=30),
            _a2a_task("t6", "c1", TaskState.TASK_STATE_COMPLETED),
        ]
        for task in tasks:
            for store in (postgres, memory):
                await store.save(task, _ctx("alice"))
        await postgres.save(_a2a_task("t7", "c1", second=40), _ctx("bob"))
        after = _a2a_task("x", second=20).status.timestamp
        requests = [
            ListTasksRequest(),
            ListTasksRequest(context_id="c1"),
            ListTasksRequest(status=TaskState.TASK_STATE_COMPLETED),
            ListTasksRequest(status_timestamp_after=after),
        ]
        for request in requests:
            for page_size in (2, 3, 50):
                pages: dict[str, list[list[str]]] = {}
                for name, store in (("postgres", postgres), ("memory", memory)):
                    got: list[list[str]] = []
                    token = ""
                    while True:
                        page_request = ListTasksRequest()
                        page_request.CopyFrom(request)
                        page_request.page_size = page_size
                        page_request.page_token = token
                        page = await store.list(page_request, _ctx("alice"))
                        got.append([t.id for t in page.tasks] + [f"total={page.total_size}"])
                        token = page.next_page_token
                        if not token:
                            break
                    pages[name] = got
                assert pages["postgres"] == pages["memory"], (request, page_size)
    finally:
        await db.close()


async def test_a2a_tasks_expire_and_go_with_their_thread(dsn: str) -> None:
    db = await _opened(dsn)
    try:
        store = PostgresTaskStore(1, db)
        await store.save(_a2a_task("old", "c1"), _ctx("alice"))
        await asyncio.sleep(1.2)
        assert await store.get("old", _ctx("alice")) is None  # expired on the database clock
        assert await store.sweep() == (1, 0)  # and deleted by the sweep
        store = PostgresTaskStore(3600, db)
        owned = (("alice", "a", "c1"), ("bob", "b", "c1"), ("bob", "c", "c2"))
        for owner, task_id, context in owned:
            await store.save(_a2a_task(task_id, context), _ctx(owner))
        assert await store.delete_context("c1") == 2  # every owner's tasks of that thread
        assert await store.get("c", _ctx("bob")) is not None
    finally:
        await db.close()


async def test_a2a_tasks_of_a_dead_run_are_failed_not_left_working(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task `working` in a process that died is failed once its thread's lease is gone."""
    monkeypatch.setattr(chat_module, "RECONCILE_GRACE_S", 0.0)
    db = await _opened(dsn)
    locks = ThreadLocks(dsn)
    try:
        store = PostgresTaskStore(3600, db)
        await store.save(_a2a_task("dead", "t-dead", TaskState.TASK_STATE_WORKING), _ctx("alice"))
        await store.save(_a2a_task("live", "t-live", TaskState.TASK_STATE_WORKING), _ctx("alice"))
        await store.save(
            _a2a_task("paused", "t-paused", TaskState.TASK_STATE_INPUT_REQUIRED), _ctx("alice")
        )
        lease = await locks.acquire("t-live")  # its run goes on in some replica
        await asyncio.sleep(0.05)
        assert await store.sweep() == (0, 1)
        dead = await store.get("dead", _ctx("alice"))
        assert dead is not None and dead.status.state == TaskState.TASK_STATE_FAILED
        assert dead.status.message.parts[0].text == INTERRUPTED_TASK_TEXT
        live = await store.get("live", _ctx("alice"))
        assert live is not None and live.status.state == TaskState.TASK_STATE_WORKING
        # The lease is not this app's: a CancelTask here is refused (see a2a.py).
        assert await store.running_elsewhere(live) is True
        await lease.release()
        assert await store.running_elsewhere(live) is False
        paused = await store.get("paused", _ctx("alice"))
        assert paused is not None and paused.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    finally:
        await locks.close()
        await db.close()


async def test_a2a_tasks_holding_a_nul_are_saved_with_a_replacement_character(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Postgres stores no U+0000 (jsonb and TEXT refuse it): the store saves U+FFFD instead.

    A save that failed on one (a tool's output the reply repeats, say) lost the
    task while its run went on; the sweep then failed it as if its process had died.
    """
    monkeypatch.setattr(chat_module, "RECONCILE_GRACE_S", 0.0)
    db = await _opened(dsn)
    try:
        store = PostgresTaskStore(3600, db)
        task = _a2a_task("t1", "ab\x00cd", TaskState.TASK_STATE_WORKING, text="reading\x00 42")
        message = task.history.add()
        message.message_id = "m1"
        message.parts.add().text = "tell me\x00 more"
        json_format.ParseDict({"key\x00": ["value\x00"]}, message.parts.add().data)
        json_format.ParseDict({"note": "x\x00"}, message.metadata)
        await store.save(task, _ctx("alice"))
        seen = await store.get("t1", _ctx("alice"))
        assert seen is not None
        assert seen.context_id == "ab\ufffdcd"
        assert seen.artifacts[0].parts[0].text == "reading\ufffd 42"
        assert seen.history[0].parts[0].text == "tell me\ufffd more"
        assert json_format.MessageToDict(seen.history[0].parts[1].data) == {
            "key\ufffd": ["value\ufffd"]
        }
        assert json_format.MessageToDict(seen.history[0].metadata) == {"note": "x\ufffd"}
        # The contextId a caller names finds it; an id holding a U+0000 is not an error.
        listed = await store.list(ListTasksRequest(context_id="ab\x00cd"), _ctx("alice"))
        assert [t.id for t in listed.tasks] == ["t1"]
        assert await store.get("t\x00", _ctx("alice")) is None
        # Its thread has no run: the sweep fails it, rewriting the stored task.
        assert await store.sweep() == (0, 1)
        failed = await store.get("t1", _ctx("alice"))
        assert failed is not None and failed.status.state == TaskState.TASK_STATE_FAILED
        assert failed.artifacts[0].parts[0].text == "reading\ufffd 42"
    finally:
        await db.close()
