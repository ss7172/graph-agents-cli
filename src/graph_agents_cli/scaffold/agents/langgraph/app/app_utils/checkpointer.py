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

"""Checkpointer selection for the fastapi runtime (memory or postgres), and the Postgres pool.

`CHECKPOINTER=memory` gives an `InMemorySaver` (no database, state lost on
restart); `CHECKPOINTER=postgres` gives an `AsyncPostgresSaver` on
`POSTGRES_DSN` with its schema set up. Under `langgraph-server` the server owns
persistence through `DATABASE_URI`/`REDIS_URI` and `agent.py` always exports
an unbound graph.

Pools (`open_pool`) check each connection before handing it out and replace
dead ones, so a Postgres restart or failover costs no failed requests once the
database is back. Size them with `DB_POOL_MIN_SIZE` (default 1) and
`DB_POOL_MAX_SIZE` (default 10); the fastapi runtime shares one pool between
the checkpointer and the app tables. Schema setup (the saver's migrations and
the app's DDL) runs under a Postgres advisory lock (`schema_lock`), so
replicas starting together against a fresh database take turns.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

from {{cookiecutter.agent_directory}}.app_utils.limits import SettingsError

MEMORY = "memory"
POSTGRES = "postgres"

# Advisory-lock key serialising schema setup across replicas (any fixed 64-bit value).
SCHEMA_LOCK_KEY = 0x6761_7363_6865_6D61  # "gaschema"
SCHEMA_LOCK_POLL_S = 0.25
SCHEMA_LOCK_TIMEOUT_S = 300.0

DB_POOL_MIN_SIZE = ("DB_POOL_MIN_SIZE", 1)
DB_POOL_MAX_SIZE = ("DB_POOL_MAX_SIZE", 10)
# Seconds a request waits for a free pooled connection before failing.
POOL_TIMEOUT_S = 10.0
# Idle connections older than this are closed (keeps NAT/proxy reaping harmless).
POOL_MAX_IDLE_S = 300.0


def checkpointer_kind() -> str:
    """`memory` (default) or `postgres`, from `CHECKPOINTER`."""
    return (os.environ.get("CHECKPOINTER") or MEMORY).strip().lower()


def postgres_dsn() -> str:
    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise RuntimeError(
            "CHECKPOINTER=postgres requires POSTGRES_DSN (from the Secret or the chart)."
        )
    return dsn


def _size(spec: tuple[str, int]) -> int:
    name, default = spec
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(f"{name}={raw!r} is not an integer.") from None
    if value < 0:
        raise SettingsError(f"{name}={value} must be >= 0.")
    return value


def pool_sizes() -> tuple[int, int]:
    """`(DB_POOL_MIN_SIZE, DB_POOL_MAX_SIZE)`, validated."""
    low, high = _size(DB_POOL_MIN_SIZE), _size(DB_POOL_MAX_SIZE)
    if high < 1 or low > high:
        raise SettingsError(f"DB_POOL_MAX_SIZE={high} must be >= 1 and >= DB_POOL_MIN_SIZE={low}.")
    return low, high


@asynccontextmanager
async def open_pool(dsn: str) -> AsyncIterator[Any]:
    """An open `AsyncConnectionPool` that health-checks every connection it hands out.

    A connection killed by a Postgres restart or failover fails the check and
    is replaced instead of failing the request that drew it. One dead
    connection usually means every idle one died with it, so the first failed
    check also sweeps the whole pool (`pool.check()`) in the background: the
    waiting request then gets a fresh connection on its next try (about a
    second) instead of drawing, and backing off from, each dead one in turn.
    """
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    holder: dict[str, Any] = {}

    async def check(conn: Any) -> None:
        try:
            await AsyncConnectionPool.check_connection(conn)
        except Exception:
            sweep = holder.get("sweep")
            if holder.get("pool") is not None and (sweep is None or sweep.done()):
                holder["sweep"] = asyncio.ensure_future(holder["pool"].check())
            raise

    low, high = pool_sizes()
    pool: AsyncConnectionPool = AsyncConnectionPool(
        conninfo=dsn,
        min_size=low,
        max_size=high,
        open=False,
        check=check,
        timeout=POOL_TIMEOUT_S,
        max_idle=POOL_MAX_IDLE_S,
        kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0},
    )
    holder["pool"] = pool
    await pool.open()
    try:
        yield pool
    finally:
        holder.pop("pool", None)
        sweep = holder.pop("sweep", None)
        if sweep is not None and not sweep.done():
            sweep.cancel()
        await pool.close()


@asynccontextmanager
async def schema_lock(dsn: str, *, timeout: float = SCHEMA_LOCK_TIMEOUT_S) -> AsyncIterator[None]:
    """Hold the schema advisory lock (on a connection of its own) for the block.

    Waiting replicas poll with `pg_try_advisory_lock` instead of blocking in
    `pg_advisory_lock`: a session blocked in a statement holds a snapshot, and
    the saver's `CREATE INDEX CONCURRENTLY` (run by the lock holder) waits for
    every such snapshot, so a blocking wait would deadlock the two.
    """
    import psycopg

    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            cur = await conn.execute("SELECT pg_try_advisory_lock(%s)", (SCHEMA_LOCK_KEY,))
            row = await cur.fetchone()
            if row and row[0]:
                break
            if loop.time() >= deadline:
                raise TimeoutError(
                    f"another replica held the schema setup lock for over {timeout:g} s"
                )
            await asyncio.sleep(SCHEMA_LOCK_POLL_S)
        try:
            yield
        finally:
            # Closing the connection releases the lock too; this just makes it prompt.
            with suppress(Exception):
                await conn.execute("SELECT pg_advisory_unlock(%s)", (SCHEMA_LOCK_KEY,))


@asynccontextmanager
async def get_checkpointer(pool: Any = None) -> AsyncIterator[Any]:
    """Yield a ready checkpointer for the configured kind.

    Postgres uses `pool` when given (the app's shared pool), else a pool of
    its own, and runs `setup()` (idempotent) under the schema lock so the
    checkpoint tables exist before the first run.
    """
    kind = checkpointer_kind()
    if kind == MEMORY:
        from langgraph.checkpoint.memory import InMemorySaver

        yield InMemorySaver()
        return
    if kind == POSTGRES:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        dsn = postgres_dsn()
        if pool is not None:
            saver = AsyncPostgresSaver(pool)
            async with schema_lock(dsn):
                await saver.setup()
            yield saver
            return
        async with open_pool(dsn) as own_pool:
            saver = AsyncPostgresSaver(own_pool)
            async with schema_lock(dsn):
                await saver.setup()
            yield saver
        return
    raise RuntimeError(f"Unknown CHECKPOINTER {kind!r}; expected 'memory' or 'postgres'.")
