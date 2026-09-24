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

"""A fake of the chat API's approval contract, for the CLI's client tests.

It follows the contract the scaffolded runtime implements: a gated call
pauses the run (``message.end`` with ``status: awaiting_approval`` and the
``approval`` payload); ``GET /threads/{thread_id}/approvals`` lists a
thread's approvals for its owner, its approvers and read-across roles;
``POST /threads/{thread_id}/approvals/{approval_id}`` takes ``{"decision",
"comment"}`` and answers 404 (unknown), 403 (not an allowed approver), 410
(expired), 409 (not pending) or the resumed run as SSE; a new ``/chat``
message on a thread with a pending approval is 409 ``approval_pending``;
deleting a thread deletes its approvals. ``requester`` is the principal who
started the run; ``role:<x>`` is another principal holding role x (a
requester decides their own call only when ``requester`` is listed). An
approved call is recorded in ``sent`` exactly once.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

Events = list[tuple[str, Any]]


@dataclass
class FakeApproval:
    approval_id: str
    thread_id: str
    requester: str
    approvers: list[str]
    expires_at: datetime
    api: str = "orders"
    method: str = "POST"
    path: str = "/orders/ORD-1/cancel"
    operation_id: str | None = "cancelOrder"
    query: dict[str, Any] = field(default_factory=dict)
    body: Any = field(default_factory=lambda: {"reason": "duplicate"})
    reason: str | None = "cancel_order: the customer asked to cancel ORD-1"
    tool_call_id: str = "call-gated"
    tool_name: str = "cancel_order"
    status: str = "pending"
    decided_by: str | None = None
    decided_at: str | None = None
    comment: str | None = None

    def payload(self) -> dict[str, Any]:
        """The paused run's ``approval`` (the call, who decides, until when)."""
        return {
            "approval_id": self.approval_id,
            "api": self.api,
            "method": self.method,
            "path": self.path,
            "query": self.query,
            "body": self.body,
            "operation_id": self.operation_id,
            "reason": self.reason,
            "approvers": list(self.approvers),
            "expires_at": self.expires_at.isoformat().replace("+00:00", "Z"),
        }

    def row(self) -> dict[str, Any]:
        return {
            **self.payload(),
            "id": self.approval_id,
            "thread_id": self.thread_id,
            "status": self.status,
            "decided_at": self.decided_at,
            "comment": self.comment,
        }


