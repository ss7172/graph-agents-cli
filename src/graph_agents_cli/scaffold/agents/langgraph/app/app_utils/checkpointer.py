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

Outages are bounded on every connection the app opens (`connection_kwargs`):
a connection attempt gives up after `connect_timeout` (5 s), and TCP
keepalives on both ends notice a vanished peer in about a minute instead of the
operating system's two hours. A parameter already set in the DSN wins. While
the database is known to be unreachable (`DbHealth`), a request waits at most
`DOWN_POOL_TIMEOUT_S` for a connection instead of `POOL_TIMEOUT_S`, and the
pool retries in short cycles (`RECONNECT_TIMEOUT_S`), so requests fail fast
during an outage and the app is ready again seconds after the database is.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from functools import cache
from typing import Any

from {{cookiecutter.agent_directory}}.app_utils import metrics
from {{cookiecutter.agent_directory}}.app_utils.limits import SettingsError

logger = logging.getLogger(__name__)

MEMORY = "memory"
POSTGRES = "postgres"

# Advisory-lock key serialising schema setup across replicas (any fixed 64-bit value).
SCHEMA_LOCK_KEY = 0x6761_7363_6865_6D61  # "gaschema"
SCHEMA_LOCK_POLL_S = 0.25
SCHEMA_LOCK_TIMEOUT_S = 300.0

DB_POOL_MIN_SIZE = ("DB_POOL_MIN_SIZE", 1)
DB_POOL_MAX_SIZE = ("DB_POOL_MAX_SIZE", 10)
# Seconds a request waits for a free pooled connection before failing (503).
POOL_TIMEOUT_S = 5.0
# The same while the database is known to be unreachable: fail fast.
DOWN_POOL_TIMEOUT_S = 2.0
# A reconnect cycle of the pool gives up after this long; the next request
# starts a new one at once, instead of waiting out an ever longer back-off.
RECONNECT_TIMEOUT_S = 5.0
# Idle connections older than this are closed (keeps NAT/proxy reaping harmless).
POOL_MAX_IDLE_S = 300.0

# libpq parameters every connection gets unless the DSN sets them.
CONNECTION_DEFAULTS: dict[str, str] = {
    "connect_timeout": "5",
    "keepalives": "1",
    "keepalives_idle": "30",
    "keepalives_interval": "10",
    "keepalives_count": "3",
    # Unacknowledged data for this long (ms) means the server is gone (Linux).
    "tcp_user_timeout": "60000",
}
# Server-side keepalives, so Postgres ends the sessions of a replica that
# vanished without closing its connections (they hold connection slots).
SERVER_KEEPALIVE_SQL = (
    "SELECT set_config('tcp_keepalives_idle', '30', false), "
    "set_config('tcp_keepalives_interval', '10', false), "
    "set_config('tcp_keepalives_count', '3', false)"
)


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


def connection_kwargs(dsn: str) -> dict[str, str]:
    """The `CONNECTION_DEFAULTS` the DSN does not set itself.

    Raises `SettingsError` for a DSN that does not parse: a typo is a
    configuration error that stops startup, not an outage to wait out.
    """
    from psycopg import ProgrammingError
    from psycopg.conninfo import conninfo_to_dict

    try:
        given = conninfo_to_dict(dsn)
    except ProgrammingError as exc:
        raise SettingsError(f"The database URL does not parse: {exc}") from None
    defaults = dict(CONNECTION_DEFAULTS)
    if os.environ.get("PGCONNECT_TIMEOUT"):
        defaults.pop("connect_timeout")
    return {k: v for k, v in defaults.items() if k not in given}


def is_connection_error(exc: BaseException) -> bool:
    """True for errors that mean the database cannot be reached (not a failed statement)."""
    from psycopg import InterfaceError, OperationalError, errors
    from psycopg_pool import PoolTimeout

    if isinstance(exc, PoolTimeout | InterfaceError | OSError | TimeoutError):
        return True
    if isinstance(exc, errors.QueryCanceled | errors.TransactionRollback):
        return False
    return isinstance(exc, OperationalError)


class DbHealth:
    """Whether the database was reachable at last contact, logged once per change.

    Marked down when the pool times out while its connection attempts fail,
    when a pool reconnect cycle gives up, or when a connection breaks under a
    query; marked up by every connection handed out or opened.
    """

    def __init__(self) -> None:
        self.up = True
        self.changed_at = time.monotonic()
        self.last_error: str | None = None

    def mark_up(self) -> None:
        if not self.up:
            self.up = True
            self.changed_at = time.monotonic()
            metrics.observe_database(True)
            logger.info("database reachable again")

    def mark_down(self, reason: BaseException | str) -> None:
        text = reason if isinstance(reason, str) else f"{type(reason).__name__}: {reason}"
        self.last_error = text.strip().splitlines()[0][:300] if text.strip() else text
        if self.up:
            self.up = False
            self.changed_at = time.monotonic()
            metrics.observe_database(False)
            logger.warning("database unreachable: %s", self.last_error)

    def checkout_timeout(self) -> float:
        return POOL_TIMEOUT_S if self.up else DOWN_POOL_TIMEOUT_S


async def connect(dsn: str, *, health: DbHealth | None = None, **kwargs: Any) -> Any:
    """A direct autocommit connection with the `connection_kwargs` defaults.

    The server-side keepalives are set best effort (a proxy may refuse them).
    """
    import psycopg

    try:
        conn = await psycopg.AsyncConnection.connect(
            dsn, autocommit=True, **{**connection_kwargs(dsn), **kwargs}
        )
    except Exception as exc:
        if health is not None and is_connection_error(exc):
            health.mark_down(exc)
        raise
    await _server_keepalives(conn)
    if health is not None:
        health.mark_up()
    return conn


