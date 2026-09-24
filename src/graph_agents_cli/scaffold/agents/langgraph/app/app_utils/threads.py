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

"""App-owned thread ownership (one principal per thread) and per-thread run locks.

Under `fastapi` the `threads` table (`thread_id`, `principal_id`, `tenant`,
`created_at`, `updated_at`) lives beside the library-owned checkpointer
schema; the ownership check runs before any checkpointer access, so a thread
id alone cannot cross a principal boundary. Claiming a new thread is one
atomic insert-if-absent, so two principals racing for the same new id cannot
both win. Under `langgraph-server` the same rule is applied by `chat.py` to
the thread metadata `{principal_id, tenant}` the app writes at creation (the
server's own `@auth.on` filters in `auth.py` protect the native Threads/Runs
API called from outside; the app's loopback SDK calls bypass them).

Only the owner may continue (write to) or delete a thread. Roles listed in
`AUTH_READ_ACROSS_ROLES` may additionally *read* other principals' threads.

One run per thread (`ThreadLocks`, `ThreadBusy`: HTTP 409 `{"code":
"thread_busy"}`) lives in `run_locks.py`: in-process locks plus Postgres
leases that every replica honours; the names are re-exported here.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from fastapi import HTTPException

from {{cookiecutter.agent_directory}}.app_utils.auth import Principal
from {{cookiecutter.agent_directory}}.app_utils.db import Database, utcnow_iso
from {{cookiecutter.agent_directory}}.app_utils.run_locks import (
    THREAD_BUSY,
    LeaseLost,
    ThreadBusy,
    ThreadLease,
    ThreadLocks,
)

__all__ = [
    "THREAD_BUSY",
    "LeaseLost",
    "ThreadBusy",
    "ThreadLease",
    "ThreadLocks",
    "ThreadRecord",
    "ThreadStore",
]

logger = logging.getLogger(__name__)


@dataclass
class ThreadRecord:
    thread_id: str
    principal_id: str
    tenant: str | None = None
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str | None = None

    def __post_init__(self) -> None:
        if self.updated_at is None:
            self.updated_at = self.created_at

    def public(self) -> dict[str, Any]:
        """What `GET /threads` returns (no principal id: it may be an email address)."""
        return {
            "thread_id": self.thread_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def read_across_roles() -> set[str]:
    """Roles allowed to read other principals' threads (`AUTH_READ_ACROSS_ROLES`)."""
    raw = os.environ.get("AUTH_READ_ACROSS_ROLES", "")
    return {r.strip() for r in raw.split(",") if r.strip()}


def reads_across(principal: Principal) -> bool:
    return bool(set(principal.roles) & read_across_roles())


def is_owner(principal: Principal, record: ThreadRecord) -> bool:
    return record.principal_id == principal.id


def can_access(principal: Principal, record: ThreadRecord) -> bool:
    """Owner, or a principal holding one of the read-across roles."""
    return is_owner(principal, record) or reads_across(principal)


def assert_access(principal: Principal, record: ThreadRecord) -> None:
    """Read access: the owner or a read-across role."""
    if not can_access(principal, record):
        raise HTTPException(status_code=403, detail="This thread belongs to another principal.")


def assert_owner(principal: Principal, record: ThreadRecord) -> None:
    """Write access (continue, delete): the owner only; read-across roles are read-only."""
    if not is_owner(principal, record):
        raise HTTPException(status_code=403, detail="This thread belongs to another principal.")


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _record_from_row(row: dict[str, Any]) -> ThreadRecord:
    return ThreadRecord(
        thread_id=row["thread_id"],
        principal_id=row["principal_id"],
        tenant=row.get("tenant"),
        created_at=_iso(row.get("created_at")) or utcnow_iso(),
        updated_at=_iso(row.get("updated_at")),
    )


