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

"""App-owned tables beside the checkpointer schema, and the run-record store.

Run records are durable rows in Postgres whenever the app has a Postgres
database: `POSTGRES_DSN` under `CHECKPOINTER=postgres` (fastapi; tables `runs`
and `threads`), `DATABASE_URI` under langgraph-server (table `agent_runs`, so
nothing collides with the server's own schema). Otherwise they live in a
bounded in-process dict (the newest `MEMORY_RUNS_CAP` records).

Records always hold the `metadata` capture set plus the caller's (capped)
`/chat` metadata, and hold request/response content only under
`TRACE_CAPTURE=full`. The schema is created at startup with `CREATE ... IF NOT
EXISTS` / `ADD COLUMN IF NOT EXISTS` under a Postgres advisory lock, so
replicas starting together do not race and an existing database is upgraded
in place (the index added to an existing `threads` table is built
concurrently, without blocking writes). `RETENTION_DAYS` (see `chat.py`)
purges the records of idle threads.
"""

from __future__ import annotations

import json
import os
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from {{cookiecutter.agent_directory}}.app_utils.checkpointer import (
    MEMORY,
    POSTGRES,
    checkpointer_kind,
    open_pool,
    postgres_dsn,
    schema_lock,
)

RUNS_TABLE = "runs"
SERVER_RUNS_TABLE = "agent_runs"
MEMORY_RUNS_CAP = 10_000

# Table names are constants of this module, never caller input.
RUNS_DDL = """
CREATE TABLE IF NOT EXISTS {runs} (
    run_id         TEXT PRIMARY KEY,
    thread_id      TEXT NOT NULL,
    principal_hash TEXT NOT NULL,
    model          TEXT,
    status         TEXT NOT NULL,
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    latency_ms     INTEGER,
    error_type     TEXT,
    metadata       JSONB,
    payload        JSONB,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE {runs} ADD COLUMN IF NOT EXISTS metadata JSONB;
CREATE INDEX IF NOT EXISTS {runs}_thread_id_idx ON {runs} (thread_id);
"""

THREADS_DDL = """
CREATE TABLE IF NOT EXISTS threads (
    thread_id    TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    tenant       TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE threads ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
CREATE INDEX IF NOT EXISTS threads_principal_id_idx ON threads (principal_id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS threads_updated_at_idx ON threads (updated_at);
"""


def capture_full() -> bool:
    """True when `TRACE_CAPTURE=full`; the default is `metadata`."""
    return (os.environ.get("TRACE_CAPTURE") or "metadata").strip().lower() == "full"


def utcnow_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def is_postgres_url(url: str | None) -> bool:
    return bool(url) and str(url).strip().lower().startswith(("postgres://", "postgresql://"))


@dataclass
class RunRecord:
    run_id: str
    thread_id: str
    principal_hash: str
    model: str | None
    status: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None
    error_type: str | None = None
    metadata: dict[str, Any] | None = None
    payload: dict[str, Any] | None = None
    created_at: str = field(default_factory=utcnow_iso)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class Database:
    """The memory/postgres switch shared by the run and thread stores.

    Under postgres one connection pool serves the app tables and (fastapi)
    the checkpointer; see `checkpointer.open_pool` for its health check and sizing.
    """

    def __init__(
        self,
        kind: str,
        dsn: str | None = None,
        *,
        runs_table: str = RUNS_TABLE,
        with_threads: bool = True,
    ) -> None:
        self.kind = kind
        self.dsn = dsn
        self.runs_table = runs_table
        self.with_threads = with_threads
        self.pool: Any = None
        self._pool_cm: Any = None

    @classmethod
    def from_env(cls) -> Database:
        """The fastapi runtime's database, from `CHECKPOINTER` / `POSTGRES_DSN`."""
        kind = checkpointer_kind()
        if kind == POSTGRES:
            return cls(POSTGRES, postgres_dsn())
        if kind == MEMORY:
            return cls(MEMORY)
        raise RuntimeError(f"Unknown CHECKPOINTER {kind!r}; expected 'memory' or 'postgres'.")

    @classmethod
    def for_server(cls) -> Database:
        """The langgraph-server runtime's run-record store: the server's Postgres, if any.

        `langgraph dev` sets `DATABASE_URI=:memory:`; only a postgres URL is a database.
        """
        uri = (os.environ.get("DATABASE_URI") or "").strip()
        if is_postgres_url(uri):
            return cls(POSTGRES, uri, runs_table=SERVER_RUNS_TABLE, with_threads=False)
        return cls(MEMORY, runs_table=SERVER_RUNS_TABLE, with_threads=False)

    @property
    def is_postgres(self) -> bool:
        return self.kind == POSTGRES

    async def open(self) -> None:
        if not self.is_postgres:
            return
        self._pool_cm = open_pool(self.dsn or "")
        self.pool = await self._pool_cm.__aenter__()
        try:
            ddl = RUNS_DDL.format(runs=self.runs_table) + (THREADS_DDL if self.with_threads else "")
            async with schema_lock(self.dsn or ""), self.pool.connection() as conn:
                for statement in _statements(ddl):
                    await conn.execute(statement)
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        cm, self._pool_cm = self._pool_cm, None
        self.pool = None
        if cm is not None:
            await cm.__aexit__(None, None, None)

    async def ping(self) -> None:
        """A trivial query; raises when the database does not answer."""
        if not self.is_postgres:
            return
        await self.fetchone("SELECT 1 AS ok")

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(sql, params)

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            row = await cur.fetchone()
            return dict(row) if row is not None else None

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return [dict(r) for r in await cur.fetchall()]


