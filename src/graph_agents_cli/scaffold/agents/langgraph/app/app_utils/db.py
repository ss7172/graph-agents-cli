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
bounded in-process dict (the newest `MEMORY_RUNS_CAP` records). Approvals of
gated API calls (`approvals.py`) live beside them the same way: table
`approvals` (fastapi) or `agent_approvals` (langgraph-server), else in process
memory, which `langgraph dev` also writes to a file beside its own threads.

A run's record is written when it starts, with status `running`, and
updated when it ends (`ok`, `step_limit`, `error`, `timeout`, `cancelled` or
`interrupted`). A run whose process died (a crash, an OOM kill, a lost node, a
rollout that ran out of grace) never updates its record: `RunStore.reconcile`
marks such records `interrupted` once the run's lease on its thread has
expired (see `run_locks.py`), at startup and every minute.

Records always hold the `metadata` capture set plus the caller's (capped)
`/chat` metadata, and hold request/response content only under
`TRACE_CAPTURE=full`. The schema is created with `CREATE ... IF NOT EXISTS` /
`ADD COLUMN IF NOT EXISTS` under a Postgres advisory lock, so replicas
starting together do not race and an existing database is upgraded in place
(indexes added to existing `threads` and `runs` tables are built concurrently,
without blocking writes). `RETENTION_DAYS` (see `chat.py`) purges the records
of idle threads.
"""

from __future__ import annotations

import json
import os
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from {{cookiecutter.agent_directory}}.app_utils.checkpointer import (
    MEMORY,
    POSTGRES,
    DbHealth,
    checkpointer_kind,
    is_connection_error,
    open_pool,
    postgres_dsn,
    schema_lock,
)

RUNS_TABLE = "runs"
SERVER_RUNS_TABLE = "agent_runs"
LOCKS_TABLE = "thread_locks"
SERVER_LOCKS_TABLE = "agent_thread_locks"
APPROVALS_TABLE = "approvals"
SERVER_APPROVALS_TABLE = "agent_approvals"
MEMORY_RUNS_CAP = 10_000

# Run statuses owned by the store (the others are set by `chat.py`).
RUN_RUNNING = "running"
RUN_INTERRUPTED = "interrupted"

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
CREATE INDEX CONCURRENTLY IF NOT EXISTS {runs}_running_idx ON {runs} (created_at)
    WHERE status = 'running';
"""

# One row per thread with a run in progress: the replica holding it (`owner`),
# a fencing token that changes whenever the thread changes hands, and the
# lease expiry the holder keeps pushing forward (see `run_locks.py`).
LOCKS_DDL = """
CREATE TABLE IF NOT EXISTS {locks} (
    thread_id   TEXT PRIMARY KEY,
    owner       TEXT NOT NULL,
    token       BIGINT NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    acquired_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE SEQUENCE IF NOT EXISTS {locks}_token_seq;
"""

