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

"""The one graph invocation path behind `/chat`, A2A and the playground.

`ChatRuntime.stream()` yields the events of the chat API
(`message.start`, `message.delta`, `tool.call`, `tool.result`, `message.end`,
`error`, plus internal heartbeats sent as SSE `: keep-alive` comments) from either:

* the in-process graph with the checkpointer bound at startup (`fastapi`), or
* the LangGraph Server this app is mounted in (`langgraph-server`), through the
  SDK's loopback client, so the server keeps owning persistence and threads.

Both runtimes apply the same rules:

* Thread ownership (one principal per thread): through the `threads` table
  under fastapi, and through the thread metadata `{principal_id, tenant}` under
  langgraph-server. The SDK loopback client runs under the server's `/noauth`
  root path, so the server's own `@auth.on` filters never see these calls; the
  check has to live here.
* One run per thread: a second run while one is in progress gets
  `ThreadBusy` (HTTP 409 `{"code": "thread_busy"}` on `/chat`). Deleting a
  thread (and the retention purge) takes the same lock, and a run checks the
  thread's owner again once it holds it, so a turn is never written to a
  thread that has no owner (fastapi). Across replicas the lock is a lease
  (`run_locks.py`): a run whose lease cannot be renewed is stopped (status
  `interrupted`) and its checkpoint writes are refused (fastapi).
* A valid history: model providers reject an assistant tool call that is not
  followed by its result. A run cut short (a timeout, a disconnect, a crash, a
  database outage, an OOM kill) can leave one, so every run first answers the
  open tool calls of its thread with an error result placed right after the
  call (and puts misplaced results back in place), before its own turn. A
  call whose arguments are not valid JSON counts as a call too (providers
  are sent it back as one); the agent answers those in the run that made
  them (`content.AnswerInvalidToolCalls`).
* Guardrails: `RUN_TIMEOUT_S` cancels a run (status `timeout`), a client that
  disconnects cancels its run (status `cancelled`), and `SSE_HEARTBEAT_S`
  keeps idle streams alive. `RECURSION_LIMIT` caps graph steps: a run that
  reaches it ends with a final message saying so (`message.end` status
  `step_limit`) and keeps what it did in the thread.
* Errors reach clients as a generic message with an `error_id`; the detail
  goes to the log under that id (and to the event under `APP_ENV=dev` only).
  An unreachable database is a 503 (or an `unavailable` event) after a few
  seconds, logged as one line without a traceback.
* Client metadata is kept in the run record and, under `TRACE_CAPTURE=full`
  only, in traces under `client_metadata` (never over the server-set
  `thread_id`, `run_id`, `principal_hash`); it is not written into checkpoints.
* Run records are durable in Postgres when the runtime has one (see `db.py`):
  written as `running` when a run starts and updated when it ends; a run that
  never ended (its process died) is marked `interrupted` by the next
  reconciliation (every minute, on any replica). `RETENTION_DAYS` purges
  threads idle longer than that. Under langgraph-server the server's own
  `DELETE /threads/{id}` removes the thread's run records too
  (`forget_thread_runs`, called by `middleware.ThreadDeleteHookMiddleware`).
* Startup does not wait for the database: the app starts, `/ready` answers
  503 and the schema setup is retried in the background until the database
  answers; requests meanwhile get 503.
* Human approval of gated API calls (`approvals.py`): a run whose tool made a
  gated call pauses (a LangGraph interrupt, kept in the checkpoint); the run
  records one pending approval per interrupt and ends with `message.end`
  status `awaiting_approval` carrying `approval` (and `approvals`, all of
  them). While one is pending, a new message on the thread is refused
  (`ApprovalPending`, HTTP 409 `{"code": "approval_pending"}`). `decide()`
  checks the decider and the approval, decides it atomically under the
  thread's run lock, and `stream(..., resume=...)` resumes the paused run with
  the decision (under langgraph-server through the server's native resume),
  acting as the requester; the run's other paused calls get their own
  approval's state with it (a decision binds its call whatever the policy says
  about gating it by then, see `api_client`). A pending approval that expires is closed by the
  next resume (the tool gets "expired") or, when a new message comes instead,
  by the history repair (the call's result says the approval expired).
  Deleting a thread deletes its approvals.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import uuid
from collections import Counter, deque
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException

from {{cookiecutter.agent_directory}}.app_utils import metrics
from {{cookiecutter.agent_directory}}.app_utils.api_client import (
    DECISION_APPROVE,
    DECISION_EXPIRED,
    DECISION_PENDING,
    DECISION_REJECT,
    approval_ledger,
    set_approval_ledger,
)
from {{cookiecutter.agent_directory}}.app_utils.api_client import end_run as end_api_run
from {{cookiecutter.agent_directory}}.app_utils.approvals import (
    APPROVE,
    APPROVED,
    CODE_APPROVAL_PENDING,
    CODE_EXPIRED,
    CODE_NOT_PENDING,
    DECISIONS,
    EXPIRED,
    PENDING,
    REJECTED,
    SWEEP_INTERVAL_S,
    ApprovalRecord,
    ApprovalStore,
    decision_value,
    is_approval_interrupt,
    may_decide,
    may_view,
    record_from_interrupt,
    resume_principal,
    sees_call,
)
from {{cookiecutter.agent_directory}}.app_utils.auth import Principal
from {{cookiecutter.agent_directory}}.app_utils.checkpointer import (
    checkpointer_kind,
    get_checkpointer,
    postgres_saver,
)
from {{cookiecutter.agent_directory}}.app_utils.content import (
    INVALID_TOOL_CALL_RESULT,
    INVALID_TOOL_CALL_TYPE,
    content_to_text,
)
from {{cookiecutter.agent_directory}}.app_utils.db import (
    RUN_INTERRUPTED,
    Database,
    RunRecord,
    RunStore,
    StorageNotReady,
    capture_full,
    is_database_unavailable,
    is_postgres_url,
)
from {{cookiecutter.agent_directory}}.app_utils.limits import (
    recursion_limit,
    retention_days,
    run_timeout_s,
    sequential_tool_calls,
    sse_heartbeat_s,
    steps_for_tool_calls,
    valid_thread_id,
)
from {{cookiecutter.agent_directory}}.app_utils.model import model_label
from {{cookiecutter.agent_directory}}.app_utils.telemetry import bind_log_context
from {{cookiecutter.agent_directory}}.app_utils.threads import (
    THREAD_BUSY,
    LeaseLost,
    ThreadBusy,
    ThreadLease,
    ThreadLocks,
    ThreadRecord,
    ThreadStore,
    assert_access,
    assert_owner,
    is_owner,
    reads_across,
    thread_deleted,
)

logger = logging.getLogger(__name__)

FASTAPI = "fastapi"
LANGGRAPH_SERVER = "langgraph-server"
GRAPH_ID = "agent"  # the key in langgraph.json "graphs"

EVENT_START = "message.start"
EVENT_DELTA = "message.delta"
EVENT_TOOL_CALL = "tool.call"
EVENT_TOOL_RESULT = "tool.result"
EVENT_END = "message.end"
EVENT_ERROR = "error"
# Internal: sent as an SSE comment line, ignored by A2A.
EVENT_HEARTBEAT = "heartbeat"

# Run statuses in run records and metrics (`running` while a run is in progress).
STATUS_OK = "ok"
# The run paused before a gated API call: it waits for a human decision.
STATUS_AWAITING_APPROVAL = "awaiting_approval"
STATUS_STEP_LIMIT = "step_limit"
STATUS_ERROR = "error"
STATUS_TIMEOUT = "timeout"
STATUS_CANCELLED = "cancelled"
STATUS_INTERRUPTED = RUN_INTERRUPTED

# Error codes clients see in `error` events.
CODE_RUN_FAILED = "run_failed"
CODE_TIMEOUT = "timeout"
CODE_RECURSION = "recursion_limit"
CODE_UNAVAILABLE = "unavailable"
CODE_FORBIDDEN = "forbidden"
# The graph paused for input this server cannot collect (an interrupt that is
# not a gated API call's).
CODE_UNSUPPORTED_INTERRUPT = "unsupported_interrupt"

# Request headers passed on to the LangGraph Server under langgraph-server.
# They matter when LANGGRAPH_SERVER_URL points at a real HTTP endpoint (its
# auth handler runs there); the default in-process loopback ignores them.
DEFAULT_FORWARD_HEADERS = ("authorization", "cookie")

RETENTION_INTERVAL_S = 3600.0
RETENTION_FIRST_DELAY_S = 60.0
RETENTION_BATCH = 500
RETENTION_MAX_BATCHES = 20

# Runs of dead processes are closed (`interrupted`) this often, by every replica.
RECONCILE_INTERVAL_S = 60.0
# A `running` record younger than this is never reconciled (clock skew margin).
RECONCILE_GRACE_S = 60.0
# Final run records that could not be written are retried (at most this many kept).
MAX_UNRECORDED_RUNS = 1000
# Database setup at startup: retried with this back-off while the database is down.
INIT_RETRY_FIRST_S = 0.5
INIT_RETRY_MAX_S = 2.0
# Each bookkeeping step after a run (history repair, run record) is bounded.
FINISH_STEP_TIMEOUT_S = 5.0
# How long the end of a run waits for its cancelled graph to stop.
PUMP_STOP_WAIT_S = 1.0

# Error results for tool calls a run left open, by why they were left open.
OPEN_CALL_INTERRUPTED = (
    "The tool call did not finish: the run was interrupted before its result was saved."
)
OPEN_CALL_STEP_LIMIT = "The tool call did not run: the run reached its step limit."
# Results for a tool call left open by a run that paused for approval, when a
# new message comes instead of a resume, by the approval's status.
OPEN_CALL_BY_APPROVAL = {
    EXPIRED: "The call was not approved: the approval request expired before anyone decided, "
    "so nothing was sent.",
    REJECTED: "The call was not approved: an approver rejected it, so nothing was sent.",
    APPROVED: "The call was approved, but the run did not continue, so nothing was sent.",
}
OPEN_CALL_SENT_UNSAVED = (
    "The call was approved and sent, but the run stopped before its result was saved."
)
# The resume value a paused call gets for its approval's state (see `decide`).
RESUME_AS = {
    PENDING: DECISION_PENDING,
    APPROVED: DECISION_APPROVE,
    REJECTED: DECISION_REJECT,
    EXPIRED: DECISION_EXPIRED,
}
UNSUPPORTED_INTERRUPT_MESSAGE = (
    "The agent paused for input this server cannot collect. Send a new message to continue."
)
STEP_LIMIT_MESSAGE = (
    "I had to stop before finishing: this request needs more steps than one run may take "
    "({limit}). What I did so far is kept in this conversation, so you can ask me to "
    "continue, or narrow the request."
)


def detect_runtime() -> str:
    """`langgraph-server` when mounted inside the server, else `fastapi`.

    The server Dockerfile sets `LANGGRAPH_SERVER=1`; `langgraph dev` and the
    server image also export `LANGSERVE_GRAPHS`. `RUNTIME` overrides both.
    """
    explicit = (os.environ.get("RUNTIME") or "").strip().lower()
    if explicit in (FASTAPI, LANGGRAPH_SERVER):
        return explicit
    if os.environ.get("LANGGRAPH_SERVER", "").lower() in ("1", "true", "yes"):
        return LANGGRAPH_SERVER
    if os.environ.get("LANGSERVE_GRAPHS"):
        return LANGGRAPH_SERVER
    return FASTAPI


def forward_header_names() -> frozenset[str]:
    """`AUTH_FORWARD_HEADERS` (comma list, case-insensitive); default `authorization,cookie`.

    Set it to the headers your auth policy reads when the LangGraph Server is
    reached over HTTP; an empty value forwards nothing.
    """
    raw = os.environ.get("AUTH_FORWARD_HEADERS")
    names = DEFAULT_FORWARD_HEADERS if raw is None else raw.split(",")
    return frozenset(n.strip().lower() for n in names if n.strip())


def select_forward_headers(headers: Mapping[str, str]) -> dict[str, str]:
    allowed = forward_header_names()
    return {k: v for k, v in headers.items() if k.lower() in allowed}


def dev_mode() -> bool:
    """`APP_ENV` is exactly `dev` (as `auth.dev_mode`)."""
    return os.environ.get("APP_ENV") == "dev"


def sse_encode(event: str, data: Mapping[str, Any]) -> str:
    if event == EVENT_HEARTBEAT:
        return ": keep-alive\n\n"
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def new_error_id() -> str:
    return uuid.uuid4().hex[:16]


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0][:300] if text else ""


def unavailable(what: str, exc: BaseException) -> HTTPException:
    """A 503 whose detail names an error id, never the exception text (logged instead)."""
    error_id = log_unavailable(what, exc)
    return HTTPException(status_code=503, detail=unavailable_detail(what, error_id))


def unavailable_detail(what: str, error_id: str) -> str:
    return f"{what} unavailable. Reference: {error_id}."


def log_unavailable(what: str, exc: BaseException) -> str:
    """Log `exc` under a new error id, which it returns.

    An unreachable database is expected during an outage: one WARNING line,
    no traceback. Anything else is logged as an ERROR with its traceback.
    """
    error_id = new_error_id()
    if is_database_unavailable(exc):
        logger.warning(
            "%s unavailable (error_id=%s): %s: %s",
            what,
            error_id,
            type(exc).__name__,
            _first_line(exc),
        )
    else:
        logger.error(
            "%s unavailable (error_id=%s): %s", what, error_id, type(exc).__name__, exc_info=exc
        )
    return error_id


@contextlib.contextmanager
def database_errors() -> Iterator[None]:
    """Turn an unreachable database inside the block into a 503 with an error id."""
    try:
        yield
    except HTTPException:
        raise
    except Exception as exc:
        if is_database_unavailable(exc):
            raise unavailable("Database", exc) from exc
        raise


def validate_thread_id(thread_id: str, runtime: str) -> str:
    """The thread id in canonical form, or 422 for an id outside the accepted form.

    LangGraph Server needs a UUID; it is canonicalised (lower case, hyphens)
    so one thread never has two spellings, e.g. for the run lock.
    """
    if not valid_thread_id(thread_id):
        raise HTTPException(
            status_code=422,
            detail="thread_id must be 1-128 letters, digits or '_ . : -'.",
        )
    if runtime == LANGGRAPH_SERVER:
        try:
            return str(uuid.UUID(thread_id))
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="thread_id must be a UUID under the langgraph-server runtime.",
            ) from None
    return thread_id


def http_status(exc: BaseException) -> int | None:
    """The HTTP status of an SDK/httpx error (`NotFoundError`, `ConflictError`, ...), if any."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


