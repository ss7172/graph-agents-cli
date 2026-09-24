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

"""One run per thread: in-process run locks, plus leases in Postgres that every replica honours.

A second run on a busy thread raises `ThreadBusy` (HTTP 409
`{"code": "thread_busy"}`) instead of racing the first one on the same
checkpoint. In-process the lock is a set of thread ids. With a Postgres
database (`dsn`) a replica also needs the thread's lease: a row of the locks
table (`thread_locks`, or `agent_thread_locks` under langgraph-server) naming
the holder (`owner`, unique per process), a fencing token that changes every
time the thread changes hands, and an expiry.

* The holder renews its leases every `RENEW_EVERY_S` seconds, pushing the
  expiry `LEASE_TTL_S` (30 s) ahead of the database clock. A replica that is
  lost without closing its connections (a node failure, a network partition,
  an OOM kill) stops renewing, and its threads are free again 30 s later.
* A lease is a row, not a session: a Postgres restart or failover, an admin
  killing the session or a proxy resetting it drops nothing, and another
  replica keeps getting 409 while the run goes on.
* The holder treats a lease as valid for `LOCAL_VALIDITY_S` after the last
  renewal it sent, well inside the expiry the database enforces. When a lease
  cannot be renewed in that time, or the renewal finds it taken over, the lease
  is lost: `on_lost` callbacks stop the run, and `fence(thread_id)` (called by
  the checkpointer before every write, see `checkpointer.postgres_saver`)
  refuses its writes, so a run that may no longer own its thread stops before
  it writes.
* A lease whose release fails (the database is down) is released again in
  the background; until then this process may take it back at once, and other
  replicas once it expires.

Deleting a thread and the retention purge take the same lease. Every lease
statement stands alone (no session state), on one connection per process.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import math
import os
import socket
import uuid
from collections.abc import Callable
from typing import Any

from {{cookiecutter.agent_directory}}.app_utils.checkpointer import DbHealth, connect
from {{cookiecutter.agent_directory}}.app_utils.db import LOCKS_TABLE

logger = logging.getLogger(__name__)

THREAD_BUSY = "thread_busy"

# How long a lease lives after its last renewal, by the database clock.
LEASE_TTL_S = 30.0
# How often the holder renews its leases.
RENEW_EVERY_S = 5.0
# How long after sending its last successful renewal the holder still writes.
# The margin to LEASE_TTL_S covers a write that waits for a connection and
# lands late: it still lands before any other replica can take the thread.
LOCAL_VALIDITY_S = 15.0
# Retry pace while renewals fail.
RETRY_EVERY_S = 1.0
# Bound on one lease statement (connecting included).
OP_TIMEOUT_S = 5.0


class ThreadBusy(Exception):
    """The thread already has a run in progress (HTTP 409 `thread_busy`)."""

    def __init__(self, thread_id: str) -> None:
        super().__init__("This thread already has a run in progress.")
        self.thread_id = thread_id


class LeaseLost(Exception):
    """This process may no longer write the thread: its run lease expired or changed hands."""

    def __init__(self, thread_id: str, reason: str) -> None:
        super().__init__(f"the run lease on the thread was lost: {reason}")
        self.thread_id = thread_id
        self.reason = reason


class ThreadLease:
    """A held run lock on one thread; `release()` is idempotent.

    `token` is the fencing token of the Postgres lease (None in-process).
    """

    def __init__(
        self, locks: ThreadLocks, thread_id: str, token: int | None, valid_until: float
    ) -> None:
        self._locks = locks
        self.thread_id = thread_id
        self.token = token
        self.valid_until = valid_until
        self.released = False
        self.lost = False
        self.lost_reason: str | None = None
        self._callbacks: list[Callable[[], Any]] = []
        self._holds: set[asyncio.Future[Any]] = set()
        self._release_deferred = False

    def hold_until(self, task: asyncio.Future[Any]) -> None:
        """Keep the thread locked until `task` is done, even once `release()` was called.

        A cancelled run can take a moment to stop (it waits for its in-flight
        writes): the thread stays busy until it has, so no new run overlaps
        it, while `check()` already refuses those writes.
        """
        if task.done():
            return
        self._holds.add(task)
        task.add_done_callback(self._hold_done)

    def _hold_done(self, task: asyncio.Future[Any]) -> None:
        self._holds.discard(task)
        if self._release_deferred and not self._holds:
            self._release_deferred = False
            self._locks._release_soon(self)

    def on_lost(self, callback: Callable[[], Any]) -> None:
        """Call `callback` once when the lease is lost (at once when it already is)."""
        if self.lost:
            callback()
        else:
            self._callbacks.append(callback)

    def check(self) -> None:
        """Raise `LeaseLost` unless this process still owns the thread."""
        if self.released:
            raise LeaseLost(self.thread_id, "the run already released it")
        if not self.lost and asyncio.get_running_loop().time() >= self.valid_until:
            self._lose("it could not be renewed in time")
        if self.lost:
            raise LeaseLost(self.thread_id, self.lost_reason or "lost")

    def _lose(self, reason: str) -> None:
        if self.lost or self.released:
            return
        self.lost = True
        self.lost_reason = reason
        logger.warning(
            "run lease lost; stopping the thread's run: %s",
            reason,
            extra={"thread_id": self.thread_id},
        )
        callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            try:
                callback()
            except Exception:
                logger.exception("a lease-lost callback failed")

    async def release(self) -> None:
        if self.released:
            return
        self.released = True
        self._callbacks.clear()
        if self._holds:
            self._release_deferred = True  # released when the last hold is done
            return
        await self._locks._release(self)


# Statements; the table name is a constant of `db.py`, never caller input.
_ACQUIRE = """
INSERT INTO {t} AS l (thread_id, owner, token, expires_at, acquired_at)
VALUES (%(thread)s, %(owner)s, nextval('{t}_token_seq'),
        now() + make_interval(secs => %(ttl)s), now())
