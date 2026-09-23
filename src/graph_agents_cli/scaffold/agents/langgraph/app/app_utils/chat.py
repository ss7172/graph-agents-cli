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

`ChatRuntime.stream()` yields the events of CONTRACTS section 5
(`message.start`, `message.delta`, `tool.call`, `tool.result`, `message.end`,
`error`) from either:

* the in-process graph with the checkpointer bound at startup (`fastapi`), or
* the LangGraph Server this app is mounted in (`langgraph-server`), through the
  SDK's loopback client, so the server keeps owning persistence and threads.

It writes the run record (D8) and enforces thread ownership (D23) under both
runtimes: through the `threads` table under fastapi, and through the thread
metadata `{principal_id, tenant}` under langgraph-server. The SDK loopback
client runs under the server's `/noauth` root path, so the server's own
`@auth.on` filters never see these calls; the check has to live here.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException

from {{cookiecutter.agent_directory}}.app_utils.auth import Principal
from {{cookiecutter.agent_directory}}.app_utils.checkpointer import checkpointer_kind, get_checkpointer
from {{cookiecutter.agent_directory}}.app_utils.content import content_to_text
from {{cookiecutter.agent_directory}}.app_utils.db import Database, RunRecord, RunStore, capture_full
from {{cookiecutter.agent_directory}}.app_utils.model import model_label
from {{cookiecutter.agent_directory}}.app_utils.threads import (
    ThreadRecord,
    ThreadStore,
    assert_access,
    assert_owner,
    is_owner,
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

# Headers forwarded to the server under langgraph-server. They matter when
# LANGGRAPH_SERVER_URL points at a real HTTP endpoint (the auth handler runs
# there); the default in-process loopback ignores them.
FORWARDED_HEADERS = ("authorization", "cookie", "x-session-token")


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


def sse_encode(event: str, data: Mapping[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


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


def _safe_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Client metadata that is safe to attach to a run: scalar values only."""
    return {
        str(k): v
        for k, v in metadata.items()
        if isinstance(v, str | int | float | bool) and not str(k).startswith("_")
    }


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


class ChatRuntime:
    """Process-wide chat runtime, started and stopped by the app lifespan."""

    def __init__(self) -> None:
        self.runtime = detect_runtime()
        self.db: Database | None = None
        self.runs: RunStore | None = None
        self.threads: ThreadStore | None = None
        self._exit: AsyncExitStack | None = None
        self.started = False

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self.started:
            return
        self.runtime = detect_runtime()
        self._exit = AsyncExitStack()
        if self.runtime == FASTAPI:
            from {{cookiecutter.agent_directory}}.agent import graph

            saver = await self._exit.enter_async_context(get_checkpointer())
            graph.checkpointer = saver
            self.db = Database.from_env()
            await self.db.open()
            self._exit.push_async_callback(self.db.close)
        else:
            # The server binds persistence; keep an in-process run-record store.
            self.db = Database("memory")
        self.runs = RunStore(self.db)
        self.threads = ThreadStore(self.db)
        self.started = True
        logger.info(
            "chat runtime started: runtime=%s checkpointer=%s",
            self.runtime,
            self.checkpointer_kind(),
        )

    async def stop(self) -> None:
        if self._exit is not None:
            await self._exit.aclose()
            self._exit = None
        self.started = False

    def checkpointer_kind(self) -> str:
        if self.runtime == LANGGRAPH_SERVER:
            return "postgres" if os.environ.get("DATABASE_URI") else "memory"
        return checkpointer_kind()

    # -- threads -----------------------------------------------------------

    async def resolve_thread(self, principal: Principal, req: ChatRequest) -> str:
        """The thread id for this request, after the ownership check (403 before streaming)."""
        if self.runtime == LANGGRAPH_SERVER:
            return await self._server_resolve_thread(principal, req)
        assert self.threads is not None
        thread_id = req.thread_id or str(uuid.uuid4())
        await self.threads.ensure(thread_id, principal)
        return thread_id

    # -- streaming ---------------------------------------------------------

    async def stream(
        self, principal: Principal, req: ChatRequest, thread_id: str
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        run_id = str(uuid.uuid4())
        started = time.perf_counter()
        state = _RunState()
        status = "ok"
        error: BaseException | None = None
        yield EVENT_START, {"thread_id": thread_id, "run_id": run_id}
        try:
            if self.runtime == LANGGRAPH_SERVER:
                source = self._server_events(principal, req, thread_id, run_id)
            else:
                source = self._local_events(principal, req, thread_id, run_id)
            async for mode, data in source:
                for event in map_stream_item(mode, data, state):
                    yield event
        except Exception as exc:  # reported to the caller as an event
            status = "error"
            error = exc
            logger.exception("run %s failed", run_id)
            yield EVENT_ERROR, {"code": type(exc).__name__, "message": str(exc)}
        finally:
            latency_ms = int((time.perf_counter() - started) * 1000)
            await self._record_run(
                principal, req, thread_id, run_id, state, status, error, latency_ms
            )
        if status == "ok":
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
                    "status": "ok",
                },
            )

    async def _local_events(
        self, principal: Principal, req: ChatRequest, thread_id: str, run_id: str
    ) -> AsyncIterator[tuple[str, Any]]:
        from {{cookiecutter.agent_directory}}.agent import AgentContext, graph

        config = {
            "configurable": {"thread_id": thread_id},
            "run_id": uuid.UUID(run_id),
            "metadata": {
                "thread_id": thread_id,
                "run_id": run_id,
                "principal_hash": principal.hashed_id(),
                **_safe_metadata(req.metadata),
            },
        }
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
                "metadata": _safe_metadata(req.metadata),
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
            payload=payload,
        )
        try:
            await self.runs.record(record)
        except Exception:  # a failed run record must not break the reply
            logger.exception("could not write run record %s", run_id)

    # -- reading a thread ----------------------------------------------------

    async def messages(
        self,
        principal: Principal,
        thread_id: str,
        forward_headers: Mapping[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        if self.runtime == LANGGRAPH_SERVER:
            return await self._server_messages(principal, thread_id, forward_headers or {})
        assert self.threads is not None
        record = await self.threads.get(thread_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Unknown thread.")
        assert_access(principal, record)
        # Tool arguments reach a non-owner only under TRACE_CAPTURE=full (CONTRACTS section 5).
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

        headers = {k: v for k, v in req_headers.items() if k.lower() in FORWARDED_HEADERS}
        return get_client(
            url=os.environ.get("LANGGRAPH_SERVER_URL") or None, headers=headers or None
        )

    async def _server_thread_record(self, client: Any, thread_id: str) -> ThreadRecord | None:
        """The thread's ownership metadata as the app wrote it at creation, or None when absent.

        The loopback client is unauthenticated on the server, so a non-404
        error is a transport/server failure (503), never an ownership signal.
        A thread without `principal_id` metadata (created through the native
        API without this app) fails closed: nobody but a read-across role reads it.
        """
        try:
            thread = await client.threads.get(thread_id)
        except Exception as exc:
            if "404" in str(exc):
                return None
            raise HTTPException(
                status_code=503,
                detail=f"LangGraph Server unavailable: {type(exc).__name__}: {exc}",
            ) from exc
        meta = thread.get("metadata") or {} if isinstance(thread, Mapping) else {}
        return ThreadRecord(
            thread_id=str(thread.get("thread_id") or thread_id),
            principal_id=str(meta.get("principal_id") or ""),
            tenant=meta.get("tenant"),
        )

    async def _server_resolve_thread(self, principal: Principal, req: ChatRequest) -> str:
        client = self._sdk_client(req.forward_headers)
        metadata = {"principal_id": principal.id, "tenant": principal.attributes.get("tenant")}
        if req.thread_id:
            record = await self._server_thread_record(client, req.thread_id)
            if record is not None:
                # chat.send is a write: the owner only (403 before message.start).
                assert_owner(principal, record)
                return record.thread_id
        try:
            thread = await client.threads.create(thread_id=req.thread_id, metadata=metadata)
        except Exception as exc:  # loopback not configured, server down, ...
            raise HTTPException(
                status_code=503,
                detail=f"LangGraph Server unavailable: {type(exc).__name__}: {exc}",
            ) from exc
        return str(thread["thread_id"])

    async def _server_events(
        self, principal: Principal, req: ChatRequest, thread_id: str, run_id: str
    ) -> AsyncIterator[tuple[str, Any]]:
        client = self._sdk_client(req.forward_headers)
        async for part in client.runs.stream(
            thread_id,
            GRAPH_ID,
            input={"messages": [{"role": "user", "content": req.message}]},
            stream_mode=["messages-tuple", "updates"],
            metadata={
                "run_id": run_id,
                "principal_id": principal.id,
                "principal_hash": principal.hashed_id(),
                **_safe_metadata(req.metadata),
            },
            context={
                "principal_id": principal.id,
                "roles": list(principal.roles),
                "attributes": dict(principal.attributes),
            },
        ):
            event = str(getattr(part, "event", ""))
            data = getattr(part, "data", None)
            if event.startswith("messages"):
                yield "messages", data
            elif event.startswith("updates"):
                yield "updates", data
            elif event == "error":
                raise RuntimeError(str(data))

    async def _server_messages(
        self, principal: Principal, thread_id: str, forward_headers: Mapping[str, str]
    ) -> list[dict[str, Any]]:
        client = self._sdk_client(forward_headers)
        record = await self._server_thread_record(client, thread_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Unknown thread.")
        assert_access(principal, record)
        # Tool arguments reach a non-owner only under TRACE_CAPTURE=full (CONTRACTS section 5).
        include_args = is_owner(principal, record) or capture_full()
        try:
            state = await client.threads.get_state(thread_id)
        except Exception as exc:
            raise HTTPException(status_code=404, detail="Unknown or inaccessible thread.") from exc
        values = state.get("values") or {}
        return [
            serialize_message(m, include_tool_args=include_args) for m in values.get("messages", [])
        ]


RUNTIME = ChatRuntime()
