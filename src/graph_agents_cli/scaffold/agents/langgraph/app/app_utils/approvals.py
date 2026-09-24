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

"""Human approval of gated outbound API calls: the records, who decides, the ledger.

An API's `approval` block in `api-policy.yaml` gates calls; `api_client.py`
pauses the run before a gated call is sent, with an interrupt whose value
describes the exact request. When a run ends paused that way, the chat
runtime (`chat.py`) records one pending approval per interrupt here
(`ApprovalStore.add`) and ends the stream with `message.end` status
`awaiting_approval` and the approval (`ApprovalRecord.public()`).

Deciding is the action `approval.decide` of the auth policy, then this rule
(`may_decide`): `requester` in `approvers` lets the principal who started the
run (the thread's owner: only the owner runs on a thread) decide; `role:<x>`
lets any principal holding role `x` decide, except the requester, who may
approve their own call only when `requester` is listed. Anyone else gets 403.
A decision is one atomic change of a pending, unexpired row
(`ApprovalStore.decide`), so of two concurrent decisions one wins and the
other finds the approval no longer pending. The run then resumes with the
decision; the tool runs again, and `api_client` sends the call only when the
decision approves exactly the request it rebuilds (the same `call_hash`) and
`ApprovalStore.consume` (the ledger) marks the approval used: once, on the
thread it was made for. A tool call that runs again without a decision (a run
continued or replayed through LangGraph Server's own API) asks the ledger
first (`ApprovalStore.bound_approvals`) and sends no call an approval was
asked for.

Records (table `approvals`, `agent_approvals` under langgraph-server, in the
app's database; in process memory without one): the thread, the run that
paused, the interrupt, the requester's hashed id and the roles and public
attributes the run acted with (never credentials), the tool call that asked
(the model message and its call id), the call (API, method, path, operation
id, `call_hash`, and in `payload` the query and JSON body as the approver sees
them, with the fields the tool named in `redact=` masked),
the approvers, the status (`pending`, `approved`, `rejected`, `expired`), the
decider's hashed id, the decision time and comment, when the approval was
used, and when it expires (`approval.timeout_s` after it was requested). Once
decided or expired the query and body are dropped from the record unless
`TRACE_CAPTURE=full`. A pending approval past its expiry is expired (=
rejected): the runtime's sweep marks it every `SWEEP_INTERVAL_S` seconds and
every read treats it so. Deleting a thread deletes its approvals.

Who sees an approval (`may_view`): the thread's owner, a principal who may
decide it, and roles in `AUTH_READ_ACROSS_ROLES` (listing only; they see the
query and body only under `TRACE_CAPTURE=full`).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from {{cookiecutter.agent_directory}}.app_utils.api_client import (
    APPROVAL_DECISION,
    APPROVAL_INTERRUPT,
    DEFAULT_APPROVAL_TIMEOUT_S,
    MAX_APPROVAL_TIMEOUT_S,
    MIN_APPROVAL_TIMEOUT_S,
    REQUESTER_APPROVER,
    ROLE_APPROVER_PREFIX,
    BoundApproval,
)
from {{cookiecutter.agent_directory}}.app_utils.auth import Principal, read_across_roles
from {{cookiecutter.agent_directory}}.app_utils.db import Database, capture_full

logger = logging.getLogger(__name__)

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
EXPIRED = "expired"
STATUSES = (PENDING, APPROVED, REJECTED, EXPIRED)

# Decisions a decider sends (`POST /threads/{id}/approvals/{approval_id}`).
APPROVE = "approve"
REJECT = "reject"
DECISIONS = {APPROVE: APPROVED, REJECT: REJECTED}

# Error codes clients see.
CODE_APPROVAL_PENDING = "approval_pending"  # 409 on /chat while an approval is pending
CODE_NOT_PENDING = "approval_not_pending"  # 409 on a decision: already decided
CODE_EXPIRED = "approval_expired"  # 410 on a decision: it expired

# How often every replica marks pending approvals past their expiry `expired`.
SWEEP_INTERVAL_S = 30.0
# Approvals kept without a database (the oldest decided ones go first).
MEMORY_APPROVALS_CAP = 10_000
COMMENT_MAX_CHARS = 1000
# Payload keys dropped once an approval is decided or expired (unless TRACE_CAPTURE=full).
CALL_CONTENT_KEYS = ("query", "body")


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _json(value: Any) -> str:
    return json.dumps(value, default=str)


def _decode(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def is_approval_interrupt(value: Any) -> bool:
    """Whether an interrupt value is a gated API call's (raised by `api_client`)."""
    return isinstance(value, Mapping) and value.get("type") == APPROVAL_INTERRUPT