class ApprovalBook:
    """The server-side state and rules of the approval routes."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        # bearer token -> (principal id, roles)
        self.principals: dict[str, tuple[str, set[str]]] = {
            "local-key": ("shared", {"shared"}),
            "alice-token": ("alice", {"support"}),
            "bob-token": ("bob", {"ops"}),
            "carol-token": ("carol", set()),
            "auditor-token": ("audra", {"auditor"}),
        }
        self.read_across_roles = {"auditor"}
        self.approvals: dict[str, FakeApproval] = {}
        self.owners: dict[str, str] = {}
        self.sent: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []
        # Listing shows every approval as pending (a listing read before a decision raced it).
        self.stale_listing = False
        self.continuation: Callable[[FakeApproval, str], Events] = default_continuation

    # -- principals -----------------------------------------------------------

    def principal(self, headers: dict[str, str]) -> tuple[str, set[str]] | None:
        auth = headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return None
        return self.principals.get(auth[len("Bearer ") :])

    @staticmethod
    def may_decide(approval: FakeApproval, pid: str, roles: set[str]) -> bool:
        if "requester" in approval.approvers and pid == approval.requester:
            return True
        return any(
            a.startswith("role:") and a[len("role:") :] in roles and pid != approval.requester
            for a in approval.approvers
        )

    # -- a paused run -----------------------------------------------------------

    def pause(
        self,
        thread_id: str,
        requester: str,
        *,
        approvers: list[str] | None = None,
        expires_in: float = 900,
        run_id: str = "run-1",
        text: str = "I will cancel that order.",
        **fields: Any,
    ) -> Events:
        """Record a pending approval; the events of the run that paused on it."""
        approval = FakeApproval(
            approval_id=fields.pop("approval_id", f"appr-{uuid.uuid4().hex[:8]}"),
            thread_id=thread_id,
            requester=requester,
            approvers=approvers or ["requester"],
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
            **fields,
        )
        with self.lock:
            self.owners.setdefault(thread_id, requester)
            self.approvals[approval.approval_id] = approval
        return paused_run(approval, run_id=run_id, text=text)

    def pending_on(self, thread_id: str) -> bool:
        return any(
            a.thread_id == thread_id and a.status == "pending" for a in self.approvals.values()
        )

    # -- routes -----------------------------------------------------------------

    def list_threads(self, headers: dict[str, str]) -> tuple[int, Any]:
        who = self.principal(headers)
        if who is None:
            return 401, {"detail": "Unauthorized"}
        return 200, [
            {"thread_id": t, "owner": f"h-{owner}"}
            for t, owner in self.owners.items()
            if owner == who[0]
        ]

    def list(self, thread_id: str, headers: dict[str, str]) -> tuple[int, Any]:
        who = self.principal(headers)
        if who is None:
            return 401, {"detail": "Unauthorized"}
        pid, roles = who
        if thread_id not in self.owners:
            return 404, {"detail": "thread not found"}
        rows = [a for a in self.approvals.values() if a.thread_id == thread_id]
        visible = (
            self.owners[thread_id] == pid
            or roles & self.read_across_roles
            or any(self.may_decide(a, pid, roles) for a in rows)
        )
        if not visible:
            return 403, {"detail": "Forbidden"}
        out = []
        for a in rows:
            row = a.row()
            if self.stale_listing:
                row["status"] = "pending"
            out.append(row)
        return 200, out

    def decide(
        self, thread_id: str, approval_id: str, headers: dict[str, str], body: dict[str, Any]
    ) -> tuple[int, Any]:
        """(status, JSON body) on refusal; (200, events of the resumed run) on success."""
        who = self.principal(headers)
        if who is None:
            return 401, {"detail": "Unauthorized"}
        pid, roles = who
        if body.get("decision") not in ("approve", "reject") or set(body) - {
            "decision",
            "comment",
        }:
            return 422, {"detail": "body must be {decision, comment}"}
        with self.lock:
            approval = self.approvals.get(approval_id)
            if approval is None or approval.thread_id != thread_id:
                return 404, {"detail": "approval not found"}
            if not self.may_decide(approval, pid, roles):
                return 403, {"detail": "not an allowed approver"}
            if approval.status == "pending" and approval.expires_at <= datetime.now(UTC):
                approval.status = "expired"
            if approval.status == "expired":
                return 410, {"detail": "approval expired"}
            if approval.status != "pending":
                return 409, {"code": "approval_not_pending", "detail": approval.status}
            decision = body["decision"]
            approval.status = "approved" if decision == "approve" else "rejected"
            approval.decided_by = pid
            approval.decided_at = datetime.now(UTC).isoformat()
            approval.comment = body.get("comment")
            self.decisions.append({"approval_id": approval_id, "by": pid, **body})
            if decision == "approve":
                # Exactly what was approved, once.
                self.sent.append(
                    {
                        "method": approval.method,
                        "path": approval.path,
                        "body": approval.body,
                        "query": approval.query,
                    }
                )
        return 200, self.continuation(approval, decision)

    def delete_thread(self, thread_id: str, headers: dict[str, str]) -> int:
        who = self.principal(headers)
        if who is None:
            return 401
        with self.lock:
            if self.owners.get(thread_id) != who[0]:
                return 404
            del self.owners[thread_id]
            for key in [k for k, a in self.approvals.items() if a.thread_id == thread_id]:
                del self.approvals[key]
        return 204


def paused_run(approval: FakeApproval, *, run_id: str = "run-1", text: str = "") -> Events:
    events: Events = [("message.start", {"thread_id": approval.thread_id, "run_id": run_id})]
    if text:
        events.append(("message.delta", {"text": text}))
    events.append(
        (
            "tool.call",
            {
                "id": approval.tool_call_id,
                "name": approval.tool_name,
                "args": {"order_id": "ORD-1"},
            },
        )
    )
    events.append(
        (
            "message.end",
            {
                "thread_id": approval.thread_id,
                "run_id": run_id,
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "latency_ms": 40,
                "status": "awaiting_approval",
                "approval": approval.payload(),
            },
        )
    )
    return events


def default_continuation(approval: FakeApproval, decision: str) -> Events:
    approved = decision == "approve"
    return [
        ("message.start", {"thread_id": approval.thread_id, "run_id": "run-2"}),
        (
            "tool.result",
            {
                "id": approval.tool_call_id,
                "name": approval.tool_name,
                "result": "cancelled" if approved else "not approved by a human; nothing was sent",
                "is_error": not approved,
            },
        ),
        (
            "message.delta",
            {
                "text": "Done: ORD-1 is cancelled."
                if approved
                else "The cancellation was not approved."
            },
        ),
        (
            "message.end",
            {
                "thread_id": approval.thread_id,
                "run_id": "run-2",
                "usage": {"input_tokens": 3, "output_tokens": 4},
                "latency_ms": 12,
                "status": "ok",
            },
        ),
    ]