@dataclass
class ChatRequest:
    message: str
    thread_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    forward_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class _RunState:
    text: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    seen_ai_ids: set[str] = field(default_factory=set)
    server_run_id: str | None = None
    # The graph's interrupts (`{"id", "value"}`) when the run paused.
    interrupts: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Resume:
    """A paused run to resume (`ChatRuntime.decide`): the resume value per interrupt id."""

    values: dict[str, Any]
    approval: ApprovalRecord
    decision: str


class ApprovalPending(Exception):
    """The thread has a pending approval: no new message until it is decided or expires.

    HTTP 409 `{"code": "approval_pending"}` on `/chat`.
    """

    def __init__(self, thread_id: str, approvals: list[dict[str, Any]]) -> None:
        super().__init__(
            "This thread is waiting for the approval of an action; decide it "
            "(POST /threads/{thread_id}/approvals/{approval_id}) or wait until it expires."
        )
        self.thread_id = thread_id
        self.approvals = approvals


class ApprovalError(Exception):
    """A decision that cannot be taken: `status_code` 404, 403, 409 or 410 with a `code`.

    The app answers `{"code", "detail", ...extra}` (`status`: the approval's
    status for 409 and 410).
    """

    def __init__(self, status_code: int, code: str, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail, **self.extra}


class RunTimeout(Exception):
    """The run passed `RUN_TIMEOUT_S`."""


# ---------------------------------------------------------------------------
# Message accessors that work for LangChain objects and the server's JSON dicts
# ---------------------------------------------------------------------------


def _get(m: Any, key: str, default: Any = None) -> Any:
    if isinstance(m, Mapping):
        return m.get(key, default)
    return getattr(m, key, default)


def _msg_type(m: Any) -> str:
    return str(_get(m, "type", "") or "")


def _is_ai(m: Any) -> bool:
    return _msg_type(m) in ("ai", "AIMessage", "AIMessageChunk")


def _is_tool(m: Any) -> bool:
    return _msg_type(m) in ("tool", "ToolMessage", "ToolMessageChunk")


def _is_human(m: Any) -> bool:
    return _msg_type(m) in ("human", "HumanMessage")


def _role(m: Any) -> str:
    if _is_ai(m):
        return "assistant"
    if _is_tool(m):
        return "tool"
    if _is_human(m):
        return "user"
    t = _msg_type(m)
    return "system" if t in ("system", "SystemMessage") else t


def tool_calls_of(m: Any) -> list[Any]:
    """Every tool call of an assistant message, in the order providers are sent them.

    The calls whose arguments did not parse (`invalid_tool_calls`, marked
    `"type": "invalid_tool_call"`) count too: LangChain sends them back to
    the provider as tool calls (langchain-openai does), so each needs a
    result like any other call.
    """
    if not _is_ai(m):
        return []
    calls = list(_get(m, "tool_calls") or [])
    for call in _get(m, "invalid_tool_calls") or []:
        if isinstance(call, Mapping) and call.get("type") != INVALID_TOOL_CALL_TYPE:
            call = {**call, "type": INVALID_TOOL_CALL_TYPE}
        calls.append(call)
    return calls


def is_invalid_tool_call(call: Any) -> bool:
    """Whether `call` is one whose arguments did not parse (never run)."""
    return _get(call, "type") == INVALID_TOOL_CALL_TYPE


def open_call_result_text(call: Any, reason: str) -> str:
    """The error result for a call that has none: `reason`, or why an invalid call never ran."""
    return INVALID_TOOL_CALL_RESULT if is_invalid_tool_call(call) else reason


def _call_id(call: Any) -> str:
    return str(_get(call, "id") or "")


def _call_args(call: Any) -> dict[str, Any]:
    """A call's arguments as a dict; `{}` for a call whose arguments did not parse."""
    args = _get(call, "args")
    return dict(args) if isinstance(args, Mapping) else {}


def _iter_messages(update: Any) -> Iterator[Any]:
    if isinstance(update, Mapping):
        messages = update.get("messages")
        if isinstance(messages, list):
            yield from messages
        elif messages is not None:
            yield messages


def _accumulate_usage(state: _RunState, m: Any) -> None:
    usage = _get(m, "usage_metadata") or {}
    if isinstance(usage, Mapping):
        state.input_tokens += int(usage.get("input_tokens") or 0)
        state.output_tokens += int(usage.get("output_tokens") or 0)


# The key of an `updates` stream item that carries the graph's interrupts.
INTERRUPT_KEY = "__interrupt__"


def interrupts_of(value: Any) -> list[dict[str, Any]]:
    """Interrupts as `{"id", "value"}`, from LangGraph `Interrupt` objects or the server's JSON."""
    items = value if isinstance(value, list | tuple) else [value]
    out: list[dict[str, Any]] = []
    for item in items:
        interrupt_id = _get(item, "id")
        if interrupt_id:
            out.append({"id": str(interrupt_id), "value": _get(item, "value")})
    return out


def map_stream_item(mode: str, data: Any, state: _RunState) -> Iterator[tuple[str, dict[str, Any]]]:
    """Map one LangGraph stream item (`messages` or `updates` mode) to chat events.

    The graph's interrupts (an `updates` item under `__interrupt__`) are
    collected in `state.interrupts`, not sent: the run's end reports them.
    """
    if mode == "messages":
        chunk = data[0] if isinstance(data, list | tuple) and data else data
        if _is_ai(chunk) and not (
            _get(chunk, "tool_call_chunks")
            or _get(chunk, "tool_calls")
            or _get(chunk, "invalid_tool_calls")
        ):
            text = content_to_text(_get(chunk, "content", ""))
            if text:
                state.text.append(text)
                yield EVENT_DELTA, {"text": text}
        return
    if mode != "updates" or not isinstance(data, Mapping):
        return
    for key, update in data.items():
        if key == INTERRUPT_KEY:
            state.interrupts.extend(interrupts_of(update))
            continue
        for m in _iter_messages(update):
            if _is_ai(m):
                msg_id = str(_get(m, "id") or "")
                if msg_id and msg_id in state.seen_ai_ids:
                    continue
                if msg_id:
                    state.seen_ai_ids.add(msg_id)
                _accumulate_usage(state, m)
                for call in tool_calls_of(m):
                    entry = {
                        "id": str(_get(call, "id") or uuid.uuid4()),
                        "name": str(_get(call, "name") or ""),
                        "args": _call_args(call),
                    }
                    state.tool_calls.append({**entry, "result": None, "is_error": False})
                    yield EVENT_TOOL_CALL, entry
            elif _is_tool(m):
                call_id = str(_get(m, "tool_call_id") or "")
                result = content_to_text(_get(m, "content", ""))
                is_error = str(_get(m, "status") or "success") == "error"
                for tc in state.tool_calls:
                    if tc["id"] == call_id:
                        tc["result"] = result
                        tc["is_error"] = is_error
                yield (
                    EVENT_TOOL_RESULT,
                    {
                        "id": call_id,
                        "name": str(_get(m, "name") or ""),
                        "result": result,
                        "is_error": is_error,
                    },
                )


def trace_metadata(
    thread_id: str, run_id: str, principal: Principal, client_metadata: Mapping[str, Any]
) -> dict[str, Any]:
    """Run metadata for traces (and, for its scalar keys, checkpoints).

    The server-set ids cannot be overwritten: client metadata only ever sits
    under `client_metadata`, a nested object, which LangGraph does not copy
    into checkpoint metadata. It is included only under `TRACE_CAPTURE=full`.
    """
    meta: dict[str, Any] = {
        "thread_id": thread_id,
        "run_id": run_id,
        "principal_hash": principal.hashed_id(),
    }
    if client_metadata and capture_full():
        meta["client_metadata"] = dict(client_metadata)
    return meta


def serialize_message(m: Any, *, include_tool_args: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": _get(m, "id"),
        "role": _role(m),
        "content": content_to_text(_get(m, "content", "")),
    }
    if _is_ai(m):
        calls = []
        for call in tool_calls_of(m):
            entry: dict[str, Any] = {"id": _get(call, "id"), "name": _get(call, "name")}
            if include_tool_args:
                entry["args"] = _call_args(call)
            calls.append(entry)
        if calls:
            out["tool_calls"] = calls
    if _is_tool(m):
        out["tool_call_id"] = _get(m, "tool_call_id")
        out["name"] = _get(m, "name")
        out["is_error"] = str(_get(m, "status") or "success") == "error"
    return out