class ThreadStore:
    """`threads` table under postgres, an in-process dict under memory."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._memory: dict[str, ThreadRecord] = {}

    async def get(self, thread_id: str) -> ThreadRecord | None:
        if not self.db.is_postgres:
            return self._memory.get(thread_id)
        row = await self.db.fetchone("SELECT * FROM threads WHERE thread_id = %s", (thread_id,))
        return _record_from_row(row) if row is not None else None

    async def claim(self, thread_id: str, principal: Principal) -> tuple[ThreadRecord, bool]:
        """The thread's record, created for `principal` if absent; `True` when this call created it.

        One atomic insert-if-absent: when two principals claim the same new
        id at once, exactly one gets `created=True` and the other reads the
        winner's record (and is refused by the ownership check).
        """
        record = ThreadRecord(
            thread_id=thread_id,
            principal_id=principal.id,
            tenant=principal.public_attributes().get("tenant"),
        )
        if not self.db.is_postgres:
            existing = self._memory.setdefault(thread_id, record)
            return existing, existing is record
        for _ in range(3):
            row = await self.db.fetchone(
                """
                INSERT INTO threads (thread_id, principal_id, tenant, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s) ON CONFLICT (thread_id) DO NOTHING
                RETURNING *
                """,
                (
                    record.thread_id,
                    record.principal_id,
                    record.tenant,
                    record.created_at,
                    record.created_at,
                ),
            )
            if row is not None:
                return _record_from_row(row), True
            existing = await self.get(thread_id)
            if existing is not None:
                return existing, False
            # Deleted between the conflict and the read: claim it again.
        raise RuntimeError(f"could not claim thread {thread_id!r}")

    async def create(self, thread_id: str, principal: Principal) -> ThreadRecord:
        record, _created = await self.claim(thread_id, principal)
        return record

    async def ensure(
        self,
        thread_id: str,
        principal: Principal,
        *,
        has_state: Callable[[str], Awaitable[bool]] | None = None,
    ) -> ThreadRecord:
        """Return the thread, creating it for `principal` when it does not exist.

        This is the write path (`chat.send`): raises 403 when the thread exists
        and belongs to another principal, even for a read-across role. A
        continued thread's `updated_at` moves forward (retention counts idle time).

        `has_state(thread_id)` tells whether the checkpointer holds state for
        the id. An id with state but no owner row is never claimed (403): its
        state belongs to whoever had it, not to the next caller. Rows are
        removed only after their state, under the run lock, so such an id
        should never exist; this is the guard if one does.
        """
        record = await self.get(thread_id)
        if record is None:
            if has_state is not None and await has_state(thread_id):
                logger.warning(
                    "thread has state but no owner; refusing to give it a new owner",
                    extra={"thread_id": thread_id},
                )
                raise HTTPException(
                    status_code=403, detail="This thread belongs to another principal."
                )
            record, created = await self.claim(thread_id, principal)
            if created:
                return record
        assert_owner(principal, record)
        await self.touch(thread_id)
        return record

    async def touch(self, thread_id: str) -> None:
        now = utcnow_iso()
        if not self.db.is_postgres:
            if thread_id in self._memory:
                self._memory[thread_id].updated_at = now
            return
        await self.db.execute(
            "UPDATE threads SET updated_at = %s WHERE thread_id = %s", (now, thread_id)
        )

    async def list_for(
        self, principal: Principal, *, limit: int = 1000, offset: int = 0
    ) -> list[ThreadRecord]:
        """The principal's threads (every thread for a read-across role), most recent first."""
        if not self.db.is_postgres:
            records = sorted(
                (r for r in self._memory.values() if can_access(principal, r)),
                key=lambda r: (r.updated_at or "", r.thread_id),
                reverse=True,
            )
            return records[offset : offset + limit]
        if reads_across(principal):
            rows = await self.db.fetchall(
                "SELECT * FROM threads ORDER BY updated_at DESC, thread_id DESC LIMIT %s OFFSET %s",
                (limit, offset),
            )
        else:
            rows = await self.db.fetchall(
                "SELECT * FROM threads WHERE principal_id = %s "
                "ORDER BY updated_at DESC, thread_id DESC LIMIT %s OFFSET %s",
                (principal.id, limit, offset),
            )
        return [_record_from_row(r) for r in rows]

    async def idle_before(self, cutoff_iso: str, *, limit: int = 500) -> list[str]:
        """Ids of threads not continued since `cutoff_iso` (oldest first)."""
        if not self.db.is_postgres:
            idle = sorted(
                (r for r in self._memory.values() if (r.updated_at or r.created_at) < cutoff_iso),
                key=lambda r: r.updated_at or r.created_at,
            )
            return [r.thread_id for r in idle[:limit]]
        rows = await self.db.fetchall(
            "SELECT thread_id FROM threads WHERE updated_at < %s ORDER BY updated_at LIMIT %s",
            (cutoff_iso, limit),
        )
        return [r["thread_id"] for r in rows]

    async def delete(self, thread_id: str) -> None:
        if not self.db.is_postgres:
            self._memory.pop(thread_id, None)
            return
        await self.db.execute("DELETE FROM threads WHERE thread_id = %s", (thread_id,))
