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

"""Postgres behaviour: schema setup across replicas, run locks, atomic claims, pool healing.

Opt-in: set `TEST_POSTGRES_DSN` to the URL of a server where the user may
create databases (each test gets a fresh one, dropped afterwards), for example
`postgresql://agent:agent@localhost:5432/postgres`.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator

import pytest

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