def dangling_tool_calls(messages: list[Any]) -> list[Any]:
    """Tool calls of the last assistant message with no result right after it.

    Only results that directly follow the call count: a result placed after
    a later message does not make the history valid for model providers.
    """
    for i in range(len(messages) - 1, -1, -1):
        if _is_ai(messages[i]):
            answered: Counter[str] = Counter()
            for m in messages[i + 1 :]:
                if not _is_tool(m):
                    break
                answered[str(_get(m, "tool_call_id") or "")] += 1
            open_calls = []
            for c in tool_calls_of(messages[i]):
                if answered[_call_id(c)] > 0:
                    answered[_call_id(c)] -= 1
                else:
                    open_calls.append(c)
            return open_calls
    return []


@dataclass
class HistoryRepair:
    """A tool-call history put right: see `repair_tool_history`."""

    messages: list[Any]  # the whole corrected history
    added: list[Any]  # error results created for calls that had none
    append_only: bool  # `messages` is the old history plus `added` at its end


def repair_tool_history(
    messages: list[Any], make_result: Callable[[Any], Any]
) -> HistoryRepair | None:
    """The history with every tool call followed by its result, or None when it already is.

    Model providers reject an assistant message whose tool calls are not
    answered right after it, and a tool result that answers no call. Every
    call counts, the ones whose arguments did not parse included
    (`tool_calls_of`). Calls and results are paired by position, turn by
    turn, since tool-call ids repeat across turns (a model may number its
    calls `call_0`, `call_1`, ... in every message) and can repeat within one
    message (some OpenAI-compatible servers send the same id, or an empty
    one, for parallel calls):

    * a call's result is a tool message with its id in the block of tool
      messages right after the call's assistant message, one per call with
      that id; these are kept where they are, in their order;
    * a call without one takes the first unclaimed tool message with its id
      that sits after the call and before the next assistant message that
      makes a call with the same id (a result a crash or an old repair left
      behind a later message), moved right after the call;
    * a call with neither gets `make_result(call)` (an error result);
    * a tool message no call claims is dropped.

    A history in which every call is answered right after it, and every tool
    message answers a call, is returned as None and never rewritten.
    """
    count = len(messages)

    def call_ids(m: Any) -> list[str]:
        return [_call_id(call) for call in tool_calls_of(m)]

    def result_id(m: Any) -> str:
        return str(_get(m, "tool_call_id") or "")

    # Pass 1: the results that answer an assistant message right after it.
    claimed: set[int] = set()
    direct: dict[int, list[int]] = {}  # assistant index -> indexes of its direct results
    for i, m in enumerate(messages):
        if not _is_ai(m):
            continue
        pending = Counter(call_ids(m))
        block: list[int] = []
        j = i + 1
        while j < count and _is_tool(messages[j]):
            if pending[result_id(messages[j])] > 0:
                pending[result_id(messages[j])] -= 1
                block.append(j)
                claimed.add(j)
            j += 1
        direct[i] = block

    def later_result(i: int, call_id: str) -> int | None:
        """An unclaimed result for `call_id` after message i, before the id is called again."""
        for k in range(i + 1, count):
            m = messages[k]
            if _is_ai(m) and call_id in call_ids(m):
                return None
            if _is_tool(m) and k not in claimed and result_id(m) == call_id:
                return k
        return None

    # Pass 2: rebuild, each assistant message followed by its results.
    out: list[Any] = []
    added: list[Any] = []
    for i, m in enumerate(messages):
        if _is_tool(m):
            continue  # placed after its call below, or dropped
        out.append(m)
        if not _is_ai(m):
            continue
        answered = Counter(result_id(messages[k]) for k in direct[i])
        out.extend(messages[k] for k in direct[i])
        for call in tool_calls_of(m):
            call_id = _call_id(call)
            if answered[call_id] > 0:
                answered[call_id] -= 1
                continue
            k = later_result(i, call_id)
            if k is not None:
                claimed.add(k)
                out.append(messages[k])
            else:
                result = make_result(call)
                out.append(result)
                added.append(result)
    if len(out) == count and all(a is b for a, b in zip(out, messages, strict=True)):
        return None
    append_only = len(out) >= count and all(
        a is b for a, b in zip(out[:count], messages, strict=True)
    )
    return HistoryRepair(messages=out, added=added, append_only=append_only)


def _server_message(m: Any) -> dict[str, Any]:
    """A server state message as a dict the server turns back into the same message.

    The server rebuilds messages from dicts by their known keys and puts any
    other key into `additional_kwargs`; only the keys that matter for the
    history are kept. It has no key for `invalid_tool_calls`, so a call
    whose arguments did not parse is kept as a call with no arguments: its
    error result must still answer a call the provider is sent.
    """
    if not isinstance(m, Mapping):
        return m
    keep = ["type", "content", "id", "name", "additional_kwargs", "response_metadata"]
    if _is_tool(m):
        keep.extend(("tool_call_id", "status", "artifact"))
    out = {k: m[k] for k in keep if m.get(k) is not None}
    if _is_ai(m):
        calls = [
            {
                "name": str(_get(c, "name") or ""),
                "args": {},
                "id": _get(c, "id"),
                "type": "tool_call",
            }
            if is_invalid_tool_call(c)
            else c
            for c in tool_calls_of(m)
        ]
        if calls:
            out["tool_calls"] = calls
    return out


# The remove-everything marker of LangGraph's `add_messages` reducer
# (`langgraph.graph.message.REMOVE_ALL_MESSAGES`).
REMOVE_ALL_MESSAGES = "__remove_all__"
# How long a step-limit reply waits for the server to mark our run done.
SERVER_IDLE_WAIT_S = 3.0


def _server_busy(thread: Any) -> bool:
    return isinstance(thread, Mapping) and thread.get("status") == "busy"


def thread_busy_error() -> dict[str, Any]:
    return {"code": THREAD_BUSY, "message": "This thread already has a run in progress."}


def _lease_lost(exc: BaseException | None) -> LeaseLost | None:
    """The `LeaseLost` behind `exc` (itself, or what it was raised from), if any."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, LeaseLost):
            return exc
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return None


def _is_recursion_error(exc: BaseException) -> bool:
    if type(exc).__name__ == "GraphRecursionError":
        return True
    # Under langgraph-server the error arrives as the stream's `error` part.
    return isinstance(exc, _ServerRunError) and exc.error_type == "GraphRecursionError"


class _ServerRunError(RuntimeError):
    """An `error` part of a LangGraph Server run stream."""

    def __init__(self, data: Any) -> None:
        super().__init__(str(data))
        self.error_type = str(data.get("error") or "") if isinstance(data, Mapping) else ""


# ---------------------------------------------------------------------------
# Pacing: run timeout and heartbeats around a stream of graph events
# ---------------------------------------------------------------------------

_ITEM, _DONE, _FAILED, _IDLE = "item", "done", "failed", "idle"


class _Pump:
    """Drive an async iterator in a task of its own and hand its items over a queue.

    The consumer can then wait with a timeout (for heartbeats and the run
    deadline) without cancelling the graph mid-step, and cancel it outright
    when the run times out or the client leaves.
    """

    def __init__(self, source: AsyncIterator[Any]) -> None:
        self._queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=64)
        self._interrupted: BaseException | None = None
        self._task = asyncio.ensure_future(self._run(source))

    async def _run(self, source: AsyncIterator[Any]) -> None:
        try:
            async for item in source:
                await self._queue.put((_ITEM, item))
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await self._queue.put((_FAILED, exc))
            return
        await self._queue.put((_DONE, None))

    def interrupt(self, exc: BaseException) -> None:
        """Stop the source now; the consumer's next `next()` fails with `exc`."""
        if self._interrupted is not None:
            return
        self._interrupted = exc
        self.cancel()
        with contextlib.suppress(asyncio.QueueFull):  # a full queue wakes nobody anyway
            self._queue.put_nowait((_FAILED, exc))

    async def next(self, timeout: float) -> tuple[str, Any]:
        if self._interrupted is not None:
            return _FAILED, self._interrupted
        try:
            item = await asyncio.wait_for(self._queue.get(), timeout)
        except TimeoutError:
            return _IDLE, None
        if self._interrupted is not None:
            return _FAILED, self._interrupted
        return item

    @property
    def task(self) -> asyncio.Future[None]:
        return self._task

    def cancel(self) -> None:
        if not self._task.done():
            self._task.cancel()

    async def close(self, timeout: float = 10) -> bool:
        """Cancel the source and wait up to `timeout` s for it to stop; True when it has."""
        self.cancel()
        if not self._task.done():
            await asyncio.wait({self._task}, timeout=timeout)
        return self._task.done()