# Human approvals of gated outbound API calls (see `approvals.py`). The requester
# and the decider are hashed ids; `payload` is what approvers are shown (the
# call, masked fields masked); `requester_context` the requester's roles and
# public attributes, which a run resumed by another principal acts with;
# `message_id` and `tool_call_id` name the tool call that asked, which the API
# client looks up (with `interrupt_id`) when that tool call runs again.
APPROVALS_DDL = """
CREATE TABLE IF NOT EXISTS {approvals} (
    approval_id       TEXT PRIMARY KEY,
    thread_id         TEXT NOT NULL,
    run_id            TEXT NOT NULL,
    interrupt_id      TEXT NOT NULL,
    requester_hash    TEXT NOT NULL,
    requester_context JSONB,
    api               TEXT NOT NULL,
    method            TEXT NOT NULL,
    path              TEXT NOT NULL,
    operation_id      TEXT,
    call_hash         TEXT NOT NULL,
    tool_call_id      TEXT,
    message_id        TEXT,
    approvers         JSONB NOT NULL,
    payload           JSONB NOT NULL,
    status            TEXT NOT NULL,
    decided_by        TEXT,
    decided_at        TIMESTAMPTZ,
    comment           TEXT,
    used_at           TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at        TIMESTAMPTZ NOT NULL
);
ALTER TABLE {approvals} ADD COLUMN IF NOT EXISTS message_id TEXT;
CREATE INDEX IF NOT EXISTS {approvals}_thread_id_idx ON {approvals} (thread_id);
CREATE INDEX IF NOT EXISTS {approvals}_tool_call_idx ON {approvals} (message_id, tool_call_id);
CREATE INDEX IF NOT EXISTS {approvals}_interrupt_idx ON {approvals} (interrupt_id);
CREATE INDEX IF NOT EXISTS {approvals}_pending_idx ON {approvals} (expires_at)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS {approvals}_requester_idx ON {approvals} (requester_hash);
CREATE INDEX IF NOT EXISTS {approvals}_approvers_idx ON {approvals} USING gin (approvers);
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


class StorageNotReady(RuntimeError):
    """The app's database is not set up yet (it has been unreachable since startup)."""