ON CONFLICT (thread_id) DO UPDATE
   SET owner = EXCLUDED.owner, token = EXCLUDED.token,
       expires_at = EXCLUDED.expires_at, acquired_at = EXCLUDED.acquired_at
 WHERE l.expires_at < now() OR l.owner = %(owner)s
RETURNING token
"""
_RENEW = """
UPDATE {t} AS l SET expires_at = now() + make_interval(secs => %(ttl)s)
  FROM unnest(%(threads)s::text[], %(tokens)s::bigint[]) AS h(thread_id, token)
 WHERE l.thread_id = h.thread_id AND l.token = h.token AND l.owner = %(owner)s
RETURNING l.thread_id, l.token
"""
_RELEASE = (
    "DELETE FROM {t} WHERE thread_id = %(thread)s AND owner = %(owner)s AND token = %(token)s"
)
_RELEASE_ANY = "DELETE FROM {t} WHERE thread_id = %(thread)s AND owner = %(owner)s"
_RELEASE_ALL = "DELETE FROM {t} WHERE owner = %(owner)s"


def _owner_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


class ThreadLocks:
    """In-process run locks, plus Postgres leases when `dsn` is set (see the module doc)."""

    def __init__(
        self,
        dsn: str | None = None,
        *,
        table: str = LOCKS_TABLE,
        health: DbHealth | None = None,
        ttl_s: float | None = None,
        renew_every_s: float | None = None,
        validity_s: float | None = None,
    ) -> None:
        self.ttl_s = LEASE_TTL_S if ttl_s is None else ttl_s
        self.renew_every_s = RENEW_EVERY_S if renew_every_s is None else renew_every_s
        self.validity_s = LOCAL_VALIDITY_S if validity_s is None else validity_s
        if not 0 < self.validity_s < self.ttl_s:
            raise ValueError("validity_s must be positive and shorter than ttl_s")
        self.dsn = dsn
        self.table = table
        self.owner = _owner_id()
        self._health = health
        self._held: dict[str, ThreadLease] = {}
        self._pending: set[str] = set()
        # Threads whose lease row may still be ours after a failed release:
        # thread id -> token (None: whatever token we hold there).
        self._unreleased: dict[str, int | None] = {}
        self._conn: Any = None
        self._conn_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._releasing: set[asyncio.Future[None]] = set()
        self._failing = False
        self._closed = False

    @property
    def held(self) -> frozenset[str]:
        return frozenset(self._held)

    def lease(self, thread_id: str) -> ThreadLease | None:
        return self._held.get(thread_id)

    async def acquire(self, thread_id: str) -> ThreadLease:
        """Take the thread's run lock or raise `ThreadBusy`."""
        if self._closed:
            raise RuntimeError("the run locks are closed")
        if thread_id in self._held or thread_id in self._pending:
            raise ThreadBusy(thread_id)
        self._pending.add(thread_id)  # no await since the check: atomic in this process
        try:
            token: int | None = None
            valid_until = math.inf
            if self.dsn:
                sent = asyncio.get_running_loop().time()
                try:
                    rows = await self._execute(
                        _ACQUIRE, {"thread": thread_id, "owner": self.owner, "ttl": self.ttl_s}
                    )
                except BaseException:
                    # The row may have been written before the failure: clean it up later.
                    self._unreleased.setdefault(thread_id, None)
                    self._ensure_heartbeat()
                    raise
                if not rows:
                    raise ThreadBusy(thread_id)
                token = int(rows[0][0])
                valid_until = sent + self.validity_s
                self._unreleased.pop(thread_id, None)  # taken over by this lease
            lease = ThreadLease(self, thread_id, token, valid_until)
            self._held[thread_id] = lease
        finally:
            self._pending.discard(thread_id)
        if self.dsn:
            self._ensure_heartbeat()
        return lease

    def fence(self, thread_id: str) -> None:
        """Raise `LeaseLost` unless this process holds a valid lease on `thread_id`.

        The checkpointer calls it before every write of the thread's state.
        """
        lease = self._held.get(thread_id)
        if lease is None:
            raise LeaseLost(thread_id, "this process holds no run lease on the thread")
        lease.check()

    def _release_soon(self, lease: ThreadLease) -> None:
        """Release `lease` in a task of its own (from a callback, which cannot await)."""
        task = asyncio.ensure_future(self._release(lease))
        self._releasing.add(task)
        task.add_done_callback(self._releasing.discard)

    async def _release(self, lease: ThreadLease) -> None:
        if self._held.get(lease.thread_id) is lease:
            del self._held[lease.thread_id]
        if not self.dsn or lease.token is None or self._closed:
            return
        params = {"thread": lease.thread_id, "owner": self.owner, "token": lease.token}
        try:
            if self._health is not None and not self._health.up:
                raise ConnectionError("the database is known to be down")
            await self._execute(_RELEASE, params, fetch=False, timeout=min(OP_TIMEOUT_S, 2.0))
        except Exception as exc:
            # Released again by the heartbeat; this process can take it back
            # meanwhile, other replicas once it expires.
            logger.warning(
                "could not release a run lease (%s); retrying in the background",
                type(exc).__name__,
                extra={"thread_id": lease.thread_id},
            )
            if lease.thread_id not in self._held:
                self._unreleased[lease.thread_id] = lease.token
            self._ensure_heartbeat()

    async def close(self) -> None:
        """Stop renewing and give back every lease of this process (best effort)."""
        self._closed = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        had_leases = bool(self._held or self._unreleased)
        for lease in self._held.values():
            lease.released = True
        self._held.clear()
        self._unreleased.clear()
        if self.dsn and had_leases:
            try:
                await self._execute(_RELEASE_ALL, {"owner": self.owner}, fetch=False, timeout=2.0)
            except Exception:
                logger.debug("could not give back the run leases at shutdown", exc_info=True)
        await self._reset()

    # -- heartbeat ------------------------------------------------------------

    def _ensure_heartbeat(self) -> None:
        if self._closed or not self.dsn:
            return
        if self._task is None or self._task.done():
            # A context of its own: not the log context of the request that started it.
            self._task = asyncio.create_task(
                self._heartbeat(), name="run-lease-heartbeat", context=contextvars.Context()
            )

    async def _heartbeat(self) -> None:
        loop = asyncio.get_running_loop()
        delay = RETRY_EVERY_S if self._unreleased else self.renew_every_s
        while not self._closed:
            await asyncio.sleep(delay)
            delay = self.renew_every_s
            try:
                flushed = await self._flush_releases()
                renewed = await self._renew()
                if self._failing and (flushed or renewed):
                    self._failing = False
                    logger.info("run leases reachable again")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                delay = RETRY_EVERY_S
                if not self._failing:
                    self._failing = True
                    logger.warning("could not renew the run leases: %s", type(exc).__name__)
            now = loop.time()
            for lease in list(self._held.values()):
                if lease.token is not None and not lease.lost and now >= lease.valid_until:
                    lease._lose("it could not be renewed in time")
            if not self._held and not self._unreleased:
                self._task = None
                return  # started again by the next acquire

    async def _renew(self) -> bool:
        """Renew every live lease in one statement; False when there was none to renew."""
        leases = [
            lease
            for lease in self._held.values()
            if lease.token is not None and not lease.lost and not lease.released
        ]
        if not leases:
            return False
        sent = asyncio.get_running_loop().time()
        rows = await self._execute(
            _RENEW,
            {
                "ttl": self.ttl_s,
                "owner": self.owner,
                "threads": [lease.thread_id for lease in leases],
                "tokens": [lease.token for lease in leases],
            },
        )
        renewed = {(str(r[0]), int(r[1])) for r in rows}
        for lease in leases:
            if (lease.thread_id, lease.token) in renewed:
                lease.valid_until = max(lease.valid_until, sent + self.validity_s)
            elif not lease.released:
                lease._lose("another replica took the thread over, or its lease row is gone")
        return True

    async def _flush_releases(self) -> bool:
        """Retry the releases that failed; False when there was none."""
        flushed = False
        for thread_id, token in list(self._unreleased.items()):
            if thread_id in self._held or thread_id in self._pending:
                self._unreleased.pop(thread_id, None)  # ours again: nothing to give back
                continue
            params: dict[str, Any] = {"thread": thread_id, "owner": self.owner}
            if token is None:
                await self._execute(_RELEASE_ANY, params, fetch=False)
            else:
                await self._execute(_RELEASE, {**params, "token": token}, fetch=False)
            self._unreleased.pop(thread_id, None)
            flushed = True
        return flushed

    # -- Postgres -----------------------------------------------------------------

    async def _execute(
        self, sql: str, params: dict[str, Any], *, fetch: bool = True, timeout: float = OP_TIMEOUT_S
    ) -> list[Any]:
        """Run one lease statement on the lease connection (reconnecting once), bounded."""
        statement = sql.format(t=self.table)
        async with asyncio.timeout(timeout), self._conn_lock:
            for attempt in (1, 2):
                conn = await self._connection()
                try:
                    cur = await conn.execute(statement, params)
                    return list(await cur.fetchall()) if fetch else []
                except Exception:
                    await self._reset_locked()
                    if attempt == 2:
                        raise
                except BaseException:
                    # Cancelled mid-statement: the connection's state is unknown.
                    await self._reset_locked()
                    raise
        return []

    async def _connection(self) -> Any:
        if self._conn is not None and not self._conn.closed:
            return self._conn
        self._conn = await connect(self.dsn or "", health=self._health)
        return self._conn

    async def _reset(self) -> None:
        async with self._conn_lock:
            await self._reset_locked()

    async def _reset_locked(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                logger.debug("closing the lease connection failed", exc_info=True)