@dataclass
class ApprovalRecord:
    approval_id: str
    thread_id: str
    run_id: str
    interrupt_id: str
    requester_hash: str
    api: str
    method: str
    path: str
    call_hash: str
    approvers: list[str]
    payload: dict[str, Any]
    expires_at: datetime
    operation_id: str | None = None
    tool_call_id: str | None = None
    # The model message that made the tool call (with `tool_call_id`, the tool call).
    message_id: str | None = None
    requester_context: dict[str, Any] = field(default_factory=dict)
    status: str = PENDING
    decided_by: str | None = None
    decided_at: datetime | None = None
    comment: str | None = None
    used_at: datetime | None = None
    created_at: datetime = field(default_factory=utcnow)

    def effective_status(self, now: datetime | None = None) -> str:
        """The status, with a pending approval past its expiry read as `expired`."""
        if self.status == PENDING and self.expires_at <= (now or utcnow()):
            return EXPIRED
        return self.status

    def is_pending(self, now: datetime | None = None) -> bool:
        return self.effective_status(now) == PENDING

    def public(self, *, include_call: bool = True, now: datetime | None = None) -> dict[str, Any]:
        """The approval as clients see it (never the call hash, the interrupt or the rule).

        `query` and `body` are there when `include_call` and the record still
        holds them (pending, or `TRACE_CAPTURE=full`).
        """
        out: dict[str, Any] = {
            "approval_id": self.approval_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "status": self.effective_status(now),
            "api": self.api,
            "method": self.method,
            "path": self.path,
            "operation_id": self.operation_id,
        }
        if include_call:
            for key in CALL_CONTENT_KEYS:
                if key in self.payload:
                    out[key] = self.payload[key]
        out.update(
            {
                "tool": self.payload.get("tool"),
                "reason": self.payload.get("reason"),
                "approvers": list(self.approvers),
                "requester": self.requester_hash,
                "created_at": _iso(self.created_at),
                "expires_at": _iso(self.expires_at),
                "decided_by": self.decided_by,
                "decided_at": _iso(self.decided_at),
                "comment": self.comment,
            }
        )
        return out


def record_from_interrupt(
    value: Mapping[str, Any],
    *,
    interrupt_id: str,
    thread_id: str,
    run_id: str,
    requester: Principal,
    now: datetime | None = None,
) -> ApprovalRecord:
    """A pending approval for the interrupt a gated call raised (`is_approval_interrupt`)."""
    now = now or utcnow()
    raw_timeout = value.get("timeout_s")
    timeout = (
        raw_timeout
        if isinstance(raw_timeout, int) and not isinstance(raw_timeout, bool)
        else DEFAULT_APPROVAL_TIMEOUT_S
    )
    timeout = min(max(timeout, MIN_APPROVAL_TIMEOUT_S), MAX_APPROVAL_TIMEOUT_S)
    approvers = [str(a) for a in value.get("approvers") or [] if isinstance(a, str)]
    payload = {
        "query": value.get("query") or {},
        "body": value.get("body"),
        "tool": value.get("tool"),
        "reason": value.get("reason"),
    }
    return ApprovalRecord(
        approval_id=uuid.uuid4().hex,
        thread_id=thread_id,
        run_id=run_id,
        interrupt_id=str(interrupt_id),
        requester_hash=requester.hashed_id(),
        requester_context={
            "roles": [str(r) for r in requester.roles],
            "attributes": requester.public_attributes(),
        },
        api=str(value.get("api") or ""),
        method=str(value.get("method") or "").upper(),
        path=str(value.get("path") or ""),
        operation_id=str(value["operation_id"]) if value.get("operation_id") else None,
        call_hash=str(value.get("call_hash") or ""),
        tool_call_id=str(value["tool_call_id"]) if value.get("tool_call_id") else None,
        message_id=str(value["message_id"]) if value.get("message_id") else None,
        approvers=approvers,
        payload=payload,
        created_at=now,
        expires_at=now + timedelta(seconds=timeout),
    )


