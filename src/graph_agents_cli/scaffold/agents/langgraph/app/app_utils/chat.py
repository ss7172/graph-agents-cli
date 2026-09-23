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
  thread that has no owner (fastapi).
* Guardrails: `RUN_TIMEOUT_S` cancels a run (status `timeout`),
  `RECURSION_LIMIT` caps graph steps, a client that disconnects cancels its
  run (status `cancelled`), and `SSE_HEARTBEAT_S` keeps idle streams alive.
* Errors reach clients as a generic message with an `error_id`; the detail
  goes to the log under that id (and to the event under `APP_ENV=dev` only).
* Client metadata is kept in the run record and, under `TRACE_CAPTURE=full`
  only, in traces under `client_metadata` (never over the server-set
  `thread_id`, `run_id`, `principal_hash`); it is not written into checkpoints.
* Run records are durable in Postgres when the runtime has one (see `db.py`),
  and `RETENTION_DAYS` purges threads idle longer than that. Under
  langgraph-server the server's own `DELETE /threads/{id}` removes the
  thread's run records too (`forget_thread_runs`, called by
  `middleware.ThreadDeleteHookMiddleware`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException

from {{cookiecutter.agent_directory}}.app_utils import metrics
from {{cookiecutter.agent_directory}}.app_utils.auth import Principal
from {{cookiecutter.agent_directory}}.app_utils.checkpointer import checkpointer_kind, get_checkpointer
from {{cookiecutter.agent_directory}}.app_utils.content import content_to_text
from {{cookiecutter.agent_directory}}.app_utils.db import (
    Database,
    RunRecord,
    RunStore,
    capture_full,
    is_postgres_url,
)
from {{cookiecutter.agent_directory}}.app_utils.limits import (
    recursion_limit,
    retention_days,
    run_timeout_s,
    sse_heartbeat_s,
    valid_thread_id,
)
from {{cookiecutter.agent_directory}}.app_utils.model import model_label
from {{cookiecutter.agent_directory}}.app_utils.telemetry import bind_log_context
from {{cookiecutter.agent_directory}}.app_utils.threads import (
    THREAD_BUSY,
    ThreadBusy,
    ThreadLease,
    ThreadLocks,
    ThreadRecord,
    ThreadStore,
    assert_access,
    assert_owner,
    is_owner,
    reads_across,
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

# Run statuses in run records and metrics.
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_TIMEOUT = "timeout"
STATUS_CANCELLED = "cancelled"

# Error codes clients see in `error` events.
CODE_RUN_FAILED = "run_failed"
CODE_TIMEOUT = "timeout"
CODE_RECURSION = "recursion_limit"
CODE_UNAVAILABLE = "unavailable"
CODE_FORBIDDEN = "forbidden"

# Request headers passed on to the LangGraph Server under langgraph-server.
# They matter when LANGGRAPH_SERVER_URL points at a real HTTP endpoint (its
# auth handler runs there); the default in-process loopback ignores them.
DEFAULT_FORWARD_HEADERS = ("authorization", "cookie")

RETENTION_INTERVAL_S = 3600.0
RETENTION_FIRST_DELAY_S = 60.0
RETENTION_BATCH = 500
RETENTION_MAX_BATCHES = 20


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
    return (os.environ.get("APP_ENV") or "").strip().lower() == "dev"


def sse_encode(event: str, data: Mapping[str, Any]) -> str:
    if event == EVENT_HEARTBEAT:
        return ": keep-alive\n\n"
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def new_error_id() -> str:
    return uuid.uuid4().hex[:16]


def unavailable(what: str, exc: BaseException) -> HTTPException:
    """A 503 whose detail names an error id, never the exception text (logged instead)."""
    error_id = new_error_id()
    logger.error(
        "%s unavailable (error_id=%s): %s", what, error_id, type(exc).__name__, exc_info=exc
    )
    return HTTPException(status_code=503, detail=f"{what} unavailable. Reference: {error_id}.")


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


def map_stream_item(mode: str, data: Any, state: _RunState) -> Iterator[tuple[str, dict[str, Any]]]:
    """Map one LangGraph stream item (`messages` or `updates` mode) to chat events."""
    if mode == "messages":
        chunk = data[0] if isinstance(data, list | tuple) and data else data
        if _is_ai(chunk) and not (_get(chunk, "tool_call_chunks") or _get(chunk, "tool_calls")):
            text = content_to_text(_get(chunk, "content", ""))
            if text:
                state.text.append(text)
                yield EVENT_DELTA, {"text": text}
        return
    if mode != "updates" or not isinstance(data, Mapping):
        return
    for update in data.values():
        for m in _iter_messages(update):
            if _is_ai(m):
                msg_id = str(_get(m, "id") or "")
                if msg_id and msg_id in state.seen_ai_ids:
                    continue
                if msg_id:
                    state.seen_ai_ids.add(msg_id)
                _accumulate_usage(state, m)
                for call in _get(m, "tool_calls") or []:
                    entry = {
                        "id": str(_get(call, "id") or uuid.uuid4()),
                        "name": str(_get(call, "name") or ""),
                        "args": dict(_get(call, "args") or {}),
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
        for call in _get(m, "tool_calls") or []:
            entry: dict[str, Any] = {"id": _get(call, "id"), "name": _get(call, "name")}
            if include_tool_args:
                entry["args"] = dict(_get(call, "args") or {})
            calls.append(entry)
        if calls:
            out["tool_calls"] = calls
    if _is_tool(m):
        out["tool_call_id"] = _get(m, "tool_call_id")
        out["name"] = _get(m, "name")
        out["is_error"] = str(_get(m, "status") or "success") == "error"
    return out


def dangling_tool_calls(messages: list[Any]) -> list[Any]:
    """Tool calls of the last assistant message that have no tool result yet."""
    answered = {str(_get(m, "tool_call_id")) for m in messages if _is_tool(m)}
    for m in reversed(messages):
        if _is_ai(m):
            return [c for c in _get(m, "tool_calls") or [] if str(_get(c, "id")) not in answered]
    return []


def thread_busy_error() -> dict[str, Any]:
    return {"code": THREAD_BUSY, "message": "This thread already has a run in progress."}


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

    async def next(self, timeout: float) -> tuple[str, Any]:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except TimeoutError:
            return _IDLE, None

    def cancel(self) -> None:
        if not self._task.done():
            self._task.cancel()

    async def close(self) -> None:
        self.cancel()
        if not self._task.done():
            await asyncio.wait({self._task}, timeout=10)


class ChatRuntime:
    """Process-wide chat runtime, started and stopped by the app lifespan."""

    def __init__(self) -> None:
        self.runtime = detect_runtime()
        self.db: Database | None = None
        self.runs: RunStore | None = None
        self.threads: ThreadStore | None = None
        self.locks: ThreadLocks = ThreadLocks()
        self._exit: AsyncExitStack | None = None
        self._retention_task: asyncio.Task[None] | None = None
        self.started = False

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self.started:
            return
        self.runtime = detect_runtime()
        self._exit = AsyncExitStack()
        try:
            if self.runtime == FASTAPI:
                from {{cookiecutter.agent_directory}}.agent import graph

                self.db = Database.from_env()
                await self.db.open()
                self._exit.push_async_callback(self.db.close)
                saver = await self._exit.enter_async_context(get_checkpointer(self.db.pool))
                graph.checkpointer = saver
            else:
                # The server binds persistence; run records go to its Postgres
                # (DATABASE_URI) when it has one.
                self.db = Database.for_server()
                await self.db.open()
                self._exit.push_async_callback(self.db.close)
            self.locks = ThreadLocks(self.db.dsn if self.db.is_postgres else None)
            self._exit.push_async_callback(self.locks.close)
            self.runs = RunStore(self.db)
            self.threads = ThreadStore(self.db)
            days = retention_days()
            if days > 0:
                self._retention_task = asyncio.create_task(self._retention_loop(days))
        except BaseException:
            await self._exit.aclose()
            self._exit = None
            raise
        self.started = True
        logger.info(
            "chat runtime started: runtime=%s checkpointer=%s retention_days=%s",
            self.runtime,
            self.checkpointer_kind(),
            retention_days(),
        )

    async def stop(self) -> None:
        task, self._retention_task = self._retention_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._exit is not None:
            await self._exit.aclose()
            self._exit = None
        self.started = False

    def checkpointer_kind(self) -> str:
        if self.runtime == LANGGRAPH_SERVER:
            return "postgres" if is_postgres_url(os.environ.get("DATABASE_URI")) else "memory"
        return checkpointer_kind()

    async def ready(self, timeout: float = 2.0) -> bool:
        """True when the runtime's storage answers a trivial query within `timeout` seconds."""
        if not self.started or self.db is None:
            return False
        try:
            async with asyncio.timeout(timeout):
                await self.db.ping()
                if self.runtime == LANGGRAPH_SERVER:
                    await self._sdk_client({}).assistants.search(limit=1)
        except Exception as exc:
            logger.warning("readiness check failed: %s", type(exc).__name__)
            return False
        return True

    # -- threads -----------------------------------------------------------

    async def resolve_thread(self, principal: Principal, req: ChatRequest) -> str:
        """The thread id for this request, after the ownership check (403 before streaming)."""
        if req.thread_id is not None:
            req.thread_id = validate_thread_id(req.thread_id, self.runtime)
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
        if self.runtime == LANGGRAPH_SERVER:
            return await self._server_list_threads(principal, limit, offset, forward_headers or {})
        assert self.threads is not None
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
        headers = forward_headers or {}
        self._assert_deletable(principal, await self._thread_record(thread_id, headers))
        lease = await self.acquire_thread(thread_id)
        try:
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
        else:
            from {{cookiecutter.agent_directory}}.agent import graph

            if graph.checkpointer is not None:
                await graph.checkpointer.adelete_thread(thread_id)
        if self.runs is not None:
            await self.runs.delete_for_thread(thread_id)
        if self.threads is not None and self.runtime == FASTAPI:
            await self.threads.delete(thread_id)

    async def forget_thread_runs(self, thread_id: str) -> None:
        """Drop a deleted thread's run records (after the server's own DELETE succeeded)."""
        if self.runs is None:
            return
        try:
            thread_id = str(uuid.UUID(thread_id))  # run records use the canonical form
        except ValueError:
            return
        await self.runs.delete_for_thread(thread_id)
        logger.info("run records of a deleted thread removed", extra={"thread_id": thread_id})

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
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Run the graph once on `thread_id` and yield the chat events.

        `lease` is the thread's run lock when the caller took it already
        (`/chat` does, to answer 409 before streaming); otherwise it is taken
        here and a busy thread yields a single `thread_busy` error event. The
        lock is released when the run ends, however it ends.
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
        run_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        started = time.perf_counter()
        deadline = loop.time() + run_timeout_s()
        heartbeat = sse_heartbeat_s()
        state = _RunState()
        status = STATUS_OK
        error: BaseException | None = None
        error_event: dict[str, Any] | None = None
        pump: _Pump | None = None
        bind_log_context(run_id=run_id, thread_id=thread_id, principal_hash=principal.hashed_id())
        metrics.ACTIVE_RUNS.inc()
        try:
            yield EVENT_START, {"thread_id": thread_id, "run_id": run_id}
            if self.runtime == LANGGRAPH_SERVER:
                source = self._server_events(principal, req, thread_id, run_id, state)
            else:
                source = self._local_events(principal, req, thread_id, run_id)
            pump = _Pump(source)
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
            status = STATUS_ERROR
            error = exc
            error_event = self._error_event(exc, run_id)
        finally:
            if pump is not None:
                pump.cancel()
            latency_ms = int((time.perf_counter() - started) * 1000)
            metrics.ACTIVE_RUNS.dec()
            metrics.observe_run(status, latency_ms / 1000, state.input_tokens, state.output_tokens)
            finish = asyncio.ensure_future(
                self._finish_run(
                    principal, req, thread_id, run_id, state, status, error, latency_ms, pump, lease
                )
            )
            # Shielded: the record and the lock release complete even when the
            # consumer is cancelled again while waiting.
            await asyncio.shield(finish)
        if error_event is not None:
            yield EVENT_ERROR, error_event
            return
        yield (
            EVENT_END,
            {
                "thread_id": thread_id,
                "run_id": run_id,
                "usage": {
                    "input_tokens": state.input_tokens,
                    "output_tokens": state.output_tokens,
                },
                "latency_ms": latency_ms,
                "status": STATUS_OK,
            },
        )

    def _error_event(self, exc: BaseException, run_id: str) -> dict[str, Any]:
        """The client-facing error: a code and a generic message; the detail is logged."""
        if isinstance(exc, ThreadBusy) or (
            http_status(exc) == 409 and self.runtime == LANGGRAPH_SERVER
        ):
            logger.info("run refused: the thread is busy")
            return {**thread_busy_error(), "run_id": run_id}
        error_id = new_error_id()
        if _is_recursion_error(exc):
            logger.warning("run reached the recursion limit (error_id=%s)", error_id)
            event: dict[str, Any] = {
                "code": CODE_RECURSION,
                "message": f"The run reached the step limit ({recursion_limit()} steps) and "
                f"was stopped. Reference: {error_id}.",
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
        thread_id: str,
        run_id: str,
        state: _RunState,
        status: str,
        error: BaseException | None,
        latency_ms: int,
        pump: _Pump | None,
        lease: ThreadLease,
    ) -> None:
        try:
            if pump is not None:
                await pump.close()
            if status in (STATUS_TIMEOUT, STATUS_CANCELLED) and self.runtime == LANGGRAPH_SERVER:
                await self._cancel_server_run(req, thread_id, state)
            if status != STATUS_OK:
                await self._close_dangling_tool_calls(req, thread_id, status)
            await self._record_run(
                principal, req, thread_id, run_id, state, status, error, latency_ms
            )
        finally:
            await lease.release()
        logger.info(
            "run finished",
            extra={"status": status, "latency_ms": latency_ms, "run_id": run_id},
        )

    async def _close_dangling_tool_calls(
        self, req: ChatRequest, thread_id: str, status: str
    ) -> None:
        """Answer the tool calls a stopped run left open, so the thread stays usable.

        A run cut short between the model's tool call and the tool's result
        leaves an assistant message whose tool calls have no results; model
        providers reject such a history on the next turn. Each open call gets
        an error result saying the run stopped (best effort, under the run lock).
        """
        content = f"The tool call did not finish: the run stopped ({status})."
        try:
            if self.runtime == LANGGRAPH_SERVER:
                client = self._sdk_client(req.forward_headers)
                snapshot = await client.threads.get_state(thread_id)
                values = snapshot.get("values") if isinstance(snapshot, Mapping) else None
                open_calls = dangling_tool_calls((values or {}).get("messages") or [])
                if open_calls:
                    patches = [
                        {
                            "type": "tool",
                            "content": content,
                            "tool_call_id": str(_get(c, "id")),
                            "name": str(_get(c, "name") or ""),
                            "status": "error",
                        }
                        for c in open_calls
                    ]
                    await client.threads.update_state(
                        thread_id, {"messages": patches}, as_node="tools"
                    )
            else:
                from langchain_core.messages import ToolMessage

                from {{cookiecutter.agent_directory}}.agent import graph

                config = {"configurable": {"thread_id": thread_id}}
                snapshot = await graph.aget_state(config)
                open_calls = dangling_tool_calls((snapshot.values or {}).get("messages") or [])
                if open_calls:
                    patches = [
                        ToolMessage(
                            content=content,
                            tool_call_id=str(_get(c, "id")),
                            name=str(_get(c, "name") or ""),
                            status="error",
                        )
                        for c in open_calls
                    ]
                    as_node = "tools" if "tools" in graph.nodes else None
                    await graph.aupdate_state(config, {"messages": patches}, as_node=as_node)
        except Exception:
            logger.warning("could not close the tool calls of a stopped run", exc_info=True)
            return
        if open_calls:
            logger.info("closed %d tool calls left open by a stopped run", len(open_calls))

    async def _local_events(
        self, principal: Principal, req: ChatRequest, thread_id: str, run_id: str
    ) -> AsyncIterator[tuple[str, Any]]:
        from {{cookiecutter.agent_directory}}.agent import AgentContext, graph

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
        async for mode, data in graph.astream(
            {"messages": [{"role": "user", "content": req.message}]},
            config=config,
            context=context,
            stream_mode=["messages", "updates"],
        ):
            yield mode, data

    async def _record_run(
        self,
        principal: Principal,
        req: ChatRequest,
        thread_id: str,
        run_id: str,
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
        record = RunRecord(
            run_id=run_id,
            thread_id=thread_id,
            principal_hash=principal.hashed_id(),
            model=model_label(),
            status=status,
            input_tokens=state.input_tokens,
            output_tokens=state.output_tokens,
            latency_ms=latency_ms,
            error_type=type(error).__name__ if error else None,
            metadata=dict(req.metadata) or None,
            payload=payload,
        )
        try:
            await self.runs.record(record)
        except Exception:  # a failed run record must not break the reply
            logger.exception("could not write the run record")

    # -- reading a thread ----------------------------------------------------

    async def messages(
        self,
        principal: Principal,
        thread_id: str,
        forward_headers: Mapping[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        thread_id = validate_thread_id(thread_id, self.runtime)
        if self.runtime == LANGGRAPH_SERVER:
            return await self._server_messages(principal, thread_id, forward_headers or {})
        assert self.threads is not None
        record = await self.threads.get(thread_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Unknown thread.")
        assert_access(principal, record)
        # Tool arguments reach a non-owner only under TRACE_CAPTURE=full.
        include_args = is_owner(principal, record) or capture_full()
        from {{cookiecutter.agent_directory}}.agent import graph

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
    ) -> AsyncIterator[tuple[str, Any]]:
        client = self._sdk_client(req.forward_headers)
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
            input={"messages": [{"role": "user", "content": req.message}]},
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
