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

"""App-owned thread ownership (DECISIONS.md D23).

Under `fastapi` the `threads` table (`thread_id`, `principal_id`, `tenant`,
`created_at`) lives beside the library-owned checkpointer schema; the
ownership check runs before any checkpointer access, so a thread id alone
cannot cross a principal boundary. Under `langgraph-server` the same rule is
applied by `chat.py` to the thread metadata `{principal_id, tenant}` the app
writes at creation (the server's own `@auth.on` filters in `auth.py` protect
the native Threads/Runs API called from outside; the app's loopback SDK calls
bypass them).

Only the owner may continue (write to) or delete a thread. Roles listed in
`AUTH_READ_ACROSS_ROLES` may additionally *read* other principals' threads.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime

from fastapi import HTTPException

from {{cookiecutter.agent_directory}}.app_utils.auth import Principal
from {{cookiecutter.agent_directory}}.app_utils.db import Database, utcnow_iso


@dataclass
class ThreadRecord:
    thread_id: str
    principal_id: str
    tenant: str | None = None
    created_at: str = field(default_factory=utcnow_iso)


def read_across_roles() -> set[str]:
    """Roles allowed to read other principals' threads (`AUTH_READ_ACROSS_ROLES`)."""
    raw = os.environ.get("AUTH_READ_ACROSS_ROLES", "")
    return {r.strip() for r in raw.split(",") if r.strip()}


def is_owner(principal: Principal, record: ThreadRecord) -> bool:
    return record.principal_id == principal.id


def can_access(principal: Principal, record: ThreadRecord) -> bool:
    """Owner, or a principal holding one of the read-across roles."""
    return is_owner(principal, record) or bool(set(principal.roles) & read_across_roles())


def assert_access(principal: Principal, record: ThreadRecord) -> None:
    """Read access: the owner or a read-across role."""
    if not can_access(principal, record):
        raise HTTPException(status_code=403, detail="This thread belongs to another principal.")


def assert_owner(principal: Principal, record: ThreadRecord) -> None:
    """Write access (continue, delete): the owner only; read-across roles are read-only."""
    if not is_owner(principal, record):
        raise HTTPException(status_code=403, detail="This thread belongs to another principal.")


class ThreadStore:
    """`threads` table under postgres, an in-process dict under memory."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._memory: dict[str, ThreadRecord] = {}

    async def get(self, thread_id: str) -> ThreadRecord | None:
        if not self.db.is_postgres:
            return self._memory.get(thread_id)
        row = await self.db.fetchone("SELECT * FROM threads WHERE thread_id = %s", (thread_id,))
        if row is None:
            return None
        created = row.get("created_at")
        return ThreadRecord(
            thread_id=row["thread_id"],
            principal_id=row["principal_id"],
            tenant=row.get("tenant"),
            created_at=created.isoformat() if isinstance(created, datetime) else str(created),
        )

    async def create(self, thread_id: str, principal: Principal) -> ThreadRecord:
        record = ThreadRecord(
            thread_id=thread_id,
            principal_id=principal.id,
            tenant=principal.attributes.get("tenant"),
        )
        if not self.db.is_postgres:
            self._memory[thread_id] = record
            return record
        await self.db.execute(
            """
            INSERT INTO threads (thread_id, principal_id, tenant, created_at)
            VALUES (%s, %s, %s, %s) ON CONFLICT (thread_id) DO NOTHING
            """,
            (record.thread_id, record.principal_id, record.tenant, record.created_at),
        )
        return record

    async def ensure(self, thread_id: str, principal: Principal) -> ThreadRecord:
        """Return the thread, creating it for `principal` when it does not exist.

        This is the write path (`chat.send`): raises 403 when the thread exists
        and belongs to another principal, even for a read-across role.
        """
        record = await self.get(thread_id)
        if record is None:
            return await self.create(thread_id, principal)
        assert_owner(principal, record)
        return record

    async def list_for(self, principal: Principal) -> list[ThreadRecord]:
        if not self.db.is_postgres:
            return sorted(
                (r for r in self._memory.values() if can_access(principal, r)),
                key=lambda r: r.created_at,
            )
        if set(principal.roles) & read_across_roles():
            rows = await self.db.fetchall("SELECT * FROM threads ORDER BY created_at")
        else:
            rows = await self.db.fetchall(
                "SELECT * FROM threads WHERE principal_id = %s ORDER BY created_at", (principal.id,)
            )
        return [
            ThreadRecord(
                r["thread_id"], r["principal_id"], r.get("tenant"), str(r.get("created_at"))
            )
            for r in rows
        ]

    async def delete(self, thread_id: str) -> None:
        if not self.db.is_postgres:
            self._memory.pop(thread_id, None)
            return
        await self.db.execute("DELETE FROM threads WHERE thread_id = %s", (thread_id,))