async def _server_keepalives(conn: Any) -> None:
    try:
        await conn.execute(SERVER_KEEPALIVE_SQL)
    except Exception as exc:  # not fatal: the connection works without them
        logger.debug("could not set the server-side TCP keepalives: %s", type(exc).__name__)


@cache
def _pool_class() -> type:
    """`AsyncConnectionPool` whose checkout timeout follows the database's health."""
    from psycopg_pool import AsyncConnectionPool, PoolTimeout

    class HealthAwarePool(AsyncConnectionPool):
        health: DbHealth

        async def getconn(self, timeout: float | None = None) -> Any:
            if timeout is None:
                timeout = self.health.checkout_timeout()
            errors_before = self.get_stats().get("connections_errors", 0)
            try:
                conn = await super().getconn(timeout)
            except PoolTimeout as exc:
                # Connection attempts failed while this request waited: the
                # database is down (a busy pool alone does not count).
                if self.get_stats().get("connections_errors", 0) > errors_before:
                    self.health.mark_down(exc)
                raise
            self.health.mark_up()
            return conn

    return HealthAwarePool


@asynccontextmanager
async def open_pool(dsn: str, health: DbHealth | None = None) -> AsyncIterator[Any]:
    """An open `AsyncConnectionPool` that health-checks every connection it hands out.

    Opening does not wait for the database: a pool opened during an outage
    connects once the database is back. A connection killed by a Postgres
    restart or failover fails the check and is replaced instead of failing the
    request that drew it. One dead connection usually means every idle one died
    with it, so the first failed check also sweeps the whole pool
    (`pool.check()`) in the background: the waiting request then gets a fresh
    connection on its next try instead of drawing, and backing off from, each
    dead one in turn.
    """
    from psycopg.rows import dict_row

    health = health if health is not None else DbHealth()
    extra = connection_kwargs(dsn)  # a DSN that does not parse stops here
    holder: dict[str, Any] = {}

    async def check(conn: Any) -> None:
        from psycopg_pool import AsyncConnectionPool

        try:
            await AsyncConnectionPool.check_connection(conn)
        except Exception:
            sweep = holder.get("sweep")
            if holder.get("pool") is not None and (sweep is None or sweep.done()):
                holder["sweep"] = asyncio.ensure_future(holder["pool"].check())
            raise

    async def configure(conn: Any) -> None:
        await _server_keepalives(conn)
        health.mark_up()

    async def reconnect_failed(pool: Any) -> None:
        health.mark_down(f"no connection for {RECONNECT_TIMEOUT_S:g} s")

    low, high = pool_sizes()
    pool = _pool_class()(
        conninfo=dsn,
        min_size=low,
        max_size=high,
        open=False,
        check=check,
        configure=configure,
        reconnect_failed=reconnect_failed,
        reconnect_timeout=RECONNECT_TIMEOUT_S,
        timeout=POOL_TIMEOUT_S,
        max_idle=POOL_MAX_IDLE_S,
        kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0, **extra},
    )
    pool.health = health
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
async def schema_lock(
    dsn: str, *, timeout: float = SCHEMA_LOCK_TIMEOUT_S, health: DbHealth | None = None
) -> AsyncIterator[None]:
    """Hold the schema advisory lock (on a connection of its own) for the block.

    Waiting replicas poll with `pg_try_advisory_lock` instead of blocking in
    `pg_advisory_lock`: a session blocked in a statement holds a snapshot, and
    the saver's `CREATE INDEX CONCURRENTLY` (run by the lock holder) waits for
    every such snapshot, so a blocking wait would deadlock the two.
    """
    async with await connect(dsn, health=health) as conn:
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


# A fence raises when this process may no longer write the thread's state
# (its run lease on the thread expired or was taken over).
Fence = Callable[[str], None]


@cache
def _fenced_saver_class() -> type:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    class FencedPostgresSaver(AsyncPostgresSaver):
        """`AsyncPostgresSaver` that asks the fence before every write.

        A run whose lease on its thread could not be renewed (another replica
        may have taken the thread over) then fails instead of writing a
        checkpoint beside, or over, the new owner's.
        """

        fence: Fence | None = None

        def _check(self, config: Any) -> None:
            if self.fence is not None:
                self.fence(str(config["configurable"]["thread_id"]))

        async def aput(self, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
            self._check(config)
            return await super().aput(config, checkpoint, metadata, new_versions)

        async def aput_writes(
            self, config: Any, writes: Any, task_id: str, task_path: str = ""
        ) -> None:
            self._check(config)
            await super().aput_writes(config, writes, task_id, task_path)

        async def adelete_thread(self, thread_id: str) -> None:
            if self.fence is not None:
                self.fence(str(thread_id))
            await super().adelete_thread(thread_id)

    return FencedPostgresSaver


def postgres_saver(pool: Any, fence: Fence | None = None) -> Any:
    """An `AsyncPostgresSaver` on `pool` whose writes pass `fence(thread_id)` first.

    Its schema is not set up here: call `saver.setup()` under `schema_lock`.
    """
    saver = _fenced_saver_class()(pool)
    saver.fence = fence
    return saver


@asynccontextmanager
async def get_checkpointer(pool: Any = None, fence: Fence | None = None) -> AsyncIterator[Any]:
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
        dsn = postgres_dsn()
        if pool is not None:
            saver = postgres_saver(pool, fence)
            async with schema_lock(dsn):
                await saver.setup()
            yield saver
            return
        async with open_pool(dsn) as own_pool:
            saver = postgres_saver(own_pool, fence)
            async with schema_lock(dsn):
                await saver.setup()
            yield saver
        return
    raise RuntimeError(f"Unknown CHECKPOINTER {kind!r}; expected 'memory' or 'postgres'.")