def _statements(ddl: str) -> list[str]:
    """One statement per `execute` (the pool may prepare statements, which run one at a time)."""
    return [part.strip() for part in ddl.split(";") if part.strip()]


def _json(value: dict[str, Any] | None) -> str | None:
    return json.dumps(value) if value is not None else None


class RunStore:
    """Run records: a Postgres table, or a bounded in-process dict under memory."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.table = db.runs_table
        self._memory: OrderedDict[str, RunRecord] = OrderedDict()

    async def record(self, run: RunRecord) -> None:
        if not self.db.is_postgres:
            self._memory[run.run_id] = run
            self._memory.move_to_end(run.run_id)
            while len(self._memory) > MEMORY_RUNS_CAP:
                self._memory.popitem(last=False)
            return
        await self.db.execute(
            f"""
            INSERT INTO {self.table} (run_id, thread_id, principal_hash, model, status,
                input_tokens, output_tokens, latency_ms, error_type, metadata, payload, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
            ON CONFLICT (run_id) DO UPDATE SET
                status = EXCLUDED.status, input_tokens = EXCLUDED.input_tokens,
                output_tokens = EXCLUDED.output_tokens, latency_ms = EXCLUDED.latency_ms,
                error_type = EXCLUDED.error_type, metadata = EXCLUDED.metadata,
                payload = EXCLUDED.payload
            """,
            (
                run.run_id,
                run.thread_id,
                run.principal_hash,
                run.model,
                run.status,
                run.input_tokens,
                run.output_tokens,
                run.latency_ms,
                run.error_type,
                _json(run.metadata),
                _json(run.payload),
                run.created_at,
            ),
        )

    async def get(self, run_id: str) -> RunRecord | None:
        if not self.db.is_postgres:
            return self._memory.get(run_id)
        row = await self.db.fetchone(f"SELECT * FROM {self.table} WHERE run_id = %s", (run_id,))
        return _run_from_row(row) if row else None

    async def list_for_thread(self, thread_id: str) -> list[RunRecord]:
        if not self.db.is_postgres:
            return sorted(
                (r for r in self._memory.values() if r.thread_id == thread_id),
                key=lambda r: r.created_at,
            )
        rows = await self.db.fetchall(
            f"SELECT * FROM {self.table} WHERE thread_id = %s ORDER BY created_at", (thread_id,)
        )
        return [_run_from_row(r) for r in rows]

    async def thread_ids_before(self, cutoff_iso: str, *, limit: int = 500) -> list[str]:
        """Threads that have a run record older than `cutoff_iso`."""
        if not self.db.is_postgres:
            ids = {r.thread_id for r in self._memory.values() if r.created_at < cutoff_iso}
            return sorted(ids)[:limit]
        rows = await self.db.fetchall(
            f"SELECT DISTINCT thread_id FROM {self.table} WHERE created_at < %s LIMIT %s",
            (cutoff_iso, limit),
        )
        return [r["thread_id"] for r in rows]

    async def delete_for_thread(self, thread_id: str) -> None:
        if not self.db.is_postgres:
            for run_id in [k for k, r in self._memory.items() if r.thread_id == thread_id]:
                del self._memory[run_id]
            return
        await self.db.execute(f"DELETE FROM {self.table} WHERE thread_id = %s", (thread_id,))


def _decode(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _run_from_row(row: dict[str, Any]) -> RunRecord:
    created = row.get("created_at")
    return RunRecord(
        run_id=row["run_id"],
        thread_id=row["thread_id"],
        principal_hash=row["principal_hash"],
        model=row.get("model"),
        status=row["status"],
        input_tokens=row.get("input_tokens"),
        output_tokens=row.get("output_tokens"),
        latency_ms=row.get("latency_ms"),
        error_type=row.get("error_type"),
        metadata=_decode(row.get("metadata")),
        payload=_decode(row.get("payload")),
        created_at=created.isoformat() if isinstance(created, datetime) else str(created),
    )
