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

"""Building blocks of the runtime's failure handling, without a database.

Tool-call history repair, the step budget, connection defaults and error
classification, database health, and stopping a run when its lease is lost.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from {{cookiecutter.agent_directory}}.app_utils import chat, checkpointer, limits, metrics
from {{cookiecutter.agent_directory}}.app_utils.chat import (
    _lease_lost,
    _Pump,
    dangling_tool_calls,
    repair_tool_history,
)
from {{cookiecutter.agent_directory}}.app_utils.threads import LeaseLost


def ai(call_ids: list[str], id: str = "") -> AIMessage:
    return AIMessage(
        content="",
        id=id or f"ai-{'-'.join(call_ids)}",
        tool_calls=[{"id": c, "name": "get_weather", "args": {}} for c in call_ids],
    )


def tool(call_id: str) -> ToolMessage:
    return ToolMessage(content="sunny", tool_call_id=call_id, id=f"tool-{call_id}")


def human(text: str) -> HumanMessage:
    return HumanMessage(content=text, id=f"h-{text}")


def error_result(call: Any) -> ToolMessage:
    return ToolMessage(content="did not finish", tool_call_id=call["id"], status="error")


def shape(messages: list[Any]) -> list[str]:
    out = []
    for m in messages:
        if isinstance(m, ToolMessage):
            out.append(f"tool:{m.tool_call_id}:{m.status}")
        elif isinstance(m, AIMessage):
            out.append("ai(" + ",".join(c["id"] for c in m.tool_calls) + ")")
        else:
            out.append("user")
    return out


# --- tool-call history repair ---------------------------------------------------------


def test_a_valid_history_needs_no_repair() -> None:
    history = [human("a"), ai(["c1", "c2"]), tool("c1"), tool("c2"), AIMessage(content="ok")]
    assert repair_tool_history(history, error_result) is None
    assert dangling_tool_calls(history) == []


def test_open_calls_at_the_end_are_answered_in_place() -> None:
    """A run killed between the tool call and its result (the crash / outage case)."""
    history = [human("a"), ai(["c1", "c2"]), tool("c1")]
    repair = repair_tool_history(history, error_result)
    assert repair is not None and repair.append_only
    assert shape(repair.messages) == ["user", "ai(c1,c2)", "tool:c1:success", "tool:c2:error"]
    assert repair.added == repair.messages[-1:]


def test_a_result_after_the_next_user_turn_is_moved_back() -> None:
    """The shape the old repair left behind: every later turn got a provider 400."""
    history = [human("a"), ai(["c1"]), human("b"), tool("c1"), human("c")]
    assert [c["id"] for c in dangling_tool_calls(history[:4])] == ["c1"]
    repair = repair_tool_history(history, error_result)
    assert repair is not None and not repair.append_only and repair.added == []
    assert shape(repair.messages) == ["user", "ai(c1)", "tool:c1:success", "user", "user"]


def test_open_calls_before_a_later_turn_get_a_result_in_place() -> None:
    history = [human("a"), ai(["c1"]), human("b"), AIMessage(content="hi", id="x")]
    repair = repair_tool_history(history, error_result)
    assert repair is not None and not repair.append_only
    assert shape(repair.messages) == ["user", "ai(c1)", "tool:c1:error", "user", "ai()"]


def test_results_that_answer_no_call_are_dropped() -> None:
    history = [human("a"), tool("orphan"), AIMessage(content="hi", id="x")]
    repair = repair_tool_history(history, error_result)
    assert repair is not None
    assert shape(repair.messages) == ["user", "ai()"]


def test_repair_works_on_server_dicts() -> None:
    history: list[Any] = [
        {"type": "human", "id": "m1", "content": "a"},
        {"type": "ai", "id": "m2", "content": "", "tool_calls": [{"id": "c1", "name": "t"}]},
        {"type": "human", "id": "m3", "content": "b"},
        {"type": "tool", "id": "m4", "tool_call_id": "c1", "content": "x"},
    ]
    repair = repair_tool_history(history, lambda call: {"type": "tool", "id": "new"})
    assert repair is not None
    assert [m["id"] for m in repair.messages] == ["m1", "m2", "m4", "m3"]
    kept = chat._server_message(
        {**history[1], "usage_metadata": {"input_tokens": 1}, "invalid_tool_calls": []}
    )
    assert set(kept) == {"type", "content", "id", "tool_calls"}


# --- step budget -------------------------------------------------------------------------


def test_the_default_step_budget_fits_the_documented_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RECURSION_LIMIT", raising=False)
    assert limits.recursion_limit() == 50
    assert limits.sequential_tool_calls(50) == 24
    assert limits.steps_for_tool_calls(20) == 42
    assert limits.sequential_tool_calls(limits.steps_for_tool_calls(20)) == 20
    assert limits.sequential_tool_calls(1) == 0


def test_a_policy_limit_beyond_the_step_budget_is_reported_at_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, caplog: pytest.LogCaptureFixture
) -> None:
    from {{cookiecutter.agent_directory}}.app_utils import api_client

    policy = tmp_path / "api-policy.yaml"
    policy.write_text(
        "apis:\n"
        "  orders:\n"
        "    base_url_env: ORDERS_URL\n"
        "    auth: none\n"
        "    allowed_methods: [GET]\n"
        "    limits: {max_calls_per_run: 30}\n"
    )
    monkeypatch.setenv("API_POLICY_PATH", str(policy))
    api_client.reset_policy_cache()
    try:
        runtime = chat.ChatRuntime()
        monkeypatch.setenv("RECURSION_LIMIT", "50")
        with caplog.at_level(logging.WARNING):
            runtime._check_step_budget()
        assert "max_calls_per_run=30" in caplog.text and "at least 62" in caplog.text
        caplog.clear()
        monkeypatch.setenv("RECURSION_LIMIT", "62")
        with caplog.at_level(logging.WARNING):
            runtime._check_step_budget()
        assert "max_calls_per_run" not in caplog.text
    finally:
        api_client.reset_policy_cache()


# --- connections and database health -------------------------------------------------------


def test_connection_defaults_bound_outages_unless_the_dsn_says_otherwise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PGCONNECT_TIMEOUT", raising=False)
    extra = checkpointer.connection_kwargs("postgresql://u:p@db:5432/app")
    assert extra["connect_timeout"] == "5" and extra["keepalives_idle"] == "30"
    extra = checkpointer.connection_kwargs(
        "postgresql://u:p@db:5432/app?connect_timeout=20&keepalives_idle=5"
    )
    assert "connect_timeout" not in extra and "keepalives_idle" not in extra
    assert extra["keepalives_interval"] == "10"
    monkeypatch.setenv("PGCONNECT_TIMEOUT", "9")
    assert "connect_timeout" not in checkpointer.connection_kwargs("host=db dbname=app")
    with pytest.raises(limits.SettingsError):
        checkpointer.connection_kwargs("postgresql://u:p@db:5432/app?nonsense_option=1")


def test_connection_errors_are_told_apart_from_failed_statements() -> None:
    from psycopg import OperationalError, ProgrammingError, errors
    from psycopg_pool import PoolTimeout

    assert checkpointer.is_connection_error(PoolTimeout("no connection"))
    assert checkpointer.is_connection_error(OperationalError("connection refused"))
    assert checkpointer.is_connection_error(ConnectionRefusedError())
    assert checkpointer.is_connection_error(errors.AdminShutdown("terminating connection"))
    assert not checkpointer.is_connection_error(errors.QueryCanceled("statement timeout"))
    assert not checkpointer.is_connection_error(ProgrammingError("syntax error"))
    assert not checkpointer.is_connection_error(ValueError("x"))


def test_database_health_changes_are_logged_once_and_shorten_the_wait(
    caplog: pytest.LogCaptureFixture,
) -> None:
    health = checkpointer.DbHealth()
    assert health.checkout_timeout() == checkpointer.POOL_TIMEOUT_S
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            health.mark_down(OSError("connection refused\nsecond line"))
        assert health.checkout_timeout() == checkpointer.DOWN_POOL_TIMEOUT_S
        assert metrics.DATABASE_UP._value.get() == 0
        health.mark_up()
        health.mark_up()
    assert caplog.text.count("database unreachable: OSError: connection refused") == 1
    assert "second line" not in caplog.text
    assert caplog.text.count("database reachable again") == 1
    assert metrics.DATABASE_UP._value.get() == 1


def test_database_errors_become_a_503_with_one_log_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from fastapi import HTTPException
    from psycopg_pool import PoolTimeout

    with caplog.at_level(logging.WARNING), pytest.raises(HTTPException) as exc:
        with chat.database_errors():
            raise PoolTimeout("couldn't get a connection after 2.00 sec")
    assert exc.value.status_code == 503 and "Reference: " in exc.value.detail
    (record,) = [r for r in caplog.records if "unavailable" in r.getMessage()]
    assert record.levelno == logging.WARNING and record.exc_info is None
    with pytest.raises(ValueError), chat.database_errors():
        raise ValueError("not a database error")  # left to the 500 handler


# --- a lost lease stops the run -----------------------------------------------------------


async def test_an_interrupted_pump_stops_its_source_and_reports_why() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def source():
        yield "first"
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        yield "never"

    pump = _Pump(source())
    assert await pump.next(1) == ("item", "first")
    await started.wait()
    lost = LeaseLost("t1", "taken over")
    pump.interrupt(lost)
    assert await pump.next(1) == ("failed", lost)
    await asyncio.wait_for(cancelled.wait(), 1)
    await pump.close()


async def test_a_run_still_stopping_keeps_its_thread_busy() -> None:
    """A cancelled graph can take a moment to stop: no new run may start on its thread meanwhile."""
    from {{cookiecutter.agent_directory}}.app_utils.threads import ThreadBusy, ThreadLocks

    locks = ThreadLocks()
    lease = await locks.acquire("t1")
    stopping = asyncio.get_running_loop().create_future()
    lease.hold_until(stopping)
    await lease.release()  # the run is over for the client...
    with pytest.raises(ThreadBusy):  # ...but its graph has not stopped yet
        await locks.acquire("t1")
    with pytest.raises(LeaseLost):  # and whatever it still writes is refused
        locks.fence("t1")
    stopping.set_result(None)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    again = await locks.acquire("t1")
    await again.release()
    assert locks.held == frozenset()


def test_a_lease_loss_is_found_behind_the_error_it_caused() -> None:
    lost = LeaseLost("t1", "expired")
    try:
        try:
            raise lost
        except LeaseLost as inner:
            raise RuntimeError("graph failed") from inner
    except RuntimeError as outer:
        assert _lease_lost(outer) is lost
    assert _lease_lost(ValueError("x")) is None
