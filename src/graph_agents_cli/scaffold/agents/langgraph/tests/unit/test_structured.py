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

"""Structured final answers (`app_utils/structured.py`), without a server.

The response schema is found and checked (fail closed: a keyword the checker
does not support stops startup), answers are validated against it, the
strategy is chosen as LangChain would with a strict provider schema, the fake
model answers in the shape, and `StructuredAnswer` sends an answer that does
not fit back to the model, then fails the step. The chat runtime's stream
mapping keeps the answer and hides the answer tool.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain.agents.structured_output import ProviderStrategy, ToolStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from pydantic import Field

from {{cookiecutter.agent_directory}}.app_utils import structured
from {{cookiecutter.agent_directory}}.app_utils.chat import (
    CODE_INVALID_STRUCTURED_RESPONSE,
    EVENT_DELTA,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    ChatRuntime,
    _RunState,
    _ServerRunError,
    map_stream_item,
)
from {{cookiecutter.agent_directory}}.app_utils.limits import SettingsError
from {{cookiecutter.agent_directory}}.app_utils.model import FakeChatModel, _fake_answer
from {{cookiecutter.agent_directory}}.app_utils.structured import (
    ANSWER_TOOL,
    StructuredAnswer,
    StructuredAnswerError,
    response_format,
    response_schema,
    response_strategy,
    schema_problems,
    validate,
)

SCHEMA: dict[str, Any] = {
    "title": "Weather report",
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "city": {"type": ["string", "null"]},
    },
    "required": ["answer", "confidence"],
    "additionalProperties": False,
}


def _write(path: Path, schema: Any) -> Path:
    path.write_text(json.dumps(schema), encoding="utf-8")
    return path


@pytest.fixture
def schema_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = _write(tmp_path / "response_schema.json", SCHEMA)
    monkeypatch.setenv("RESPONSE_SCHEMA_PATH", str(path))
    monkeypatch.delenv("RESPONSE_FORMAT_STRATEGY", raising=False)
    return path


# --- the schema -----------------------------------------------------------------------


def test_no_schema_file_means_no_structured_answers(monkeypatch) -> None:
    monkeypatch.delenv("RESPONSE_SCHEMA_PATH", raising=False)
    assert response_schema() is None and not structured.enabled()
    assert response_format(FakeChatModel(), []) is None


def test_the_schema_file_is_read_and_checked(schema_file: Path) -> None:
    assert response_schema() == SCHEMA and structured.enabled()


def test_a_named_schema_file_that_does_not_exist_stops_startup(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RESPONSE_SCHEMA_PATH", str(tmp_path / "missing.json"))
    with pytest.raises(SettingsError, match="RESPONSE_SCHEMA_PATH"):
        response_schema()


def test_a_file_that_is_not_json_stops_startup(tmp_path, monkeypatch) -> None:
    path = tmp_path / "response_schema.json"
    path.write_text("{'type': 'object'}", encoding="utf-8")
    monkeypatch.setenv("RESPONSE_SCHEMA_PATH", str(path))
    with pytest.raises(SettingsError, match="not a JSON file"):
        response_schema()


@pytest.mark.parametrize(
    ("schema", "problem"),
    [
        ([], "must be a JSON object"),
        ({"type": "array", "items": {"type": "string"}}, "root must be an object schema"),
        ({"type": ["object", "null"]}, "root must be an object schema"),
        ({"type": "object", "if": {}, "then": {}}, "`if` is not a keyword"),
        ({"type": "object", "patternProperties": {"^x": {}}}, "`patternProperties`"),
        ({"type": "object", "requried": ["a"]}, "`requried` is not a keyword"),
        ({"type": "object", "properties": {"a": {"type": "text"}}}, "must name JSON types"),
        ({"type": "object", "properties": {"a": {"$ref": "#/$defs/missing"}}}, "does not point"),
        ({"type": "object", "properties": {"a": {"$ref": "other.json#/x"}}}, "does not point"),
        ({"type": "object", "properties": {"a": {"pattern": "("}}}, "not a regular expression"),
        ({"type": "object", "properties": {"a": {"items": [{}, {}]}}}, "a list of schemas"),
        ({"type": "object", "properties": {"a": {"minLength": -1}}}, "whole number >= 0"),
        ({"type": "object", "properties": {"a": {"multipleOf": 0}}}, "number > 0"),
        ({"type": "object", "required": "a"}, "list of property names"),
        ({"type": "object", "properties": {"a": {"anyOf": []}}}, "non-empty list of schemas"),
    ],
)
def test_a_schema_the_checker_cannot_check_is_refused(schema: Any, problem: str) -> None:
    problems = schema_problems(schema)
    assert any(problem in p for p in problems), problems


def test_a_refused_schema_stops_startup_naming_the_problem(tmp_path, monkeypatch) -> None:
    path = _write(tmp_path / "s.json", {"type": "object", "if": {"type": "object"}})
    monkeypatch.setenv("RESPONSE_SCHEMA_PATH", str(path))
    with pytest.raises(SettingsError, match=r"cannot be the response schema.*`if`"):
        response_schema()


def test_a_supported_schema_passes_the_check() -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "description": "An order summary.",
        "properties": {
            "order": {"$ref": "#/$defs/order"},
            "tags": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
            "status": {"enum": ["open", "closed"]},
            "note": {"anyOf": [{"type": "string", "format": "date"}, {"type": "null"}]},
        },
        "required": ["order"],
        "$defs": {
            "order": {
                "type": "object",
                "properties": {"id": {"type": "string", "pattern": "^ORD-[0-9]{5}$"}},
                "required": ["id"],
            }
        },
    }
    assert schema_problems(schema) == []


# --- the check --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        ({"answer": "sunny", "confidence": 0.9}, None),
        ({"answer": "sunny", "confidence": 1, "city": None}, None),
        ({"answer": "sunny"}, "missing the required property 'confidence'"),
        ({"answer": 3, "confidence": 0.5}, "$.answer: must be string, not int"),
        ({"answer": "x", "confidence": 2}, "$.confidence: must be <= 1"),
        ({"answer": "x", "confidence": True}, "must be number, not bool"),
        ({"answer": "x", "confidence": 0.5, "extra": 1}, "'extra' is not allowed"),
        ({"answer": "x", "confidence": 0.5, "city": 7}, "must be string or null"),
        ([], "$: must be object, not list"),
    ],
)
def test_answers_are_checked_against_the_schema(value: Any, problem: str | None) -> None:
    errors = validate(SCHEMA, value)
    if problem is None:
        assert errors == []
    else:
        assert any(problem in e for e in errors), errors


def test_the_check_follows_refs_choices_and_json_equality() -> None:
    schema = {
        "type": "object",
        "properties": {
            "order": {"$ref": "#/$defs/order"},
            "count": {"type": "integer", "multipleOf": 2},
            "flag": {"const": True},
            "kind": {"oneOf": [{"const": "a"}, {"type": "string", "maxLength": 1}]},
            "tags": {"type": "array", "uniqueItems": True, "minItems": 1},
            "other": {"not": {"type": "null"}},
        },
        "$defs": {"order": {"type": "object", "required": ["id"]}},
    }
    assert validate(schema, {"order": {"id": "7"}, "count": 4.0, "flag": True, "kind": "b"}) == []
    errors = validate(
        schema,
        {"order": {}, "count": 3, "flag": 1, "kind": "a", "tags": [1, 1.0], "other": None},
    )
    assert "$.order: missing the required property 'id'" in errors
    assert "$.count: must be a multiple of 2" in errors
    assert "$.flag: must be true" in errors  # 1 is not true in JSON
    assert "$.kind: fits 2 of the oneOf choices, not exactly one" in errors
    assert "$.tags: the items must be unique" in errors  # 1 equals 1.0 in JSON
    assert "$.other: must not fit the `not` schema" in errors


# --- the strategy -----------------------------------------------------------------------


def test_the_strategy_setting(monkeypatch) -> None:
    monkeypatch.delenv("RESPONSE_FORMAT_STRATEGY", raising=False)
    assert response_strategy() == "auto"
    for value in ("provider", "TOOL", " auto "):
        monkeypatch.setenv("RESPONSE_FORMAT_STRATEGY", value)
        assert response_strategy() == value.strip().lower()
    monkeypatch.setenv("RESPONSE_FORMAT_STRATEGY", "json")
    with pytest.raises(SettingsError, match="RESPONSE_FORMAT_STRATEGY"):
        response_strategy()


def test_auto_picks_the_tool_for_a_model_without_structured_output(schema_file) -> None:
    fmt = response_format(FakeChatModel(), [])
    assert isinstance(fmt, ToolStrategy)
    (spec,) = fmt.schema_specs
    # A fixed name (a title with a space is not a valid tool name), the project's
    # description or a default one, and the project's properties.
    assert spec.name == ANSWER_TOOL and spec.description
    assert spec.json_schema["properties"] == SCHEMA["properties"]


def test_auto_picks_the_provider_strictly_for_a_model_that_has_it(schema_file) -> None:
    from langchain_openai import ChatOpenAI

    model = ChatOpenAI(model="gpt-5-mini", api_key="sk-test")
    fmt = response_format(model, [])
    assert isinstance(fmt, ProviderStrategy)
    # LangChain's own AutoStrategy would ask for a best-effort (non-strict) json_schema.
    assert fmt.to_model_kwargs()["response_format"]["json_schema"]["strict"] is True
    assert fmt.to_model_kwargs()["response_format"]["json_schema"]["name"] == ANSWER_TOOL


def test_an_explicit_strategy_wins(schema_file, monkeypatch) -> None:
    monkeypatch.setenv("RESPONSE_FORMAT_STRATEGY", "provider")
    assert isinstance(response_format(FakeChatModel(), []), ProviderStrategy)
    monkeypatch.setenv("RESPONSE_FORMAT_STRATEGY", "tool")
    from langchain_openai import ChatOpenAI

    assert isinstance(
        response_format(ChatOpenAI(model="gpt-5-mini", api_key="sk-test"), []), ToolStrategy
    )


def test_a_tool_named_like_the_answer_is_refused(schema_file) -> None:
    @tool
    def final_answer(text: str) -> str:
        """A project tool that happens to be called final_answer."""
        return text

    with pytest.raises(SettingsError, match="rename the tool"):
        response_format(FakeChatModel(), [final_answer])


# The shapes Anthropic's client refuses (a type list, an `enum` with no `type`), and the
# same answer written so that it takes it.
ANTHROPIC_REFUSES: dict[str, Any] = {
    "type": "object",
    "properties": {
        "category": {"enum": ["billing", "technical", "account", "other"]},
        "order_id": {"type": ["string", "null"], "pattern": "^ORD-[0-9]{5}$"},
    },
    "required": ["category", "order_id"],
    "additionalProperties": False,
}
ANTHROPIC_TAKES: dict[str, Any] = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": ["billing", "technical", "account", "other"]},
        "order_id": {"anyOf": [{"type": "string", "pattern": "^ORD-[0-9]{5}$"}, {"type": "null"}]},
    },
    "required": ["category", "order_id"],
    "additionalProperties": False,
}


def _claude() -> Any:
    from langchain_anthropic import ChatAnthropic

    # No request is sent: the strategy and the schema conversion happen before any.
    return ChatAnthropic(model="claude-sonnet-5", api_key="sk-test")


@pytest.mark.parametrize("schema", [ANTHROPIC_REFUSES, SCHEMA])
def test_auto_uses_the_tool_where_anthropic_cannot_take_the_schema(
    tmp_path, monkeypatch, schema
) -> None:
    # Every run would otherwise fail in the client, before its request (run_failed).
    monkeypatch.setenv("RESPONSE_SCHEMA_PATH", str(_write(tmp_path / "s.json", schema)))
    model = _claude()
    assert structured.provider_supported(model, [])  # the model has structured output
    assert structured.provider_refusal(model, schema) is not None
    assert isinstance(response_format(model, []), ToolStrategy)


def test_auto_uses_anthropics_structured_output_for_a_schema_it_takes(
    tmp_path, monkeypatch
) -> None:
    from anthropic import transform_schema

    monkeypatch.setenv("RESPONSE_SCHEMA_PATH", str(_write(tmp_path / "s.json", ANTHROPIC_TAKES)))
    fmt = response_format(_claude(), [])
    assert isinstance(fmt, ProviderStrategy)
    # What langchain-anthropic does with it before the request.
    transform_schema(fmt.to_model_kwargs()["response_format"]["json_schema"]["schema"])


def test_the_provider_strategy_with_a_schema_anthropic_cannot_take_stops_startup(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("RESPONSE_SCHEMA_PATH", str(_write(tmp_path / "s.json", ANTHROPIC_REFUSES)))
    monkeypatch.setenv("RESPONSE_FORMAT_STRATEGY", "provider")
    with pytest.raises(SettingsError, match=r"cannot send response_schema\.json.*=tool"):
        response_format(_claude(), [])
    # The tool strategy takes it; other providers' clients are not asked.
    monkeypatch.setenv("RESPONSE_FORMAT_STRATEGY", "tool")
    assert isinstance(response_format(_claude(), []), ToolStrategy)
    assert structured.provider_refusal(FakeChatModel(), ANTHROPIC_REFUSES) is None


# --- the fake model --------------------------------------------------------------------


def test_the_fake_answers_in_the_shape() -> None:
    answer = _fake_answer(SCHEMA, "sunny")
    assert answer == {"answer": "sunny", "confidence": 0, "city": "sunny"}
    assert validate(SCHEMA, answer) == []
    provider = FakeChatModel().bind_tools(
        [], response_format={"type": "json_schema", "json_schema": {"schema": SCHEMA}}
    )
    reply = provider.invoke([HumanMessage("hello")])
    assert json.loads(reply.content)["answer"] == "Hello! How can I help you today?"
    answer_tool = {"type": "function", "function": {"name": ANSWER_TOOL, "parameters": SCHEMA}}
    forced = FakeChatModel().bind_tools([answer_tool], tool_choice="any")
    (call,) = forced.invoke([HumanMessage("hello")]).tool_calls
    assert call["name"] == ANSWER_TOOL and call["args"]["confidence"] == 0
    # A request that names the answer tool does not call it early.
    unforced = FakeChatModel().bind_tools([answer_tool])
    assert unforced.invoke([HumanMessage("give the final answer")]).tool_calls == []


# --- the middleware ---------------------------------------------------------------------


class Scripted(BaseChatModel):
    """Returns its replies in order (the last one again when they run out); records requests."""

    replies: list[AIMessage]
    seen: list[list[Any]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        return self

    def _generate(
        self, messages: list[Any], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        self.seen.append(list(messages))
        reply = self.replies[min(len(self.seen), len(self.replies)) - 1]
        return ChatResult(generations=[ChatGeneration(message=reply)])


def _answer(args: dict[str, Any], tokens: int = 10) -> AIMessage:
    usage = {"input_tokens": tokens, "output_tokens": tokens, "total_tokens": 2 * tokens}
    call = {"name": ANSWER_TOOL, "args": args, "id": f"call_{tokens}"}
    return AIMessage(content="", tool_calls=[call], usage_metadata=usage)


def _graph(model: BaseChatModel, strategy: Any) -> Any:
    return create_agent(
        model=model, tools=[], response_format=strategy, middleware=[StructuredAnswer()]
    )


def _tool_strategy() -> ToolStrategy[Any]:
    return ToolStrategy({**SCHEMA, "title": ANSWER_TOOL})


async def test_an_answer_that_fits_passes_untouched(schema_file) -> None:
    model = Scripted(replies=[_answer({"answer": "sunny", "confidence": 0.5})])
    state = await _graph(model, _tool_strategy()).ainvoke({"messages": [HumanMessage("hi")]})
    assert state["structured_response"] == {"answer": "sunny", "confidence": 0.5}
    assert len(model.seen) == 1


async def test_an_answer_that_does_not_fit_is_sent_back_and_its_usage_counts(schema_file) -> None:
    model = Scripted(
        replies=[
            _answer({"answer": "sunny", "confidence": 7}, tokens=3),
            _answer({"answer": "sunny", "confidence": 0.7}, tokens=5),
        ]
    )
    state = await _graph(model, _tool_strategy()).ainvoke({"messages": [HumanMessage("hi")]})
    assert state["structured_response"] == {"answer": "sunny", "confidence": 0.7}
    # The model read its try and a tool error naming the problem.
    retry = model.seen[1]
    assert isinstance(retry[-1], ToolMessage) and retry[-1].status == "error"
    assert "$.confidence: must be <= 1" in retry[-1].content
    # The failed try is not kept in the thread, but its tokens count.
    answers = [m for m in state["messages"] if isinstance(m, AIMessage)]
    assert len(answers) == 1 and answers[0].usage_metadata["input_tokens"] == 8


async def test_a_plain_text_final_reply_is_sent_back_under_the_tool_strategy(schema_file) -> None:
    model = Scripted(
        replies=[AIMessage(content="It is sunny."), _answer({"answer": "x", "confidence": 1})]
    )
    state = await _graph(model, _tool_strategy()).ainvoke({"messages": [HumanMessage("hi")]})
    assert state["structured_response"] == {"answer": "x", "confidence": 1}
    note = model.seen[1][-1]
    assert isinstance(note, HumanMessage) and f"call {ANSWER_TOOL}" in note.content


async def test_a_reply_that_is_not_json_is_sent_back_under_the_provider_strategy(
    schema_file,
) -> None:
    model = Scripted(
        replies=[
            AIMessage(content='{"answer": "sunny", "confidence": '),
            AIMessage(content='{"answer": "sunny", "confidence": 0.2}'),
        ]
    )
    strategy = ProviderStrategy({**SCHEMA, "title": ANSWER_TOOL}, strict=True)
    state = await _graph(model, strategy).ainvoke({"messages": [HumanMessage("hi")]})
    assert state["structured_response"] == {"answer": "sunny", "confidence": 0.2}
    assert "not valid JSON" in model.seen[1][-1].content


async def test_no_fitting_try_fails_the_step(schema_file) -> None:
    model = Scripted(replies=[_answer({"answer": "sunny"})])
    with pytest.raises(StructuredAnswerError, match=r"after 3 tries.*'confidence'"):
        await _graph(model, _tool_strategy()).ainvoke({"messages": [HumanMessage("hi")]})
    assert len(model.seen) == 3


async def test_an_answer_beside_other_tool_calls_is_sent_back_and_none_of_them_runs(
    schema_file,
) -> None:
    # LangChain would take the answer and still run the other call after it (a gated
    # one would pause the run with the answer already given): the try goes back.
    ran: list[str] = []

    @tool
    def probe(query: str) -> str:
        """Run the probe for a place."""
        ran.append(query)
        return f"{query}: 42"

    fitting = {"answer": "Oslo is fine", "confidence": 1}
    together = AIMessage(
        content="",
        tool_calls=[
            {"name": ANSWER_TOOL, "args": fitting, "id": "call_answer"},
            {"name": "probe", "args": {"query": "Oslo"}, "id": "call_probe"},
        ],
    )
    alone = AIMessage(
        content="", tool_calls=[{"name": "probe", "args": {"query": "Oslo"}, "id": "call_again"}]
    )
    model = Scripted(replies=[together, alone, _answer({"answer": "Oslo: 42", "confidence": 1})])
    graph = create_agent(
        model=model,
        tools=[probe],
        response_format=_tool_strategy(),
        middleware=[StructuredAnswer()],
    )
    state = await graph.ainvoke({"messages": [HumanMessage("hi")]})
    assert state["structured_response"] == {"answer": "Oslo: 42", "confidence": 1}
    assert ran == ["Oslo"] and len(model.seen) == 3  # the probe ran once: when called alone
    # Every call of the refused try has a result (a provider refuses a history without):
    # the answer is refused, the other call did not run.
    results = {m.tool_call_id: m for m in model.seen[1] if isinstance(m, ToolMessage)}
    assert set(results) == {"call_answer", "call_probe"}
    assert results["call_answer"].status == "error"
    assert "came with other tool calls" in results["call_answer"].content
    assert results["call_probe"].content == structured.OTHER_CALL_NOT_RUN
    # The refused try is not kept in the thread.
    kept = {c["id"] for m in state["messages"] if isinstance(m, AIMessage) for c in m.tool_calls}
    assert kept == {"call_again", "call_10"}


def test_the_middleware_does_nothing_without_a_response_format(schema_file) -> None:
    model = Scripted(replies=[AIMessage(content="plain text")])
    graph = create_agent(model=model, tools=[], middleware=[StructuredAnswer()])
    state = graph.invoke({"messages": [HumanMessage("hi")]})
    assert state["messages"][-1].content == "plain text" and len(model.seen) == 1


# --- the chat runtime's view of a structured run -----------------------------------------


def test_a_structured_run_hides_the_models_text_and_the_answer_tool() -> None:
    state = _RunState(structured_mode=True)
    chunk = {"type": "AIMessageChunk", "content": '{"answer": "sun', "id": "a1"}
    assert list(map_stream_item("messages", [chunk, {}], state)) == []
    # As LangGraph Server sends the updates: JSON, not message objects.
    update = {
        "model": {
            "messages": [
                {
                    "type": "ai",
                    "id": "a2",
                    "content": "",
                    "tool_calls": [
                        {"id": "c1", "name": "probe", "args": {}},
                        {"id": "c2", "name": ANSWER_TOOL, "args": {"answer": "x"}},
                    ],
                }
            ],
            "structured_response": {"answer": "x"},
        }
    }
    events = list(map_stream_item("updates", update, state))
    assert [e for e, _ in events] == [EVENT_TOOL_CALL]
    assert events[0][1]["name"] == "probe" and state.structured == {"answer": "x"}
    result = {"tools": {"messages": [{"type": "tool", "name": ANSWER_TOOL, "tool_call_id": "c2"}]}}
    assert list(map_stream_item("updates", result, state)) == []
    # A later step that gives no answer clears it: the last step's answer counts.
    list(
        map_stream_item("updates", {"model": {"messages": [], "structured_response": None}}, state)
    )
    assert state.structured is None


def test_a_run_without_a_schema_streams_as_before() -> None:
    state = _RunState()
    chunk = {"type": "AIMessageChunk", "content": "sunny", "id": "a1"}
    assert list(map_stream_item("messages", [chunk, {}], state)) == [
        (EVENT_DELTA, {"text": "sunny"})
    ]
    update = {"tools": {"messages": [{"type": "tool", "name": ANSWER_TOOL, "tool_call_id": "c"}]}}
    assert [e for e, _ in map_stream_item("updates", update, state)] == [EVENT_TOOL_RESULT]


def test_a_failed_answer_from_the_server_has_its_own_error_code() -> None:
    runtime = ChatRuntime()
    for exc in (
        StructuredAnswerError("no try fitted"),
        _ServerRunError({"error": "StructuredAnswerError", "message": "no try fitted"}),
    ):
        event = runtime._error_event(exc, "run-1")
        assert event["code"] == CODE_INVALID_STRUCTURED_RESPONSE, event
        assert "did not fit the response schema" in event["message"]