def decision_value(record: ApprovalRecord, decision: str) -> dict[str, Any]:
    """The resume value for the interrupt of `record`: `approve`, `reject` or `expired`,
    or `pending` for a call whose approval still waits (another one was decided).

    It names the call it was taken for (API, method, path): the client applies
    it to that call whatever the policy says about gating it by then, so a
    rejected or expired call is never sent and a pending one pauses again.
    `approvers` are the ones the approval was asked of: the client refuses an
    approval whose approvers differ from what the policy's gate names when
    the call is about to be sent.
    """
    return {
        "type": APPROVAL_DECISION,
        "approval_id": record.approval_id,
        "decision": decision,
        "api": record.api,
        "method": record.method,
        "path": record.path,
        "call_hash": record.call_hash,
        "approvers": list(record.approvers),
        "comment": record.comment,
    }


# ---------------------------------------------------------------------------
# Who decides, who sees
# ---------------------------------------------------------------------------


def role_approvers(approvers: Iterable[str]) -> set[str]:
    return {
        a[len(ROLE_APPROVER_PREFIX) :]
        for a in approvers
        if isinstance(a, str) and a.startswith(ROLE_APPROVER_PREFIX)
    }


def is_requester(principal: Principal, owner_id: str | None) -> bool:
    """The principal who started the run: the thread's owner (only the owner runs on it)."""
    return bool(owner_id) and principal.id == owner_id


def may_decide(principal: Principal, owner_id: str | None, approvers: Iterable[str]) -> bool:
    """Whether `principal` may approve or reject: see the module docstring.

    The requester decides only when `requester` is listed, never through a
    role (no self-approval unless the policy asks for requester confirmation).
    """
    approvers = list(approvers)
    if is_requester(principal, owner_id):
        return REQUESTER_APPROVER in approvers
    return bool(role_approvers(approvers) & set(principal.roles))


def reads_across(principal: Principal) -> bool:
    return bool(set(principal.roles) & read_across_roles())


def may_view(principal: Principal, owner_id: str | None, approvers: Iterable[str]) -> bool:
    """The owner, a decider, or a read-across role (for listing)."""
    return (
        is_requester(principal, owner_id)
        or may_decide(principal, owner_id, approvers)
        or reads_across(principal)
    )


def sees_call(principal: Principal, owner_id: str | None, approvers: Iterable[str]) -> bool:
    """Whether the viewer sees the call's query and body: the owner and the deciders
    do (they need them); read-across roles only under `TRACE_CAPTURE=full`."""
    return (
        is_requester(principal, owner_id)
        or may_decide(principal, owner_id, approvers)
        or capture_full()
    )


def resume_principal(record: ApprovalRecord, owner_id: str, decider: Principal) -> Principal:
    """Who the resumed run acts as: always the requester, never the decider.

    When the requester decides, their own principal of this request (its
    credentials included, for `auth: forward` APIs). When someone else does,
    the requester with the roles and public attributes the run had when it
    paused; credentials are never stored, so an `auth: forward` call approved
    by someone else has none and is not sent.
    """
    if decider.id == owner_id:
        return decider
    context = record.requester_context or {}
    return Principal(
        id=owner_id,
        roles=[str(r) for r in context.get("roles") or []],
        attributes=dict(context.get("attributes") or {}),
    )


# ---------------------------------------------------------------------------
# The store (and the ledger `api_client` consults)
# ---------------------------------------------------------------------------


def _without_call(payload: Mapping[str, Any]) -> dict[str, Any]:
    if capture_full():
        return dict(payload)
    return {k: v for k, v in payload.items() if k not in CALL_CONTENT_KEYS}


_COLUMNS = (
    "approval_id, thread_id, run_id, interrupt_id, requester_hash, requester_context, api, "
    "method, path, operation_id, call_hash, tool_call_id, message_id, approvers, payload, "
    "status, decided_by, decided_at, comment, used_at, created_at, expires_at"
)
# Clears the call from the payload unless the first parameter is true (TRACE_CAPTURE=full).
_CLEARED_PAYLOAD = "CASE WHEN %s THEN payload ELSE payload - 'query' - 'body' END"


def _record_from_row(row: Mapping[str, Any]) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=row["approval_id"],
        thread_id=row["thread_id"],
        run_id=row["run_id"],
        interrupt_id=row["interrupt_id"],
        requester_hash=row["requester_hash"],
        requester_context=_decode(row.get("requester_context")) or {},
        api=row["api"],
        method=row["method"],
        path=row["path"],
        operation_id=row.get("operation_id"),
        call_hash=row["call_hash"],
        tool_call_id=row.get("tool_call_id"),
        message_id=row.get("message_id"),
        approvers=list(_decode(row.get("approvers")) or []),
        payload=dict(_decode(row.get("payload")) or {}),
        status=row["status"],
        decided_by=row.get("decided_by"),
        decided_at=_as_datetime(row.get("decided_at")),
        comment=row.get("comment"),
        used_at=_as_datetime(row.get("used_at")),
        created_at=_as_datetime(row.get("created_at")) or utcnow(),
        expires_at=_as_datetime(row.get("expires_at")) or utcnow(),
    )