def is_database_unavailable(exc: BaseException) -> bool:
    """True when `exc` means the database cannot be reached (a 503, not a 500)."""
    return isinstance(exc, StorageNotReady) or is_connection_error(exc)


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
    the checkpointer; see `checkpointer.open_pool` for its health check and
    sizing. `health` tracks whether the database answered at last contact.
    """

    def __init__(
        self,
        kind: str,
        dsn: str | None = None,
        *,
        runs_table: str = RUNS_TABLE,
        locks_table: str = LOCKS_TABLE,
        approvals_table: str = APPROVALS_TABLE,
        with_threads: bool = True,
    ) -> None:
        self.kind = kind
        self.dsn = dsn
        self.runs_table = runs_table
        self.locks_table = locks_table
        self.approvals_table = approvals_table
        self.with_threads = with_threads
        self.health = DbHealth()
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
        tables = {
            "runs_table": SERVER_RUNS_TABLE,
            "locks_table": SERVER_LOCKS_TABLE,
            "approvals_table": SERVER_APPROVALS_TABLE,
        }
        if is_postgres_url(uri):
            return cls(POSTGRES, uri, with_threads=False, **tables)
        return cls(MEMORY, with_threads=False, **tables)

    @property
    def is_postgres(self) -> bool:
        return self.kind == POSTGRES

    async def open(self) -> None:
        """Open the pool and set up the schema at once (see `open_pool` and `setup`)."""
        await self.open_pool()
        try:
            await self.setup()
        except BaseException:
            await self.close()
            raise

    async def open_pool(self) -> None:
        """Open the connection pool without waiting for the database (postgres only).

        A DSN that does not parse raises `SettingsError`; an unreachable
        database does not: the pool connects once the database answers.
        """
        if not self.is_postgres or self.pool is not None:
            return
        self._pool_cm = open_pool(self.dsn or "", self.health)
        self.pool = await self._pool_cm.__aenter__()

    def ddl(self) -> str:
        return (
            RUNS_DDL.format(runs=self.runs_table)
            + LOCKS_DDL.format(locks=self.locks_table)
            + APPROVALS_DDL.format(approvals=self.approvals_table)
            + (THREADS_DDL if self.with_threads else "")
        )

    async def setup(self, extra: Callable[[], Awaitable[None]] | None = None) -> None:
        """Create or upgrade the app tables, then run `extra` (the saver's own setup).

        Both run under the schema lock, so replicas starting together take
        turns. Raises while the database is unreachable; the caller retries.
        """
        if not self.is_postgres:
            return
        if self.pool is None:
            raise StorageNotReady("the database pool is not open")
        async with schema_lock(self.dsn or "", health=self.health):
            async with self.pool.connection() as conn:
                for statement in _statements(self.ddl()):
                    await conn.execute(statement)
            if extra is not None:
                await extra()

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
        await self._run(sql, params, None)

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        row = await self._run(sql, params, "one")
        return dict(row) if row is not None else None

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in await self._run(sql, params, "all")]

    async def _run(self, sql: str, params: Sequence[Any], fetch: str | None) -> Any:
        if self.pool is None:
            raise StorageNotReady("the database pool is not open")
        async with self.pool.connection() as conn:
            try:
                cur = await conn.execute(sql, params)
                if fetch == "one":
                    return await cur.fetchone()
                if fetch == "all":
                    return await cur.fetchall()
                return None
            except Exception as exc:
                if getattr(conn, "broken", False) or getattr(conn, "closed", False):
                    self.health.mark_down(exc)
                raise


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
        self.locks_table = db.locks_table
        self._memory: OrderedDict[str, RunRecord] = OrderedDict()

    async def start(self, run: RunRecord) -> None:
        """Record a run as it starts (status `running`); `record` writes how it ended."""
        run.status = RUN_RUNNING
        if not self.db.is_postgres:
            self._remember(run)
            return
        await self.db.execute(
            f"""
            INSERT INTO {self.table} (run_id, thread_id, principal_hash, model, status,
                metadata, created_at)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (run_id) DO NOTHING
            """,
            (
                run.run_id,
                run.thread_id,
                run.principal_hash,
                run.model,
                run.status,
                _json(run.metadata),
                run.created_at,
            ),
        )

    async def reconcile(self, grace_s: float) -> list[str]:
        """Mark `running` records whose run is gone as `interrupted`; return their run ids.

        A run holds a lease on its thread for as long as it runs, so a
        `running` record older than `grace_s` whose thread has no live lease
        belongs to a process that died before it could record how the run
        ended. Replicas may reconcile at the same time: each record is updated
        once (the second update no longer matches `status = 'running'`).
        """
        if not self.db.is_postgres:
            return []
        rows = await self.db.fetchall(
            f"""
            UPDATE {self.table} AS r
               SET status = %s, error_type = COALESCE(r.error_type, 'ProcessLost')
             WHERE r.status = %s
               AND r.created_at < now() - make_interval(secs => %s)
               AND NOT EXISTS (
                   SELECT 1 FROM {self.locks_table} AS l
                    WHERE l.thread_id = r.thread_id AND l.expires_at > now())
            RETURNING r.run_id
            """,
            (RUN_INTERRUPTED, RUN_RUNNING, float(grace_s)),
        )
        return [r["run_id"] for r in rows]

    def _remember(self, run: RunRecord) -> None:
        self._memory[run.run_id] = run
        self._memory.move_to_end(run.run_id)
        while len(self._memory) > MEMORY_RUNS_CAP:
            self._memory.popitem(last=False)

    async def record(self, run: RunRecord) -> None:
        if not self.db.is_postgres:
            self._remember(run)
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

    async def thread_ids_before(
        self, cutoff_iso: str, *, after: str | None = None, limit: int = 500
    ) -> list[str]:
        """Threads that have a run record older than `cutoff_iso`, in id order.

        One page: ids greater than `after` (pass the last id of the previous
        page to get the next one).
        """
        if not self.db.is_postgres:
            ids = {
                r.thread_id
                for r in self._memory.values()
                if r.created_at < cutoff_iso and (after is None or r.thread_id > after)
            }
            return sorted(ids)[:limit]
        if after is None:
            rows = await self.db.fetchall(
                f"SELECT DISTINCT thread_id FROM {self.table} WHERE created_at < %s "
                "ORDER BY thread_id LIMIT %s",
                (cutoff_iso, limit),
            )
        else:
            rows = await self.db.fetchall(
                f"SELECT DISTINCT thread_id FROM {self.table} WHERE created_at < %s "
                "AND thread_id > %s ORDER BY thread_id LIMIT %s",
                (cutoff_iso, after, limit),
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
