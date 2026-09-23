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

Run records follow the checkpointer:
durable rows in the agent's own Postgres under `CHECKPOINTER=postgres`
(`CREATE TABLE IF NOT EXISTS` at startup, no retention job), an in-process
dict under `CHECKPOINTER=memory`. They always hold the `metadata` capture set
and hold request/response content only under `TRACE_CAPTURE=full`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from {{cookiecutter.agent_directory}}.app_utils.checkpointer import (
    MEMORY,
    POSTGRES,
    checkpointer_kind,
    postgres_dsn,
)

RUNS_DDL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id         TEXT PRIMARY KEY,
    thread_id      TEXT NOT NULL,
    principal_hash TEXT NOT NULL,
    model          TEXT,
    status         TEXT NOT NULL,
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    latency_ms     INTEGER,
    error_type     TEXT,
    payload        JSONB,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS runs_thread_id_idx ON runs (thread_id);
"""

THREADS_DDL = """
CREATE TABLE IF NOT EXISTS threads (
    thread_id    TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    tenant       TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS threads_principal_id_idx ON threads (principal_id);
"""


def capture_full() -> bool:
    """True when `TRACE_CAPTURE=full`; the default is `metadata`."""
    return (os.environ.get("TRACE_CAPTURE") or "metadata").strip().lower() == "full"


def utcnow_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


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
    payload: dict[str, Any] | None = None
    created_at: str = field(default_factory=utcnow_iso)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class Database:
    """The memory/postgres switch shared by the run and thread stores."""

    def __init__(self, kind: str, dsn: str | None = None) -> None:
        self.kind = kind
        self.dsn = dsn
        self.pool: Any = None

    @classmethod
    def from_env(cls) -> Database:
        kind = checkpointer_kind()
        if kind == POSTGRES:
            return cls(POSTGRES, postgres_dsn())
        if kind == MEMORY:
            return cls(MEMORY)
        raise RuntimeError(f"Unknown CHECKPOINTER {kind!r}; expected 'memory' or 'postgres'.")

    @property
    def is_postgres(self) -> bool:
        return self.kind == POSTGRES

    async def open(self) -> None:
        if not self.is_postgres:
            return
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        self.pool = AsyncConnectionPool(
            conninfo=self.dsn or "",
            open=False,
            kwargs={"autocommit": True, "row_factory": dict_row},
        )
        await self.pool.open()
        async with self.pool.connection() as conn:
            await conn.execute(RUNS_DDL)
            await conn.execute(THREADS_DDL)

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

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


class RunStore:
    """Run records: `runs` table under postgres, an in-process dict under memory."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._memory: dict[str, RunRecord] = {}

    async def record(self, run: RunRecord) -> None:
        if not self.db.is_postgres:
            self._memory[run.run_id] = run
            return
        await self.db.execute(
            """
            INSERT INTO runs (run_id, thread_id, principal_hash, model, status, input_tokens,
                              output_tokens, latency_ms, error_type, payload, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (run_id) DO UPDATE SET
                status = EXCLUDED.status, input_tokens = EXCLUDED.input_tokens,
                output_tokens = EXCLUDED.output_tokens, latency_ms = EXCLUDED.latency_ms,
                error_type = EXCLUDED.error_type, payload = EXCLUDED.payload
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
                json.dumps(run.payload) if run.payload is not None else None,
                run.created_at,
            ),
        )

    async def get(self, run_id: str) -> RunRecord | None:
        if not self.db.is_postgres:
            return self._memory.get(run_id)
        row = await self.db.fetchone("SELECT * FROM runs WHERE run_id = %s", (run_id,))
        return _run_from_row(row) if row else None

    async def list_for_thread(self, thread_id: str) -> list[RunRecord]:
        if not self.db.is_postgres:
            return sorted(
                (r for r in self._memory.values() if r.thread_id == thread_id),
                key=lambda r: r.created_at,
            )
        rows = await self.db.fetchall(
            "SELECT * FROM runs WHERE thread_id = %s ORDER BY created_at", (thread_id,)
        )
        return [_run_from_row(r) for r in rows]


def _run_from_row(row: dict[str, Any]) -> RunRecord:
    created = row.get("created_at")
    payload = row.get("payload")
    if isinstance(payload, str):
        payload = json.loads(payload)
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
        payload=payload,
        created_at=created.isoformat() if isinstance(created, datetime) else str(created),
    )