class ChatRuntime:
    """Process-wide chat runtime, started and stopped by the app lifespan."""

    def __init__(self) -> None:
        self.runtime = detect_runtime()
        self.db: Database | None = None
        self.runs: RunStore | None = None
        self.threads: ThreadStore | None = None
        self.approvals: ApprovalStore | None = None
        self.locks: ThreadLocks = ThreadLocks()
        self._exit: AsyncExitStack | None = None
        self._retention_task: asyncio.Task[None] | None = None
        self._init_task: asyncio.Task[None] | None = None
        self._maintenance_task: asyncio.Task[None] | None = None
        self._approvals_task: asyncio.Task[None] | None = None
        self._saver: Any = None
        # Final run records whose write failed, retried by the maintenance loop.
        self._unrecorded: deque[RunRecord] = deque(maxlen=MAX_UNRECORDED_RUNS)
        self._was_ready: bool | None = None
        self.started = False
        # True from startup until the database schema is set up (postgres).
        self.initialising = False

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Bind storage and start the background work; never waits for the database.

        Configuration errors (an unknown `CHECKPOINTER`, a missing or
        unparsable DSN) stop startup. An unreachable database does not: the
        schema setup is retried in the background (`initialising`), `/ready`
        answers 503 and requests get 503 until it succeeds.
        """
        if self.started:
            return
        self.runtime = detect_runtime()
        self._exit = AsyncExitStack()
        try:
            if self.runtime == FASTAPI:
                self.db = Database.from_env()
            else:
                # The server binds persistence; run records go to its Postgres
                # (DATABASE_URI) when it has one.
                self.db = Database.for_server()
            await self.db.open_pool()
            self._exit.push_async_callback(self.db.close)
            self.locks = ThreadLocks(
                self.db.dsn if self.db.is_postgres else None,
                table=self.db.locks_table,
                health=self.db.health,
            )
            self._exit.push_async_callback(self.locks.close)
            if self.runtime == FASTAPI:
                from {{cookiecutter.agent_directory}}.agent import graph

                if self.db.is_postgres:
                    # Set up with the app tables once the database answers.
                    self._saver = postgres_saver(self.db.pool, fence=self.locks.fence)
                else:
                    self._saver = await self._exit.enter_async_context(get_checkpointer())
                graph.checkpointer = self._saver
            self.runs = RunStore(self.db)
            self.threads = ThreadStore(self.db)
            self.approvals = ApprovalStore(self.db)
            # The ledger the API client marks approvals used in, once, before a
            # gated call is sent (the graph runs in this process under both runtimes).
            set_approval_ledger(self.approvals)
        except BaseException:
            await self._exit.aclose()
            self._exit = None
            raise
        self.started = True
        self._check_step_budget()
        if self.db.is_postgres:
            self.initialising = True
            self._init_task = asyncio.create_task(self._initialise(), name="database-setup")
        else:
            self._storage_ready()
        logger.info(
            "chat runtime started: runtime=%s checkpointer=%s retention_days=%s",
            self.runtime,
            self.checkpointer_kind(),
            retention_days(),
        )

    async def _initialise(self) -> None:
        """Set up the schema, retrying with back-off until the database answers."""
        assert self.db is not None
        extra = self._saver.setup if self.runtime == FASTAPI and self.db.is_postgres else None
        delay = INIT_RETRY_FIRST_S
        attempt = 0
        while True:
            attempt += 1
            try:
                await self.db.setup(extra)
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if attempt == 1 or attempt % 30 == 0:  # about once a minute
                    if is_database_unavailable(exc):
                        logger.warning(
                            "database not reachable yet (%s: %s); not ready, retrying every %g s",
                            type(exc).__name__,
                            _first_line(exc),
                            INIT_RETRY_MAX_S,
                        )
                    else:
                        logger.error("database setup failed; retrying", exc_info=exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, INIT_RETRY_MAX_S)
        self.initialising = False
        if attempt > 1:
            logger.info("database set up after %d attempts", attempt)
        self._storage_ready()

    def _storage_ready(self) -> None:
        """Start the background work that needs the database."""
        days = retention_days()
        if days > 0:
            self._retention_task = asyncio.create_task(self._retention_loop(days))
        if self.db is not None and self.db.is_postgres:
            self._maintenance_task = asyncio.create_task(self._maintenance_loop())
        self._approvals_task = asyncio.create_task(self._approvals_loop())

    async def stop(self) -> None:
        tasks = [
            self._init_task,
            self._maintenance_task,
            self._retention_task,
            self._approvals_task,
        ]
        self._init_task = self._maintenance_task = self._retention_task = None
        self._approvals_task = None
        for task in tasks:
            if task is not None:
                task.cancel()
        await asyncio.gather(*(t for t in tasks if t is not None), return_exceptions=True)
        if self.approvals is not None and approval_ledger() is self.approvals:
            set_approval_ledger(None)
        if self._exit is not None:
            await self._exit.aclose()
            self._exit = None
        self.started = False
        self.initialising = False

    def checkpointer_kind(self) -> str:
        if self.runtime == LANGGRAPH_SERVER:
            return "postgres" if is_postgres_url(os.environ.get("DATABASE_URI")) else "memory"
        return checkpointer_kind()

    def _check_step_budget(self) -> None:
        """Warn when an API's `max_calls_per_run` cannot be reached within `RECURSION_LIMIT`."""
        try:
            from {{cookiecutter.agent_directory}}.app_utils.api_client import load_policy

            policy = load_policy()
        except Exception:  # no or invalid policy: reported where it is used
            return
        limit = recursion_limit()
        fits = sequential_tool_calls(limit)
        for name, settings in sorted(policy.apis.items()):
            max_calls = (settings.get("limits") or {}).get("max_calls_per_run")
            if isinstance(max_calls, int) and max_calls > fits:
                logger.warning(
                    "RECURSION_LIMIT=%d allows about %d sequential tool calls per run, fewer "
                    "than limits.max_calls_per_run=%d of API %r: runs stop at the step limit "
                    "first. Set RECURSION_LIMIT to at least %d, or lower the API's limit.",
                    limit,
                    fits,
                    max_calls,
                    name,
                    steps_for_tool_calls(max_calls),
                )

    def _require_storage(self) -> None:
        """503 while the database is not set up yet (startup during an outage)."""
        if not self.started or self.initialising:
            raise unavailable("Database", StorageNotReady("the database is not set up yet"))

    async def ready(self, timeout: float = 2.0) -> bool:
        """True when the runtime's storage answers a trivial query within `timeout` seconds."""
        ok = False
        reason = "starting"
        if self.started and self.db is not None and not self.initialising:
            try:
                async with asyncio.timeout(timeout):
                    await self.db.ping()
                    if self.runtime == LANGGRAPH_SERVER:
                        await self._sdk_client({}).assistants.search(limit=1)
                ok = True
            except Exception as exc:
                reason = type(exc).__name__
        elif self.initialising:
            reason = "the database is not set up yet"
        if ok != self._was_ready:  # log changes only, not every probe
            if ok:
                logger.info("ready")
            else:
                logger.warning("not ready: %s", reason)
        self._was_ready = ok
        return ok

    async def _maintenance_loop(self) -> None:
        """Every `RECONCILE_INTERVAL_S`: write failed run records, close runs of dead processes."""
        failing = False
        while True:
            try:
                await self.reconcile_runs()
                if failing:
                    failing = False
                    logger.info("run reconciliation works again")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not failing:  # once per outage, not every round
                    failing = True
                    logger.warning(
                        "run reconciliation failed (%s); retrying every %g s",
                        type(exc).__name__,
                        RECONCILE_INTERVAL_S,
                    )
            await asyncio.sleep(RECONCILE_INTERVAL_S)

    async def _approvals_loop(self) -> None:
        """Every `SWEEP_INTERVAL_S`: mark pending approvals past their expiry `expired`."""
        failing = False
        while True:
            try:
                await self.sweep_approvals()
                failing = False
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not failing:
                    failing = True
                    logger.warning(
                        "approval expiry sweep failed (%s); retrying every %g s",
                        type(exc).__name__,
                        SWEEP_INTERVAL_S,
                    )
            await asyncio.sleep(SWEEP_INTERVAL_S)

    async def sweep_approvals(self) -> list[ApprovalRecord]:
        """Mark pending approvals past their expiry `expired` (= rejected); return them."""
        if self.approvals is None:
            return []
        expired = await self.approvals.expire_due()
        if expired:
            metrics.observe_approvals("expired", len(expired))
            logger.info("%d pending approvals expired", len(expired))
        return expired

    async def reconcile_runs(self, grace_s: float | None = None) -> list[str]:
        """Write run records that failed earlier, then mark runs of dead processes `interrupted`."""
        if self.runs is None:
            return []
        grace_s = RECONCILE_GRACE_S if grace_s is None else grace_s
        while self._unrecorded:
            record = self._unrecorded[0]
            await self.runs.record(record)
            self._unrecorded.popleft()
        run_ids = await self.runs.reconcile(grace_s)
        if run_ids:
            metrics.observe_interrupted_runs(len(run_ids))
            logger.warning(
                "%d runs never finished (their process stopped); recorded as interrupted: %s",
                len(run_ids),
                ", ".join(run_ids[:20]),
            )
        return run_ids

    # -- threads -----------------------------------------------------------

    async def resolve_thread(self, principal: Principal, req: ChatRequest) -> str:
        """The thread id for this request, after the ownership check (403 before streaming)."""
        if req.thread_id is not None:
            req.thread_id = validate_thread_id(req.thread_id, self.runtime)
        self._require_storage()
        if self.runtime == LANGGRAPH_SERVER:
            return await self._server_resolve_thread(principal, req)
        thread_id = req.thread_id or str(uuid.uuid4())
        await self._ensure_owner(principal, thread_id)
        return thread_id

    async def _ensure_owner(self, principal: Principal, thread_id: str) -> None:
        """fastapi: claim the thread for `principal` when it has no owner, else check the owner.

        403 for someone else's thread, and for a thread id whose checkpoints
        have no owner row (none should exist; this keeps any that do from
        passing to whoever claims the id next).
        """
        assert self.threads is not None
        try:
            await self.threads.ensure(thread_id, principal, has_state=self._has_checkpoints)
        except HTTPException:
            raise
        except Exception as exc:  # the database: a 503 naming an error id, not its text
            raise unavailable("Database", exc) from exc

    async def _has_checkpoints(self, thread_id: str) -> bool:
        from {{cookiecutter.agent_directory}}.agent import graph

        saver = graph.checkpointer
        if saver is None or not hasattr(saver, "aget_tuple"):
            return False
        return await saver.aget_tuple({"configurable": {"thread_id": thread_id}}) is not None

    async def acquire_thread(
        self, thread_id: str, principal: Principal | None = None
    ) -> ThreadLease:
        """The thread's run lock; `ThreadBusy` when a run is in progress on it.

        With `principal` (the sender of the run about to start), the owner is
        checked again once the lock is held (fastapi): a DELETE can take the
        lock, and remove the thread, between `resolve_thread` and this call.
        The run then starts the thread afresh for its sender, or gets 403 when
        another principal claimed the id meanwhile, instead of writing a turn
        that no owner row covers. Deletion and retention hold the same lock,
        so the owner cannot change while the run holds it. (Under
        langgraph-server the server keeps thread and state together: a run on
        a deleted thread fails there with 404.)
        """
        self._require_storage()
        try:
            lease = await self.locks.acquire(thread_id)
        except ThreadBusy:
            raise
        except Exception as exc:
            raise unavailable("Database", exc) from exc
        if principal is not None and self.runtime == FASTAPI:
            try:
                await self._ensure_owner(principal, thread_id)
            except BaseException:
                await lease.release()
                raise
        return lease

    async def list_threads(
        self,
        principal: Principal,
        *,
        limit: int,
        offset: int,
        forward_headers: Mapping[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """The caller's threads (every thread for a read-across role), most recent first."""
        self._require_storage()
        if self.runtime == LANGGRAPH_SERVER:
            return await self._server_list_threads(principal, limit, offset, forward_headers or {})
        assert self.threads is not None
        with database_errors():
            records = await self.threads.list_for(principal, limit=limit, offset=offset)
        return [r.public() for r in records]

    async def delete_thread(
        self,
        principal: Principal,
        thread_id: str,
        forward_headers: Mapping[str, str] | None = None,
    ) -> None:
        """Delete a thread with its checkpoints and run records: the owner only.

        404 for an unknown thread, 403 for someone else's, `ThreadBusy` while
        a run is in progress on it. The owner is checked before the run lock
        (a stranger never takes it) and again once it is held: the thread may
        have been deleted, and its id claimed by someone else, in between.
        """
        thread_id = validate_thread_id(thread_id, self.runtime)
        self._require_storage()
        headers = forward_headers or {}
        with database_errors():
            self._assert_deletable(principal, await self._thread_record(thread_id, headers))
        lease = await self.acquire_thread(thread_id)
        try:
            with database_errors():
                self._assert_deletable(principal, await self._thread_record(thread_id, headers))
                await self._delete_thread_data(thread_id, headers)
        finally:
            await lease.release()
        logger.info("thread deleted", extra={"thread_id": thread_id})

    async def _thread_record(
        self, thread_id: str, forward_headers: Mapping[str, str]
    ) -> ThreadRecord | None:
        if self.runtime == LANGGRAPH_SERVER:
            return await self._server_thread_record(self._sdk_client(forward_headers), thread_id)
        assert self.threads is not None
        return await self.threads.get(thread_id)

    @staticmethod
    def _assert_deletable(principal: Principal, record: ThreadRecord | None) -> None:
        if record is None:
            raise HTTPException(status_code=404, detail="Unknown thread.")
        assert_owner(principal, record)

    async def _delete_thread_data(
        self, thread_id: str, forward_headers: Mapping[str, str] | None = None
    ) -> None:
        if self.runtime == LANGGRAPH_SERVER:
            client = self._sdk_client(forward_headers or {})
            try:
                await client.threads.delete(thread_id)
            except Exception as exc:
                if http_status(exc) != 404:
                    raise unavailable("LangGraph Server", exc) from exc
            # The app keeps no thread rows here; tell the listeners itself (the
            # retention purge lands here; the A2A task store drops the thread's
            # tasks). Under fastapi `ThreadStore.delete` below does it.
            await thread_deleted(thread_id)
        else:
            from {{cookiecutter.agent_directory}}.agent import graph

            if graph.checkpointer is not None:
                await graph.checkpointer.adelete_thread(thread_id)
        if self.runs is not None:
            await self.runs.delete_for_thread(thread_id)
        if self.approvals is not None:
            await self.approvals.delete_for_thread(thread_id)
        if self.threads is not None and self.runtime == FASTAPI:
            await self.threads.delete(thread_id)

    async def forget_thread_runs(self, thread_id: str) -> None:
        """Drop a deleted thread's run records and approvals (after the server's own
        DELETE succeeded)."""
        if self.runs is None and self.approvals is None:
            return
        try:
            thread_id = str(uuid.UUID(thread_id))  # run records use the canonical form
        except ValueError:
            return
        if self.runs is not None:
            await self.runs.delete_for_thread(thread_id)
        if self.approvals is not None:
            await self.approvals.delete_for_thread(thread_id)
        logger.info(
            "run records and approvals of a deleted thread removed", extra={"thread_id": thread_id}
        )

    # -- approvals -------------------------------------------------------------

    async def pending_approvals(self, thread_id: str) -> list[dict[str, Any]]:
        """The thread's pending approvals as its owner sees them (oldest first)."""
        if self.approvals is None:
            return []
        with database_errors():
            return [r.public() for r in await self.approvals.pending_for_thread(thread_id)]

    async def assert_no_pending_approval(self, thread_id: str) -> None:
        """`ApprovalPending` (409) while the thread waits for an approval decision."""
        pending = await self.pending_approvals(thread_id)
        if pending:
            raise ApprovalPending(thread_id, pending)

    async def thread_approvals(
        self,
        principal: Principal,
        thread_id: str,
        forward_headers: Mapping[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """A thread's approvals the caller may see, newest first.

        The owner and read-across roles see every one; a decider the ones it
        may decide. Anyone else gets 403, an unknown thread 404.
        """
        thread_id = validate_thread_id(thread_id, self.runtime)
        self._require_storage()
        assert self.approvals is not None
        with database_errors():
            await self.sweep_approvals()
            thread = await self._thread_record(thread_id, forward_headers or {})
        if thread is None:
            raise HTTPException(status_code=404, detail="Unknown thread.")
        with database_errors():
            records = await self.approvals.for_thread(thread_id)
        owner = thread.principal_id
        visible = [r for r in records if may_view(principal, owner, r.approvers)]
        if not visible and not (is_owner(principal, thread) or reads_across(principal)):
            raise HTTPException(status_code=403, detail="This thread belongs to another principal.")
        return [r.public(include_call=sees_call(principal, owner, r.approvers)) for r in visible]

    async def visible_approvals(
        self, principal: Principal, *, status: str | None, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        """Approvals across threads the caller requested, may decide, or reads across."""
        self._require_storage()
        assert self.approvals is not None
        with database_errors():
            await self.sweep_approvals()
            records = await self.approvals.visible(
                principal, status=status, limit=limit, offset=offset
            )
        # The requester and the deciders see the call; read-across roles only
        # under TRACE_CAPTURE=full (as `sees_call`).
        own = principal.hashed_id()
        roles = {f"role:{r}" for r in principal.roles}
        return [
            r.public(
                include_call=r.requester_hash == own
                or bool(roles & set(r.approvers))
                or capture_full()
            )
            for r in records
        ]

    async def decide(
        self,
        principal: Principal,
        thread_id: str,
        approval_id: str,
        decision: str,
        comment: str | None = None,
        forward_headers: Mapping[str, str] | None = None,
    ) -> tuple[ThreadLease, Resume, Principal]:
        """Approve or reject a pending approval; the thread's run lock and what to resume.

        `ApprovalError` 404 (no such approval on this thread), 403 (the caller
        may not decide it), 410 (expired), 409 (decided already, or the run no
        longer waits for it); `ThreadBusy` while a run is in progress on the
        thread. The decision is one atomic change under the thread's run lock,
        so of two concurrent decisions one wins. Returns the held lease (the
        caller streams the resumed run with it), the resume values and the
        principal the resumed run acts as (the requester, never the decider).
        """
        thread_id = validate_thread_id(thread_id, self.runtime)
        self._require_storage()
        assert self.approvals is not None
        verdict = DECISIONS.get(decision)
        if verdict is None:
            raise HTTPException(status_code=422, detail="decision must be 'approve' or 'reject'.")
        headers = forward_headers or {}
        with database_errors():
            await self.sweep_approvals()
            record = await self.approvals.get(approval_id)
        if record is None or record.thread_id != thread_id:
            raise ApprovalError(404, "approval_not_found", "Unknown approval.")
        with database_errors():
            thread = await self._thread_record(thread_id, headers)
        if thread is None:
            raise ApprovalError(404, "approval_not_found", "Unknown approval.")
        if not may_decide(principal, thread.principal_id, record.approvers):
            raise ApprovalError(
                403, "not_an_approver", "You may not decide this approval (see its approvers)."
            )
        self._check_decidable(record)
        lease = await self.acquire_thread(thread_id)
        try:
            with database_errors():
                paused = await self._paused_interrupts(thread_id, headers)
                if record.interrupt_id not in paused:
                    # The thread went on without this approval (nothing waits for it).
                    if await self.approvals.expire(approval_id) is not None:
                        metrics.observe_approvals("expired")
                    raise ApprovalError(
                        409,
                        CODE_NOT_PENDING,
                        "The run no longer waits for this approval.",
                        status=EXPIRED,
                    )
                decided = await self.approvals.decide(
                    approval_id, verdict, principal.hashed_id(), comment
                )
                if decided is None:
                    current = await self.approvals.get(approval_id)
                    if current is None:
                        raise ApprovalError(404, "approval_not_found", "Unknown approval.")
                    self._check_decidable(current)
                    raise ApprovalError(
                        409, CODE_NOT_PENDING, "The approval is decided already.", status=PENDING
                    )
                metrics.observe_approvals(verdict)
                resume_as = DECISION_APPROVE if decision == APPROVE else DECISION_REJECT
                values = {decided.interrupt_id: decision_value(decided, resume_as)}
                # The paused run's other interrupts get their own approval's state
                # too: their tools run again on this resume, and each decision is
                # bound to its call whatever the policy now says about gating it.
                # An expired (or rejected) one ends its tool, a pending one pauses
                # again for the same approval, and an approved one whose run never
                # continued is sent only as an approval allows.
                latest: dict[str, ApprovalRecord] = {}
                for other in await self.approvals.for_thread(thread_id):  # newest first
                    latest.setdefault(other.interrupt_id, other)
                for interrupt_id in paused:
                    other = latest.get(interrupt_id)
                    if interrupt_id not in values and other is not None:
                        state = other.effective_status(self.approvals.now())
                        values[interrupt_id] = decision_value(other, RESUME_AS[state])
        except BaseException:
            await lease.release()
            raise
        logger.info(
            "approval decided: %s",
            verdict,
            extra={"thread_id": thread_id, "approval_id": approval_id},
        )
        acting = resume_principal(decided, thread.principal_id, principal)
        return lease, Resume(values=values, approval=decided, decision=decision), acting

    @staticmethod
    def _check_decidable(record: ApprovalRecord) -> None:
        status = record.effective_status()
        if status == EXPIRED:
            raise ApprovalError(410, CODE_EXPIRED, "The approval expired.", status=status)
        if status != PENDING:
            raise ApprovalError(
                409, CODE_NOT_PENDING, f"The approval is {status} already.", status=status
            )

    async def _paused_interrupts(
        self, thread_id: str, forward_headers: Mapping[str, str]
    ) -> dict[str, Any]:
        """The interrupts the thread's paused run waits on: id -> value."""
        if self.runtime == LANGGRAPH_SERVER:
            client = self._sdk_client(forward_headers)
            try:
                state = await client.threads.get_state(thread_id)
            except Exception as exc:
                if http_status(exc) == 404:
                    return {}
                raise unavailable("LangGraph Server", exc) from exc
            items = state.get("interrupts") if isinstance(state, Mapping) else None
            return {i["id"]: i["value"] for i in interrupts_of(items or [])}
        from {{cookiecutter.agent_directory}}.agent import graph

        snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        found = list(getattr(snapshot, "interrupts", None) or ())
        return {i["id"]: i["value"] for i in interrupts_of(found)}

    async def _record_approvals(
        self, principal: Principal, thread_id: str, run_id: str, interrupts: list[dict[str, Any]]
    ) -> list[ApprovalRecord]:
        """A pending approval per interrupt of a paused run (one already pending is kept)."""
        assert self.approvals is not None
        records: list[ApprovalRecord] = []
        for item in interrupts:
            record, created, superseded = await self.approvals.add(
                record_from_interrupt(
                    item["value"],
                    interrupt_id=item["id"],
                    thread_id=thread_id,
                    run_id=run_id,
                    requester=principal,
                    now=self.approvals.now(),
                )
            )
            if created:
                metrics.observe_approvals("requested")
            metrics.observe_approvals("expired", superseded)
            if all(r.approval_id != record.approval_id for r in records):
                records.append(record)
        logger.info("run paused for %d approval(s)", len(records), extra={"thread_id": thread_id})
        return records

    async def _approval_results(self, thread_id: str) -> dict[str, str]:
        """Error results, by tool call id, for calls a paused run left open (see the repair)."""
        if self.approvals is None:
            return {}
        texts: dict[str, str] = {}
        for record in await self.approvals.for_thread(thread_id):  # newest first
            text = (
                OPEN_CALL_SENT_UNSAVED
                if record.used_at is not None
                else OPEN_CALL_BY_APPROVAL.get(record.effective_status())
            )
            if record.tool_call_id and text and record.tool_call_id not in texts:
                texts[record.tool_call_id] = text
        return texts

    # -- retention -----------------------------------------------------------

    async def _retention_loop(self, days: int) -> None:
        await asyncio.sleep(RETENTION_FIRST_DELAY_S)
        while True:
            try:
                purged = 0
                for _ in range(RETENTION_MAX_BATCHES):  # a backlog drains over a few rounds
                    removed = await self.purge_expired(
                        days, batch=RETENTION_BATCH, sweep_run_records=False
                    )
                    purged += removed
                    if removed < RETENTION_BATCH:
                        break
                if purged:
                    logger.info("retention purge removed %d idle threads", purged)
                swept = await self.sweep_orphaned_runs(days, batch=RETENTION_BATCH)
                if swept:
                    logger.info("retention removed the run records of %d deleted threads", swept)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("retention purge failed; retrying in an hour")
            await asyncio.sleep(RETENTION_INTERVAL_S)

    async def purge_expired(
        self, days: int | None = None, *, batch: int = 500, sweep_run_records: bool = True
    ) -> int:
        """Delete threads (checkpoints, run records) idle for more than `days`; best effort.

        A thread with a run in progress is skipped this round, and so is one
        continued since it was listed: idleness is checked again under the
        thread's run lock, which a new turn needs too. Returns how many
        threads were removed. `sweep_run_records` also runs
        `sweep_orphaned_runs` (the hourly loop runs it once per round instead).
        """
        days = retention_days() if days is None else days
        if days <= 0:
            return 0
        cutoff = datetime.now(tz=UTC) - timedelta(days=days)
        if self.runtime == LANGGRAPH_SERVER:
            candidates = await self._server_idle_threads(cutoff, batch)
        else:
            assert self.threads is not None
            candidates = await self.threads.idle_before(cutoff.isoformat(), limit=batch)
        purged = 0
        for thread_id in candidates:
            try:
                lease = await self.locks.acquire(thread_id)
            except ThreadBusy:
                continue
            try:
                if not await self._still_idle(thread_id, cutoff):
                    continue
                await self._delete_thread_data(thread_id)
                purged += 1
            finally:
                await lease.release()
        if sweep_run_records:
            await self.sweep_orphaned_runs(days, batch=batch)
        return purged

    async def _still_idle(self, thread_id: str, cutoff: datetime) -> bool:
        """True when the thread still exists and has not been continued since `cutoff`."""
        if self.runtime == LANGGRAPH_SERVER:
            try:
                thread = await self._sdk_client({}).threads.get(thread_id)
            except Exception as exc:
                if http_status(exc) == 404:
                    return False
                raise
            if not isinstance(thread, Mapping) or thread.get("status") == "busy":
                return False
            updated = _parse_time(thread.get("updated_at"))
        else:
            assert self.threads is not None
            record = await self.threads.get(thread_id)
            if record is None:
                return False
            updated = _parse_time(record.updated_at or record.created_at)
        return updated is not None and updated < cutoff

    async def sweep_orphaned_runs(self, days: int | None = None, *, batch: int = 500) -> int:
        """langgraph-server: drop run records, older than `days`, of threads the server no longer has.

        The server's own `DELETE /threads/{id}` removes a thread's run records
        (`forget_thread_runs`); this catches the rest (for example threads
        deleted while this app could not reach its database). Paged by
        thread id, so later pages are reached however many live threads have
        old run records. Returns how many threads' records were removed.
        """
        days = retention_days() if days is None else days
        if days <= 0 or self.runtime != LANGGRAPH_SERVER or self.runs is None:
            return 0
        cutoff_iso = (datetime.now(tz=UTC) - timedelta(days=days)).isoformat()
        client = self._sdk_client({})
        removed = 0
        after: str | None = None
        for _ in range(RETENTION_MAX_BATCHES):
            thread_ids = await self.runs.thread_ids_before(cutoff_iso, after=after, limit=batch)
            for thread_id in thread_ids:
                if await self._server_thread_record(client, thread_id) is None:
                    await self.runs.delete_for_thread(thread_id)
                    if self.approvals is not None:
                        await self.approvals.delete_for_thread(thread_id)
                    removed += 1
            if len(thread_ids) < batch:
                break
            after = thread_ids[-1]
        return removed

    # -- streaming ---------------------------------------------------------

    async def stream(
        self,
        principal: Principal,
        req: ChatRequest,
        thread_id: str,
        lease: ThreadLease | None = None,
        resume: Resume | None = None,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Run the graph once on `thread_id` and yield the chat events.

        `lease` is the thread's run lock when the caller took it already
        (`/chat` does, to answer 409 before streaming); otherwise it is taken
        here and a busy thread yields a single `thread_busy` error event. The
        lock is released when the run ends, however it ends.

        The run is recorded (`running`) before it starts: a run that cannot be
        recorded does not start (an `unavailable` error event). A new message
        on a thread with a pending approval does not start either (an
        `approval_pending` error event). With `resume` (from `decide()`) the
        paused run continues with the decision instead of a new message, and
        `principal` is the requester it acts as. A run that pauses for approval
        ends with `message.end` status `awaiting_approval` and the approvals.
        """
        if lease is None:
            try:
                lease = await self.acquire_thread(thread_id, principal)
            except ThreadBusy:
                yield EVENT_ERROR, thread_busy_error()
                return
            except HTTPException as exc:  # not the owner any more, or the database is down
                code = CODE_FORBIDDEN if exc.status_code == 403 else CODE_UNAVAILABLE
                yield EVENT_ERROR, {"code": code, "message": str(exc.detail)}
                return
        if resume is None:
            try:
                await self.assert_no_pending_approval(thread_id)
            except (ApprovalPending, HTTPException) as exc:
                await lease.release()
                if isinstance(exc, ApprovalPending):
                    yield (
                        EVENT_ERROR,
                        {
                            "code": CODE_APPROVAL_PENDING,
                            "message": str(exc),
                            "approvals": exc.approvals,
                        },
                    )
                else:
                    yield EVENT_ERROR, {"code": CODE_UNAVAILABLE, "message": str(exc.detail)}
                return
            except BaseException:
                await lease.release()
                raise
        run_id = str(uuid.uuid4())
        bind_log_context(run_id=run_id, thread_id=thread_id, principal_hash=principal.hashed_id())
        record = RunRecord(
            run_id=run_id,
            thread_id=thread_id,
            principal_hash=principal.hashed_id(),
            model=model_label(),
            status=STATUS_OK,
            metadata=dict(req.metadata) or None,
        )
        try:
            if self.runs is not None:
                async with asyncio.timeout(FINISH_STEP_TIMEOUT_S):
                    await self.runs.start(record)
        except Exception as exc:
            # Not recorded, not run: every run that starts leaves a record.
            await lease.release()
            error_id = new_error_id()
            logger.log(
                logging.WARNING if is_database_unavailable(exc) else logging.ERROR,
                "run not started: its run record could not be written (error_id=%s): %s: %s",
                error_id,
                type(exc).__name__,
                _first_line(exc),
                exc_info=None if is_database_unavailable(exc) else exc,
            )
            yield (
                EVENT_ERROR,
                {
                    "code": CODE_UNAVAILABLE,
                    "message": f"The run could not start: the database is unavailable. "
                    f"Reference: {error_id}.",
                    "error_id": error_id,
                    "run_id": run_id,
                },
            )
            return
        loop = asyncio.get_running_loop()
        started = time.perf_counter()
        deadline = loop.time() + run_timeout_s()
        heartbeat = sse_heartbeat_s()
        state = _RunState()
        status = STATUS_OK
        error: BaseException | None = None
        error_event: dict[str, Any] | None = None
        final_text: str | None = None
        pump: _Pump | None = None
        paused: list[ApprovalRecord] = []
        metrics.ACTIVE_RUNS.inc()
        try:
            start: dict[str, Any] = {"thread_id": thread_id, "run_id": run_id}
            if resume is not None:
                start.update(approval_id=resume.approval.approval_id, decision=resume.decision)
            yield EVENT_START, start
            lease.check()
            if self.runtime == LANGGRAPH_SERVER:
                source = self._server_events(principal, req, thread_id, run_id, state, resume)
            else:
                source = self._local_events(principal, req, thread_id, run_id, resume)
            pump = _Pump(source)
            # A lease lost mid-run (this replica cannot confirm it still owns
            # the thread) stops the run now, not at its next write.
            lease.on_lost(lambda: pump.interrupt(LeaseLost(thread_id, lease.lost_reason or "lost")))
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise RunTimeout()
                kind, payload = await pump.next(min(heartbeat, remaining))
                if kind == _IDLE:
                    if loop.time() >= deadline:
                        raise RunTimeout()
                    yield EVENT_HEARTBEAT, {}
                    continue
                if kind == _DONE:
                    break
                if kind == _FAILED:
                    raise payload
                mode, data = payload
                for event in map_stream_item(mode, data, state):
                    yield event
            if state.interrupts:
                # The graph paused. A gated API call's interrupt waits for a
                # decision (recorded before the lock is released, so no new
                # message can slip in first); any other kind cannot be answered here.
                lease.check()
                if all(is_approval_interrupt(i["value"]) for i in state.interrupts):
                    paused = await self._record_approvals(
                        principal, thread_id, run_id, state.interrupts
                    )
                    status = STATUS_AWAITING_APPROVAL
                else:
                    status = STATUS_ERROR
                    error_event = {
                        "code": CODE_UNSUPPORTED_INTERRUPT,
                        "message": UNSUPPORTED_INTERRUPT_MESSAGE,
                        "run_id": run_id,
                    }
                    logger.warning("run paused for input this server cannot collect")
        except RunTimeout:
            status = STATUS_TIMEOUT
            error_id = new_error_id()
            error_event = {
                "code": CODE_TIMEOUT,
                "message": f"The run took longer than {run_timeout_s():g} s and was cancelled. "
                f"Reference: {error_id}.",
                "error_id": error_id,
                "run_id": run_id,
            }
            logger.warning("run timed out (error_id=%s)", error_id)
        except (asyncio.CancelledError, GeneratorExit):
            status = STATUS_CANCELLED
            logger.info("run cancelled: the client went away")
            raise
        except Exception as exc:
            error = exc
            if _lease_lost(exc) is not None:
                status = STATUS_INTERRUPTED
            elif _is_recursion_error(exc):
                if pump is not None:
                    await pump.close()  # the graph has stopped; make sure before writing
                final_text = await self._end_at_step_limit(req, thread_id, lease)
                status = STATUS_STEP_LIMIT if final_text is not None else STATUS_ERROR
            else:
                status = STATUS_ERROR
            if final_text is None:
                error_event = self._error_event(exc, run_id)
        finally:
            if pump is not None:
                pump.cancel()
            latency_ms = int((time.perf_counter() - started) * 1000)
            metrics.ACTIVE_RUNS.dec()
            metrics.observe_run(status, latency_ms / 1000, state.input_tokens, state.output_tokens)
            # The run's outbound-API call counts (limits.max_calls_per_run) go with it.
            end_api_run(run_id)
            finish = asyncio.ensure_future(
                self._finish_run(
                    principal, req, record, state, status, error, latency_ms, pump, lease
                )
            )
            # Shielded: the record and the lock release complete even when the
            # consumer is cancelled again while waiting.
            await asyncio.shield(finish)
        if error_event is not None:
            yield EVENT_ERROR, error_event
            return
        if final_text is not None:
            yield EVENT_DELTA, {"text": final_text}
        end: dict[str, Any] = {
            "thread_id": thread_id,
            "run_id": run_id,
            "usage": {
                "input_tokens": state.input_tokens,
                "output_tokens": state.output_tokens,
            },
            "latency_ms": latency_ms,
            "status": status,
        }
        if paused:
            # The requester sees what it is asked to approve (it asked for it).
            end["approval"] = paused[0].public()
            end["approvals"] = [record.public() for record in paused]
        yield EVENT_END, end

    def _error_event(self, exc: BaseException, run_id: str) -> dict[str, Any]:
        """The client-facing error: a code and a generic message; the detail is logged."""
        if isinstance(exc, ThreadBusy) or (
            http_status(exc) == 409 and self.runtime == LANGGRAPH_SERVER
        ):
            logger.info("run refused: the thread is busy")
            return {**thread_busy_error(), "run_id": run_id}
        error_id = new_error_id()
        lost = _lease_lost(exc)
        if _is_recursion_error(exc):
            logger.warning("run reached the recursion limit (error_id=%s)", error_id)
            event: dict[str, Any] = {
                "code": CODE_RECURSION,
                "message": f"The run reached the step limit ({recursion_limit()} steps) and "
                f"was stopped. Reference: {error_id}.",
            }
        elif lost is not None:
            logger.warning("run stopped (error_id=%s): %s", error_id, lost)
            event = {
                "code": CODE_UNAVAILABLE,
                "message": "The run was stopped: it could no longer confirm it was the only "
                f"run on this thread. Reference: {error_id}.",
            }
        elif is_database_unavailable(exc):
            logger.warning(
                "run failed: the database is unavailable (error_id=%s): %s: %s",
                error_id,
                type(exc).__name__,
                _first_line(exc),
            )
            event = {
                "code": CODE_UNAVAILABLE,
                "message": f"The run failed: the database is unavailable. Reference: {error_id}.",
            }
        else:
            logger.error("run failed (error_id=%s)", error_id, exc_info=exc)
            event = {
                "code": CODE_RUN_FAILED,
                "message": f"The run failed. Reference: {error_id}.",
            }
        event.update({"error_id": error_id, "run_id": run_id})
        if dev_mode():
            event["detail"] = f"{type(exc).__name__}: {exc}"
        return event

    async def _finish_run(
        self,
        principal: Principal,
        req: ChatRequest,
        record: RunRecord,
        state: _RunState,
        status: str,
        error: BaseException | None,
        latency_ms: int,
        pump: _Pump | None,
        lease: ThreadLease,
    ) -> None:
        """Bookkeeping after a run, each step bounded: the client's last event is not held up."""
        thread_id = record.thread_id
        try:
            if pump is not None and not await pump.close(PUMP_STOP_WAIT_S):
                # Still stopping (it waits for writes in flight, which the lease
                # fence refuses): the thread stays busy until it has stopped,
                # but the client need not wait for it.
                lease.hold_until(pump.task)
            if (
                status in (STATUS_TIMEOUT, STATUS_CANCELLED, STATUS_INTERRUPTED)
                and self.runtime == LANGGRAPH_SERVER
            ):
                await self._cancel_server_run(req, thread_id, state)
            jobs = [self._record_run(principal, req, record, state, status, error, latency_ms)]
            stopped = pump is None or pump.task.done()
            # A graph still stopping could write after the repair: the next run repairs then.
            if stopped and self._repairs_after(status, error, lease):
                reason = f"The tool call did not finish: the run stopped ({status})."
                jobs.append(self._close_dangling_tool_calls(req, thread_id, reason))
            await asyncio.gather(*jobs)
            if stopped and not lease.lost and status not in (STATUS_OK, STATUS_AWAITING_APPROVAL):
                await self._expire_orphaned_approvals(req, thread_id, record.run_id)
        finally:
            await lease.release()
        logger.info(
            "run finished",
            extra={"status": status, "latency_ms": latency_ms, "run_id": record.run_id},
        )

    async def _expire_orphaned_approvals(
        self, req: ChatRequest, thread_id: str, run_id: str
    ) -> None:
        """Expire the thread's pending approvals that nothing waits for (best effort).

        A run that ends without pausing (cancelled, failed, timed out) can
        leave pending approvals behind: ones it recorded before it was cut
        short (its client never saw them), or ones of the paused run it
        resumed whose calls the repair has since answered. Left pending, they
        would refuse the thread's next message (409 `approval_pending`) until
        they expire; expired, the next message goes through and the history
        repair tells the model the call was not approved.
        """
        if self.approvals is None or (self.db is not None and not self.db.health.up):
            return
        expired = 0
        try:
            async with asyncio.timeout(FINISH_STEP_TIMEOUT_S):
                pending = await self.approvals.pending_for_thread(thread_id)
                if not pending:
                    return
                waiting = await self._paused_interrupts(thread_id, req.forward_headers)
                for item in pending:
                    if item.run_id == run_id or item.interrupt_id not in waiting:
                        if await self.approvals.expire(item.approval_id) is not None:
                            expired += 1
        except Exception as exc:
            logger.warning(
                "could not expire the approvals of a stopped run (%s); they expire on time",
                type(exc).__name__,
            )
        if expired:
            metrics.observe_approvals("expired", expired)
            logger.info("expired %d approval(s) left by a stopped run", expired)

    def _repairs_after(self, status: str, error: BaseException | None, lease: ThreadLease) -> bool:
        """Whether a stopped run should answer the tool calls it left open.

        Not after a clean end, not after a pause for approval (the open call
        waits for its decision), not when the run never started (the thread
        was busy: the open calls belong to the run in progress) and not when
        this process no longer owns the thread.
        """
        if (
            status in (STATUS_OK, STATUS_AWAITING_APPROVAL, STATUS_STEP_LIMIT, STATUS_INTERRUPTED)
            or lease.lost
        ):
            return False
        if error is None:
            return True
        return not (isinstance(error, ThreadBusy) or http_status(error) == 409)

    async def _close_dangling_tool_calls(
        self, req: ChatRequest, thread_id: str, reason: str
    ) -> None:
        """Answer the tool calls a stopped run left open, so the thread stays usable (best effort).

        A run cut short between the model's tool call and the tool's result
        leaves an assistant message whose tool calls have no results; model
        providers reject such a history on the next turn. Each open call gets
        an error result right after it. The next run repairs anything this
        misses (for example when the database is down right now).
        """
        if self.db is not None and not self.db.health.up:
            logger.info("the database is down: the next run answers the open tool calls")
            return
        try:
            async with asyncio.timeout(FINISH_STEP_TIMEOUT_S):
                closed = await self._repair_history(req, thread_id, reason)
        except Exception as exc:
            logger.warning(
                "could not close the tool calls of a stopped run (%s); the next run will",
                type(exc).__name__,
            )
            return
        if closed:
            logger.info("closed %d tool calls left open by a stopped run", closed)

    async def _repair_history(
        self,
        req: ChatRequest,
        thread_id: str,
        reason: str,
        final_text: str | None = None,
        texts: Mapping[str, str] | None = None,
    ) -> int:
        """Put the thread's tool-call history right, then add `final_text` as the last reply.

        Every tool call gets its result right after it (an error result saying
        `reason` when it has none, `texts[call id]` for a call named there,
        or that the arguments were not valid JSON for a call whose arguments
        did not parse) and results that answer no call go; see
        `repair_tool_history`. Only the owner of the thread's run lease writes
        (fastapi: the checkpointer's fence). Returns how many open calls got a
        result. Under langgraph-server a thread with a run in progress (started
        through the server's own API) is left alone.
        """
        if self.runtime == LANGGRAPH_SERVER:
            return await self._server_repair_history(req, thread_id, reason, final_text, texts)
        from langchain_core.messages import AIMessage, RemoveMessage, ToolMessage
        from langgraph.constants import END

        from {{cookiecutter.agent_directory}}.agent import graph

        def error_result(call: Any) -> Any:
            return ToolMessage(
                content=open_call_result_text(call, (texts or {}).get(_call_id(call), reason)),
                tool_call_id=str(_get(call, "id") or ""),
                name=str(_get(call, "name") or ""),
                status="error",
                id=str(uuid.uuid4()),
            )

        config = {"configurable": {"thread_id": thread_id}}
        snapshot = await graph.aget_state(config)
        if snapshot.tasks:
            # A step that never finished (the process stopped mid-step): the
            # state shown includes what its tasks already wrote, but an update
            # is applied to the last checkpoint without them. Make them part
            # of the thread first, as the next run's input would.
            await graph.aupdate_state(config, None, as_node=END)
            snapshot = await graph.aget_state(config)
        messages = list((snapshot.values or {}).get("messages") or [])
        repair = repair_tool_history(messages, error_result)
        update: list[Any] = []
        if repair is not None:
            update = (
                list(repair.added)
                if repair.append_only
                else [RemoveMessage(id=REMOVE_ALL_MESSAGES), *repair.messages]
            )
        if final_text is not None:
            update.append(AIMessage(content=final_text, id=str(uuid.uuid4())))
        if not update:
            return 0
        if final_text is not None and "model" in graph.nodes:
            as_node: str | None = "model"
        else:
            as_node = "tools" if "tools" in graph.nodes else None
        await graph.aupdate_state(config, {"messages": update}, as_node=as_node)
        if repair is not None and not repair.append_only:
            logger.warning("moved misplaced tool results back after their calls")
        return len(repair.added) if repair is not None else 0

    async def _server_repair_history(
        self,
        req: ChatRequest,
        thread_id: str,
        reason: str,
        final_text: str | None,
        texts: Mapping[str, str] | None = None,
    ) -> int:
        client = self._sdk_client(req.forward_headers)
        thread = await client.threads.get(thread_id)
        if final_text is not None and _server_busy(thread):
            # Ending our own run at the step limit: the server sends the run's
            # error before it marks the run done and the thread idle.
            thread = await self._server_wait_idle(client, thread_id, SERVER_IDLE_WAIT_S)
        if _server_busy(thread):
            # A run owns the thread (one started through the server's own API,
            # or ours still winding down): its open tool calls are its own.
            if final_text is not None:
                raise ThreadBusy(thread_id)
            return 0
        snapshot = await client.threads.get_state(thread_id)
        if isinstance(snapshot, Mapping) and snapshot.get("tasks"):
            # Writes of a step that never finished: part of the thread first
            # (see `_repair_history`).
            await client.threads.update_state(thread_id, None, as_node="__end__")
            snapshot = await client.threads.get_state(thread_id)
        values = snapshot.get("values") if isinstance(snapshot, Mapping) else None
        messages = list((values or {}).get("messages") or [])

        def error_result(call: Any) -> dict[str, Any]:
            return {
                "type": "tool",
                "content": open_call_result_text(call, (texts or {}).get(_call_id(call), reason)),
                "tool_call_id": str(_get(call, "id") or ""),
                "name": str(_get(call, "name") or ""),
                "status": "error",
                "id": str(uuid.uuid4()),
            }

        repair = repair_tool_history(messages, error_result)
        update: list[Any] = []
        if repair is not None:
            update = (
                list(repair.added)
                if repair.append_only
                else [
                    # The server turns each dict back into a message and needs a
                    # `content` on every one, a RemoveMessage's included.
                    {"type": "remove", "id": REMOVE_ALL_MESSAGES, "content": ""},
                    *(_server_message(m) for m in repair.messages),
                ]
            )
        if final_text is not None:
            update.append({"type": "ai", "content": final_text, "id": str(uuid.uuid4())})
        if not update:
            return 0
        as_node = "model" if final_text is not None else "tools"
        await client.threads.update_state(thread_id, {"messages": update}, as_node=as_node)
        return len(repair.added) if repair is not None else 0

    async def _server_wait_idle(self, client: Any, thread_id: str, timeout_s: float) -> Any:
        """The server thread once it is no longer busy, or as it is after `timeout_s`."""
        deadline = time.monotonic() + timeout_s
        thread = await client.threads.get(thread_id)
        while _server_busy(thread) and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            thread = await client.threads.get(thread_id)
        return thread

    async def _end_at_step_limit(
        self, req: ChatRequest, thread_id: str, lease: ThreadLease
    ) -> str | None:
        """End a run that reached `RECURSION_LIMIT` with a final reply saying so.

        The thread keeps everything the run did (its open tool calls get an
        error result), then the reply; the client gets the reply and
        `message.end` with status `step_limit`. None when that cannot be
        written: the run then ends with the `recursion_limit` error.
        """
        text = STEP_LIMIT_MESSAGE.format(limit=recursion_limit())
        try:
            lease.check()
            async with asyncio.timeout(FINISH_STEP_TIMEOUT_S):
                await self._repair_history(req, thread_id, OPEN_CALL_STEP_LIMIT, final_text=text)
        except Exception as exc:
            logger.warning("could not end the run with a step-limit reply (%s)", type(exc).__name__)
            return None
        logger.warning(
            "run reached the step limit (%d steps); ended it with a reply", recursion_limit()
        )
        return text

    async def _local_events(
        self,
        principal: Principal,
        req: ChatRequest,
        thread_id: str,
        run_id: str,
        resume: Resume | None = None,
    ) -> AsyncIterator[tuple[str, Any]]:
        from langgraph.types import Command

        from {{cookiecutter.agent_directory}}.agent import AgentContext, graph

        if resume is None:
            # A run cut short earlier (a crash, an outage) may have left tool calls
            # without results: answer them before this run appends its turn.
            # (A resume continues the paused step: its open call is the one decided.)
            await self._repair_before_run(req, thread_id)
        config = {
            "configurable": {"thread_id": thread_id},
            "run_id": uuid.UUID(run_id),
            "recursion_limit": recursion_limit(),
            "metadata": trace_metadata(thread_id, run_id, principal, req.metadata),
        }
        # In-process only (never persisted): tools may need the caller's
        # credentials for `auth: forward` APIs.
        context = AgentContext(
            principal_id=principal.id,
            roles=list(principal.roles),
            attributes=dict(principal.attributes),
        )
        graph_input: Any = (
            Command(resume=resume.values)
            if resume is not None
            else {"messages": [{"role": "user", "content": req.message}]}
        )
        async for mode, data in graph.astream(
            graph_input,
            config=config,
            context=context,
            stream_mode=["messages", "updates"],
        ):
            yield mode, data

    async def _repair_before_run(self, req: ChatRequest, thread_id: str) -> None:
        """Repair the thread's history before a run appends to it.

        Under fastapi a failure fails the run (its database is this app's).
        Under langgraph-server the repair is best effort: the server may be
        reached through a client that cannot read the state.
        """
        try:
            # A call a paused run left waiting for an approval that expired (or
            # was decided but never resumed) says so in its result.
            texts = await self._approval_results(thread_id)
            closed = await self._repair_history(req, thread_id, OPEN_CALL_INTERRUPTED, texts=texts)
        except Exception as exc:
            if self.runtime == FASTAPI:
                raise
            logger.warning(
                "could not check the thread's tool calls before the run: %s", type(exc).__name__
            )
            return
        if closed:
            logger.warning("answered %d tool calls an interrupted run left open", closed)

    async def _record_run(
        self,
        principal: Principal,
        req: ChatRequest,
        record: RunRecord,
        state: _RunState,
        status: str,
        error: BaseException | None,
        latency_ms: int,
    ) -> None:
        if self.runs is None:
            return
        payload: dict[str, Any] | None = None
        if capture_full():
            payload = {
                "message": req.message,
                "response": "".join(state.text),
                "tool_calls": state.tool_calls,
                "error": str(error) if error else None,
            }
        final = RunRecord(
            run_id=record.run_id,
            thread_id=record.thread_id,
            principal_hash=principal.hashed_id(),
            model=record.model,
            status=status,
            input_tokens=state.input_tokens,
            output_tokens=state.output_tokens,
            latency_ms=latency_ms,
            error_type=type(error).__name__ if error else None,
            metadata=dict(req.metadata) or None,
            payload=payload,
            created_at=record.created_at,
        )
        if self.db is not None and not self.db.health.up:
            # Known down: do not hold the reply up; the maintenance loop writes it.
            self._unrecorded.append(final)
            logger.warning("the run record is written once the database is back")
            return
        try:
            async with asyncio.timeout(FINISH_STEP_TIMEOUT_S):
                await self.runs.record(final)
        except Exception as exc:  # a failed run record must not break the reply
            self._unrecorded.append(final)
            logger.warning(
                "could not write the run record (%s); retrying in the background",
                type(exc).__name__,
            )

    # -- reading a thread ----------------------------------------------------

    async def messages(
        self,
        principal: Principal,
        thread_id: str,
        forward_headers: Mapping[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        thread_id = validate_thread_id(thread_id, self.runtime)
        self._require_storage()
        if self.runtime == LANGGRAPH_SERVER:
            return await self._server_messages(principal, thread_id, forward_headers or {})
        assert self.threads is not None
        with database_errors():
            record = await self.threads.get(thread_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Unknown thread.")
        assert_access(principal, record)
        # Tool arguments reach a non-owner only under TRACE_CAPTURE=full.
        include_args = is_owner(principal, record) or capture_full()
        from {{cookiecutter.agent_directory}}.agent import graph

        with database_errors():
            snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        values = snapshot.values if snapshot is not None else {}
        return [
            serialize_message(m, include_tool_args=include_args) for m in values.get("messages", [])
        ]

    # -- langgraph-server: proxy through the loopback SDK client --------------

    def _sdk_client(self, req_headers: Mapping[str, str]) -> Any:
        from langgraph_sdk import get_client

        headers = select_forward_headers(req_headers)
        return get_client(
            url=os.environ.get("LANGGRAPH_SERVER_URL") or None, headers=headers or None
        )

    @staticmethod
    def _record_of(thread: Any, thread_id: str) -> ThreadRecord:
        meta = (thread.get("metadata") or {}) if isinstance(thread, Mapping) else {}
        return ThreadRecord(
            thread_id=str(thread.get("thread_id") or thread_id)
            if isinstance(thread, Mapping)
            else thread_id,
            principal_id=str(meta.get("principal_id") or ""),
            tenant=meta.get("tenant"),
        )

    async def _server_thread_record(self, client: Any, thread_id: str) -> ThreadRecord | None:
        """The thread's ownership metadata as the app wrote it at creation, or None when absent.

        The loopback client is unauthenticated on the server, so any error
        other than 404 is a transport/server failure (503), never an ownership
        signal. A thread without `principal_id` metadata (created through the
        native API without this app) fails closed: nobody but a read-across
        role reads it.
        """
        try:
            thread = await client.threads.get(thread_id)
        except Exception as exc:
            if http_status(exc) == 404:
                return None
            raise unavailable("LangGraph Server", exc) from exc
        return self._record_of(thread, thread_id)

    async def _server_resolve_thread(self, principal: Principal, req: ChatRequest) -> str:
        client = self._sdk_client(req.forward_headers)
        metadata = {
            "principal_id": principal.id,
            "tenant": principal.public_attributes().get("tenant"),
        }
        if req.thread_id:
            record = await self._server_thread_record(client, req.thread_id)
            if record is not None:
                # chat.send is a write: the owner only (403 before message.start).
                assert_owner(principal, record)
                return record.thread_id
        try:
            # `do_nothing` returns the existing thread when another request
            # created it in between: its metadata then decides, not ours.
            thread = await client.threads.create(
                thread_id=req.thread_id, metadata=metadata, if_exists="do_nothing"
            )
        except Exception as exc:  # loopback not configured, server down, ...
            raise unavailable("LangGraph Server", exc) from exc
        record = self._record_of(thread, req.thread_id or "")
        assert_owner(principal, record)
        return record.thread_id

    async def _server_events(
        self,
        principal: Principal,
        req: ChatRequest,
        thread_id: str,
        run_id: str,
        state: _RunState,
        resume: Resume | None = None,
    ) -> AsyncIterator[tuple[str, Any]]:
        if resume is None:
            # As under fastapi: answer tool calls an interrupted run left open first.
            await self._repair_before_run(req, thread_id)
        client = self._sdk_client(req.forward_headers)
        # A resume continues the paused run through the server's native resume
        # (the server's auth handler refuses a `command` from outside the app).
        run_input: dict[str, Any] = (
            {"command": {"resume": resume.values}}
            if resume is not None
            else {"input": {"messages": [{"role": "user", "content": req.message}]}}
        )
        metadata = {
            **trace_metadata(thread_id, run_id, principal, req.metadata),
            # The server merges the thread's metadata, whose `principal_id` is
            # the raw owner id (the ownership check needs it there), into the
            # run's metadata, and from there into the traced config metadata
            # and each checkpoint's metadata. This key overrides it there with
            # the hashed id; the thread itself keeps the raw one.
            "principal_id": principal.hashed_id(),
        }
        async for part in client.runs.stream(
            thread_id,
            GRAPH_ID,
            **run_input,
            stream_mode=["messages-tuple", "updates"],
            metadata=metadata,
            config={"recursion_limit": recursion_limit()},
            # The server persists run context: never the principal's credentials.
            # The raw id stays here (tools act on the caller's behalf); run
            # context is not traced, and the metadata key above keeps it out of
            # checkpoint metadata too.
            context={
                "principal_id": principal.id,
                "roles": list(principal.roles),
                "attributes": principal.public_attributes(),
            },
            multitask_strategy="reject",
            on_disconnect="cancel",
        ):
            event = str(getattr(part, "event", ""))
            data = getattr(part, "data", None)
            if event == "metadata" and isinstance(data, Mapping):
                state.server_run_id = str(data.get("run_id") or "") or None
            elif event.startswith("messages"):
                yield "messages", data
            elif event.startswith("updates"):
                yield "updates", data
            elif event == "error":
                raise _ServerRunError(data)

    async def _cancel_server_run(self, req: ChatRequest, thread_id: str, state: _RunState) -> None:
        """Stop the server-side run behind a timed-out or abandoned stream (best effort)."""
        if not state.server_run_id:
            return
        try:
            client = self._sdk_client(req.forward_headers)
            async with asyncio.timeout(10):
                await client.runs.cancel(thread_id, state.server_run_id, wait=True)
        except Exception as exc:
            if http_status(exc) not in (404, 409):
                logger.warning("could not cancel the server run: %s", type(exc).__name__)

    async def _server_messages(
        self, principal: Principal, thread_id: str, forward_headers: Mapping[str, str]
    ) -> list[dict[str, Any]]:
        client = self._sdk_client(forward_headers)
        record = await self._server_thread_record(client, thread_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Unknown thread.")
        assert_access(principal, record)
        # Tool arguments reach a non-owner only under TRACE_CAPTURE=full.
        include_args = is_owner(principal, record) or capture_full()
        try:
            state = await client.threads.get_state(thread_id)
        except Exception as exc:
            if http_status(exc) == 404:
                raise HTTPException(status_code=404, detail="Unknown thread.") from exc
            raise unavailable("LangGraph Server", exc) from exc
        values = state.get("values") or {}
        return [
            serialize_message(m, include_tool_args=include_args) for m in values.get("messages", [])
        ]

    async def _server_list_threads(
        self,
        principal: Principal,
        limit: int,
        offset: int,
        forward_headers: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        client = self._sdk_client(forward_headers)
        filters: dict[str, Any] = {}
        if not reads_across(principal):
            filters["metadata"] = {"principal_id": principal.id}
        try:
            threads = await client.threads.search(
                limit=limit, offset=offset, sort_by="updated_at", sort_order="desc", **filters
            )
        except Exception as exc:
            raise unavailable("LangGraph Server", exc) from exc
        out = []
        for thread in threads:
            if not isinstance(thread, Mapping):
                continue
            out.append(
                {
                    "thread_id": str(thread.get("thread_id")),
                    "created_at": _iso(thread.get("created_at")),
                    "updated_at": _iso(thread.get("updated_at")),
                }
            )
        return out

    async def _server_idle_threads(self, cutoff: datetime, batch: int) -> list[str]:
        client = self._sdk_client({})
        threads = await client.threads.search(
            limit=batch, offset=0, sort_by="updated_at", sort_order="asc"
        )
        idle: list[str] = []
        for thread in threads:
            updated = _parse_time(thread.get("updated_at")) if isinstance(thread, Mapping) else None
            if updated is None or updated >= cutoff:
                break
            if thread.get("status") == "busy":
                continue
            idle.append(str(thread.get("thread_id")))
        return idle


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        with contextlib.suppress(ValueError):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


RUNTIME = ChatRuntime()