class ApprovalStore:
    """The approvals table under postgres, an in-process dict otherwise.

    Also the ledger `api_client` marks approvals used in (`consume`). Times
    are this process's clock (`clock`), for the expiry and the decision alike.
    """

    def __init__(self, db: Database, *, clock: Any = utcnow) -> None:
        self.db = db
        self.table = db.approvals_table
        self._clock = clock
        self._memory: OrderedDict[str, ApprovalRecord] = OrderedDict()

    def now(self) -> datetime:
        return self._clock()

    # -- writes -----------------------------------------------------------------

    async def add(self, record: ApprovalRecord) -> tuple[ApprovalRecord, bool, int]:
        """Record a pending approval: `(record, created, superseded)`.

        A tool asked again for the same interrupt (the run resumed for another
        approval, so this tool call ran again) keeps the pending approval of the
        same request instead of a second one (`created` False). A pending
        approval of that interrupt for a different request, or past its expiry,
        is marked expired (`superseded` counts them).
        """
        now = self.now()
        if not self.db.is_postgres:
            superseded = 0
            for existing in list(self._memory.values()):
                if (
                    existing.thread_id != record.thread_id
                    or existing.interrupt_id != record.interrupt_id
                    or existing.status != PENDING
                ):
                    continue
                if existing.call_hash == record.call_hash and existing.expires_at > now:
                    return existing, False, 0
                self._close(existing, EXPIRED, now)
                superseded += 1
            self._memory[record.approval_id] = record
            self._evict()
            return record, True, superseded
        rows = await self.db.fetchall(
            f"""
            UPDATE {self.table}
               SET status = %s, decided_at = %s, payload = {_CLEARED_PAYLOAD}
             WHERE thread_id = %s AND interrupt_id = %s AND status = %s
               AND (call_hash <> %s OR expires_at <= %s)
            RETURNING approval_id
            """,
            (
                EXPIRED,
                now,
                capture_full(),
                record.thread_id,
                record.interrupt_id,
                PENDING,
                record.call_hash,
                now,
            ),
        )
        existing = await self.db.fetchone(
            f"""
            SELECT {_COLUMNS} FROM {self.table}
             WHERE thread_id = %s AND interrupt_id = %s AND status = %s AND call_hash = %s
               AND expires_at > %s
             ORDER BY created_at DESC LIMIT 1
            """,
            (record.thread_id, record.interrupt_id, PENDING, record.call_hash, now),
        )
        if existing is not None:
            return _record_from_row(existing), False, len(rows)
        await self.db.execute(
            f"""
            INSERT INTO {self.table} ({_COLUMNS})
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                    %s::jsonb, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                record.approval_id,
                record.thread_id,
                record.run_id,
                record.interrupt_id,
                record.requester_hash,
                _json(record.requester_context),
                record.api,
                record.method,
                record.path,
                record.operation_id,
                record.call_hash,
                record.tool_call_id,
                record.message_id,
                _json(record.approvers),
                _json(record.payload),
                record.status,
                record.decided_by,
                record.decided_at,
                record.comment,
                record.used_at,
                record.created_at,
                record.expires_at,
            ),
        )
        return record, True, len(rows)

    async def decide(
        self, approval_id: str, status: str, decided_by: str, comment: str | None
    ) -> ApprovalRecord | None:
        """Decide a pending, unexpired approval (`approved` or `rejected`), atomically.

        None when it is not pending any more (decided, expired, gone): of two
        concurrent decisions exactly one gets the record.
        """
        if status not in (APPROVED, REJECTED):
            raise ValueError(f"not a decision: {status!r}")
        now = self.now()
        comment = (comment or "").strip()[:COMMENT_MAX_CHARS] or None
        if not self.db.is_postgres:
            record = self._memory.get(approval_id)
            if record is None or not record.is_pending(now):
                return None
            self._close(record, status, now, decided_by=decided_by, comment=comment)
            return record
        row = await self.db.fetchone(
            f"""
            UPDATE {self.table}
               SET status = %s, decided_by = %s, decided_at = %s, comment = %s,
                   payload = {_CLEARED_PAYLOAD}
             WHERE approval_id = %s AND status = %s AND expires_at > %s
            RETURNING {_COLUMNS}
            """,
            (status, decided_by, now, comment, capture_full(), approval_id, PENDING, now),
        )
        return _record_from_row(row) if row is not None else None

    async def consume(
        self, approval_id: str, call_hash: str, thread_id: str | None = None
    ) -> str | None:
        """The ledger: mark an approved approval used, once. None when it may be sent now.

        It must be approved, for exactly the request `call_hash`, on thread
        `thread_id` (when given), and never used before; else the reason.
        """
        now = self.now()
        if not self.db.is_postgres:
            record = self._memory.get(approval_id)
            problem = self._unusable(record, call_hash, thread_id)
            if problem is None and record is not None:
                record.used_at = now
            return problem
        row = await self.db.fetchone(
            f"""
            UPDATE {self.table} SET used_at = %s
             WHERE approval_id = %s AND status = %s AND used_at IS NULL AND call_hash = %s
               AND (%s::text IS NULL OR thread_id = %s::text)
            RETURNING approval_id
            """,
            (now, approval_id, APPROVED, call_hash, thread_id, thread_id),
        )
        if row is not None:
            return None
        return self._unusable(await self.get(approval_id), call_hash, thread_id) or (
            "the approval is not usable"
        )

    @staticmethod
    def _unusable(
        record: ApprovalRecord | None, call_hash: str, thread_id: str | None
    ) -> str | None:
        if record is None:
            return "no such approval"
        if thread_id is not None and record.thread_id != thread_id:
            return "the approval belongs to another thread"
        if record.status != APPROVED:
            return f"the approval is {record.effective_status()}, not approved"
        if record.call_hash != call_hash:
            return "the approval is for a different request"
        if record.used_at is not None:
            return "the approval was already used"
        return None

    async def expire(self, approval_id: str) -> ApprovalRecord | None:
        """Mark one pending approval `expired` (its run no longer waits for it); None if not pending."""
        now = self.now()
        if not self.db.is_postgres:
            record = self._memory.get(approval_id)
            if record is None or record.status != PENDING:
                return None
            self._close(record, EXPIRED, now)
            return record
        row = await self.db.fetchone(
            f"""
            UPDATE {self.table}
               SET status = %s, decided_at = %s, payload = {_CLEARED_PAYLOAD}
             WHERE approval_id = %s AND status = %s
            RETURNING {_COLUMNS}
            """,
            (EXPIRED, now, capture_full(), approval_id, PENDING),
        )
        return _record_from_row(row) if row is not None else None

    async def expire_due(self) -> list[ApprovalRecord]:
        """Mark every pending approval past its expiry `expired`; return them (the sweep)."""
        now = self.now()
        if not self.db.is_postgres:
            due = [r for r in self._memory.values() if r.status == PENDING and r.expires_at <= now]
            for record in due:
                self._close(record, EXPIRED, now)
            return due
        rows = await self.db.fetchall(
            f"""
            UPDATE {self.table}
               SET status = %s, decided_at = %s, payload = {_CLEARED_PAYLOAD}
             WHERE status = %s AND expires_at <= %s
            RETURNING {_COLUMNS}
            """,
            (EXPIRED, now, capture_full(), PENDING, now),
        )
        return [_record_from_row(r) for r in rows]

    async def delete_for_thread(self, thread_id: str) -> int:
        """Delete every approval of a thread (the thread was deleted); how many."""
        if not self.db.is_postgres:
            gone = [k for k, r in self._memory.items() if r.thread_id == thread_id]
            for key in gone:
                del self._memory[key]
            return len(gone)
        rows = await self.db.fetchall(
            f"DELETE FROM {self.table} WHERE thread_id = %s RETURNING approval_id", (thread_id,)
        )
        return len(rows)

    # -- reads ------------------------------------------------------------------

    async def bound_approvals(
        self, *, tool_call: tuple[str, str] | None = None, interrupt_id: str | None = None
    ) -> list[BoundApproval]:
        """The ledger's answer for a tool call that runs again without a decision.

        Every approval asked by the tool call `(message id, tool call id)` or by
        the interrupt `interrupt_id`, newest first, on any thread: a copy of a
        thread keeps its messages, so a tool call there is the same tool call
        (message ids are unique, so another thread's tool call never matches).
        `api_client` refuses the call when one of them was asked for it.
        """
        now = self.now()
        message_id, call_id = tool_call or (None, None)
        if not self.db.is_postgres:
            records = [
                r
                for r in self._memory.values()
                if (message_id and r.message_id == message_id and r.tool_call_id == call_id)
                or (interrupt_id and r.interrupt_id == interrupt_id)
            ]
        else:
            rows = await self.db.fetchall(
                f"""
                SELECT {_COLUMNS} FROM {self.table}
                 WHERE (message_id = %s::text AND tool_call_id = %s::text)
                    OR interrupt_id = %s::text
                """,
                (message_id, call_id, interrupt_id),
            )
            records = [_record_from_row(r) for r in rows]
        records.sort(key=lambda r: r.created_at, reverse=True)
        return [
            BoundApproval(
                api=r.api,
                method=r.method,
                path=r.path,
                status=r.effective_status(now),
                used=r.used_at is not None,
            )
            for r in records
        ]

    async def get(self, approval_id: str) -> ApprovalRecord | None:
        if not self.db.is_postgres:
            return self._memory.get(approval_id)
        row = await self.db.fetchone(
            f"SELECT {_COLUMNS} FROM {self.table} WHERE approval_id = %s", (approval_id,)
        )
        return _record_from_row(row) if row is not None else None

    async def for_thread(self, thread_id: str) -> list[ApprovalRecord]:
        """Every approval of a thread, newest first."""
        if not self.db.is_postgres:
            return sorted(
                (r for r in self._memory.values() if r.thread_id == thread_id),
                key=lambda r: r.created_at,
                reverse=True,
            )
        rows = await self.db.fetchall(
            f"SELECT {_COLUMNS} FROM {self.table} WHERE thread_id = %s "
            "ORDER BY created_at DESC, approval_id",
            (thread_id,),
        )
        return [_record_from_row(r) for r in rows]

    async def pending_for_thread(self, thread_id: str) -> list[ApprovalRecord]:
        """The thread's pending approvals that have not expired, oldest first."""
        now = self.now()
        return sorted(
            (r for r in await self.for_thread(thread_id) if r.is_pending(now)),
            key=lambda r: r.created_at,
        )

    async def visible(
        self,
        principal: Principal,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ApprovalRecord]:
        """Approvals the caller may see across threads, newest first (`GET /approvals`).

        Their own (requested on their threads), the ones a role of theirs may
        decide, and every one for a read-across role. `status` filters (a
        pending approval past its expiry counts as `expired`).
        """
        now = self.now()
        across = reads_across(principal)
        own = principal.hashed_id()
        roles = {f"{ROLE_APPROVER_PREFIX}{r}" for r in principal.roles}
        if not self.db.is_postgres:
            rows = [
                r
                for r in sorted(self._memory.values(), key=lambda r: r.created_at, reverse=True)
                if (across or r.requester_hash == own or roles & set(r.approvers))
                and (status is None or r.effective_status(now) == status)
            ]
            return rows[offset : offset + limit]
        conditions = ["(%s OR requester_hash = %s OR approvers ?| %s::text[])"]
        params: list[Any] = [across, own, sorted(roles)]
        if status == PENDING:
            conditions.append("status = %s AND expires_at > %s")
            params += [PENDING, now]
        elif status == EXPIRED:
            conditions.append("(status = %s OR (status = %s AND expires_at <= %s))")
            params += [EXPIRED, PENDING, now]
        elif status is not None:
            conditions.append("status = %s")
            params.append(status)
        rows = await self.db.fetchall(
            f"SELECT {_COLUMNS} FROM {self.table} WHERE {' AND '.join(conditions)} "
            "ORDER BY created_at DESC, approval_id LIMIT %s OFFSET %s",
            (*params, limit, offset),
        )
        return [_record_from_row(r) for r in rows]

    # -- memory -----------------------------------------------------------------

    @staticmethod
    def _close(
        record: ApprovalRecord,
        status: str,
        now: datetime,
        *,
        decided_by: str | None = None,
        comment: str | None = None,
    ) -> None:
        record.status = status
        record.decided_at = now
        if decided_by is not None:
            record.decided_by = decided_by
        if comment is not None:
            record.comment = comment
        record.payload = _without_call(record.payload)

    def _evict(self) -> None:
        while len(self._memory) > MEMORY_APPROVALS_CAP:
            victim = next(
                (k for k, r in self._memory.items() if r.status != PENDING),
                next(iter(self._memory)),
            )
            del self._memory[victim]
