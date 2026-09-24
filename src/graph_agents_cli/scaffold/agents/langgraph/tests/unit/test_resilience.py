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

Tool-call history repair, tool calls whose arguments are not valid JSON, the
step budget, connection defaults and error classification, database health,
and stopping a run when its lease is lost.
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
from {{cookiecutter.agent_directory}}.app_utils.content import (
    INVALID_TOOL_CALL_RESULT,
    AnswerInvalidToolCalls,
    UntrustedToolResults,
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


# --- repeated tool-call ids ------------------------------------------------------------
# Models reuse ids across turns: the template's fake model always calls `call_get_weather`,
# and some providers number the calls of each message `call_0`, `call_1`, ...


def turn(text: str, call_ids: list[str], n: int) -> list[Any]:
    """One healthy turn: the user, a tool call, its results, the reply."""
    return [
        human(f"{text}{n}"),
        ai(call_ids, id=f"ai-{n}"),
        *(ToolMessage(content=f"r{n}-{c}", tool_call_id=c, id=f"t{n}-{c}") for c in call_ids),
        AIMessage(content=f"reply {n}", id=f"reply-{n}"),
    ]


def test_healthy_turns_with_repeated_ids_are_never_rewritten() -> None:
    history = [*turn("sf", ["call_0"], 1), *turn("paris", ["call_0"], 2)]
    history += turn("both", ["call_0", "call_1"], 3)
    assert repair_tool_history(history, error_result) is None


def test_random_healthy_histories_are_never_rewritten() -> None:
    """Any mix of turns, ids reused or not, parallel calls or none: nothing to repair."""
    import random

    rng = random.Random(1234)
    for _ in range(300):
        history: list[Any] = []
        for n in range(rng.randint(1, 6)):
            width = rng.randint(0, 3)
            ids = [
                rng.choice(["call_0", "call_1", "call_get_weather", f"u{n}{i}"])
                for i in range(width)
            ]
            ids = list(dict.fromkeys(ids))
            if ids:
                history += turn("q", ids, n)
            else:
                history += [human(f"q{n}"), AIMessage(content="plain", id=f"plain-{n}")]
        assert repair_tool_history(history, error_result) is None, shape(history)


def test_a_repeated_id_keeps_every_turns_result_when_the_last_call_is_open() -> None:
    """The regression: turn 2's result was deleted because turn 1 used the same id."""
    history = [*turn("sf", ["call_0"], 1), *turn("paris", ["call_0"], 2)]
    history += [human("again"), ai(["call_0"], id="ai-3")]
    repair = repair_tool_history(history, error_result)
    assert repair is not None and repair.append_only
    assert [m.id for m in repair.messages[: len(history)]] == [m.id for m in history]
    assert shape(repair.messages[-2:]) == ["ai(call_0)", "tool:call_0:error"]
    assert [m.content for m in repair.messages if isinstance(m, ToolMessage)][:2] == [
        "r1-call_0",
        "r2-call_0",
    ]


def test_a_misplaced_result_goes_back_to_its_own_turn_when_ids_repeat() -> None:
    first = turn("sf", ["call_0"], 1)
    # Turn 2's result landed after the next user message; turn 3 reuses the id.
    history = [
        *first,
        human("paris"),
        ai(["call_0"], id="ai-2"),
        human("hello?"),
        ToolMessage(content="late", tool_call_id="call_0", id="late"),
        *turn("tokyo", ["call_0"], 3),
    ]
    repair = repair_tool_history(history, error_result)
    assert repair is not None and not repair.append_only and repair.added == []
    ids = [m.id for m in repair.messages]
    assert ids[ids.index("ai-2") + 1] == "late"
    assert "t1-call_0" in ids and "t3-call_0" in ids  # the other turns keep theirs
    assert repair_tool_history(repair.messages, error_result) is None


def test_a_later_turns_result_is_never_taken_for_an_earlier_open_call() -> None:
    history = [human("a"), ai(["call_0"], id="ai-1"), human("b"), *turn("c", ["call_0"], 2)]
    repair = repair_tool_history(history, error_result)
    assert repair is not None
    assert shape(repair.messages)[:3] == ["user", "ai(call_0)", "tool:call_0:error"]
    assert "t2-call_0" in [m.id for m in repair.messages]


@pytest.mark.parametrize("call_id", ["dup", ""])
def test_one_id_for_two_calls_of_one_message_keeps_both_results(call_id: str) -> None:
    """Some OpenAI-compatible servers repeat an id (or send none) within one message."""
    history = turn("sf and rome", [call_id, call_id], 1)
    history[3] = ToolMessage(content="rome", tool_call_id=call_id, id="t1-second")
    assert repair_tool_history(history, error_result) is None
    assert [c["id"] for c in dangling_tool_calls(history[:4])] == []
    # Only one of the two answered: the other gets an error result, the answer stays.
    repair = repair_tool_history(history[:3], error_result)
    assert repair is not None and repair.append_only and len(repair.added) == 1
    assert shape(repair.messages)[2:] == [f"tool:{call_id}:success", f"tool:{call_id}:error"]
    assert [c["id"] for c in dangling_tool_calls(history[:3])] == [call_id]
    # A third result for the id answers no call: dropped.
    extra = ToolMessage(content="again", tool_call_id=call_id, id="t1-third")
    repair = repair_tool_history([*history[:4], extra, history[4]], error_result)
    assert repair is not None and [m.id for m in repair.messages] == [m.id for m in history]


def invalid_call(call_id: str, raw: str = "{'query': 'SF'}") -> AIMessage:
    """An assistant message whose only tool call has arguments that are not valid JSON."""
    return AIMessage(
        content="",
        id=f"ai-invalid-{call_id}",
        invalid_tool_calls=[
            {
                "type": "invalid_tool_call",
                "id": call_id,
                "name": "get_weather",
                "args": raw,
                "error": "not valid JSON",
            }
        ],
    )


def test_a_call_with_invalid_arguments_needs_a_result_too() -> None:
    """LangChain sends invalid calls back to the provider as calls: each needs a result."""

    def result(call: Any) -> ToolMessage:
        text = chat.open_call_result_text(call, "did not finish")
        return ToolMessage(content=text, tool_call_id=call["id"], status="error")

    history = [human("a"), invalid_call("call_0"), human("b")]
    repair = repair_tool_history(history, result)
    assert repair is not None and not repair.append_only
    assert shape(repair.messages) == ["user", "ai()", "tool:call_0:error", "user"]
    assert repair.messages[2].content == INVALID_TOOL_CALL_RESULT
    assert repair_tool_history(repair.messages, result) is None
    assert [c["id"] for c in dangling_tool_calls(history[:2])] == ["call_0"]
    # A valid call next to it keeps the stop reason.
    both = invalid_call("call_1")
    both.tool_calls = [{"id": "call_0", "name": "get_weather", "args": {}}]
    repair = repair_tool_history([human("a"), both], result)
    assert repair is not None and repair.append_only
    assert [m.content for m in repair.added] == ["did not finish", INVALID_TOOL_CALL_RESULT]


def test_the_server_rebuilds_an_invalid_call_as_a_call_its_result_answers() -> None:
    """The server has no key for `invalid_tool_calls`: kept as a call with no arguments."""
    message = invalid_call("call_0").model_dump()
    kept = chat._server_message(message)
    assert "invalid_tool_calls" not in kept
    assert kept["tool_calls"] == [
        {"name": "get_weather", "args": {}, "id": "call_0", "type": "tool_call"}
    ]


# --- tool calls whose arguments are not valid JSON --------------------------------------


def _agent(model: Any, tools: list[Any]) -> Any:
    from langchain.agents import create_agent

    return create_agent(
        model=model, tools=tools, middleware=[AnswerInvalidToolCalls(), UntrustedToolResults()]
    )


def _weather_tool() -> Any:
    from langchain_core.tools import tool

    @tool
    def get_weather(query: str) -> str:
        """Test-only tool: the weather for QUERY."""
        return f"sunny in {query}"

    return get_weather


@pytest.mark.parametrize("script", ["BADARGS", "TRAILINGCOMMA"])
async def test_invalid_arguments_are_answered_and_the_model_replies(
    openai_compatible: Any, script: str
) -> None:
    """Without the answer the run ended with no reply and every later turn was a 400."""
    graph = _agent(openai_compatible.model(), [_weather_tool()])
    state = await graph.ainvoke({"messages": [{"role": "user", "content": f"weather {script}"}]})
    messages = state["messages"]
    assert shape(messages) == ["user", "ai()", "tool:call_0:error", "ai()"]
    assert messages[1].invalid_tool_calls and messages[2].content == INVALID_TOOL_CALL_RESULT
    assert messages[-1].content.startswith("Found: ") and "not valid JSON" in messages[-1].content
    assert openai_compatible.refusals == []
    assert repair_tool_history(messages, error_result) is None
    # The next turn is accepted by the provider.
    state = await graph.ainvoke({"messages": [*messages, HumanMessage("thanks")]})
    assert state["messages"][-1].content == "ok" and openai_compatible.refusals == []


async def test_a_model_that_never_gets_the_arguments_right_is_asked_twice_more(
    openai_compatible: Any,
) -> None:
    graph = _agent(openai_compatible.model(), [_weather_tool()])
    state = await graph.ainvoke({"messages": [{"role": "user", "content": "ALWAYSBAD"}]})
    assert shape(state["messages"]) == [
        "user",
        *(item for n in range(3) for item in ("ai()", f"tool:call_{n}:error")),
    ]
    assert len(openai_compatible.requests) == 3 and openai_compatible.refusals == []
    assert repair_tool_history(state["messages"], error_result) is None
    # The three tries take the steps of one plain reply: the step budget counts the same.
    from langgraph.errors import GraphRecursionError

    budget = 1
    while budget < 20 and isinstance(
        await _outcome(graph.with_config({"recursion_limit": budget}), "hello"),
        GraphRecursionError,
    ):
        budget += 1
    state = await _outcome(graph.with_config({"recursion_limit": budget}), "ALWAYSBAD")
    assert not isinstance(state, Exception) and len(state["messages"]) == 7


async def _outcome(graph: Any, text: str) -> Any:
    try:
        return await graph.ainvoke({"messages": [{"role": "user", "content": text}]})
    except Exception as exc:
        return exc


async def test_a_valid_call_next_to_an_invalid_one_still_runs(openai_compatible: Any) -> None:
    graph = _agent(openai_compatible.model(), [_weather_tool()])
    state = await graph.ainvoke({"messages": [{"role": "user", "content": "BADANDGOOD"}]})
    results = {m.tool_call_id: m for m in state["messages"] if isinstance(m, ToolMessage)}
    assert results["call_0"].content == "sunny in SF" and results["call_0"].status == "success"
    assert results["call_1"].content == INVALID_TOOL_CALL_RESULT
    assert state["messages"][-1].content.startswith("Found: ")
    assert openai_compatible.refusals == []


async def test_one_id_for_two_calls_runs_both_and_the_next_turn_is_accepted(
    openai_compatible: Any,
) -> None:
    graph = _agent(openai_compatible.model(), [_weather_tool()])
    state = await graph.ainvoke({"messages": [{"role": "user", "content": "DUPIDS"}]})
    results = [m.content for m in state["messages"] if isinstance(m, ToolMessage)]
    assert sorted(results) == ["sunny in Rome", "sunny in SF"]
    assert repair_tool_history(state["messages"], error_result) is None
    state = await graph.ainvoke({"messages": [*state["messages"], HumanMessage("thanks")]})
    assert openai_compatible.refusals == []


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


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://agent:S3CRETpw%zz@127.0.0.1:5432/app",
        "host=db user=agent password=S3CRETpw dbname",
        "host=db password='S3CRETpw",
    ],
)
def test_a_dsn_that_does_not_parse_never_shows_its_text(dsn: str) -> None:
    with pytest.raises(limits.SettingsError) as exc:
        checkpointer.connection_kwargs(dsn)
    assert "S3CRET" not in str(exc.value) and "does not parse" in str(exc.value)


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
