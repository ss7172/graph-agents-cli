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

"""The deterministic `fake` model calls whichever bound tool a request mentions."""

from __future__ import annotations

from typing import Any, Literal

from langchain.tools import ToolRuntime
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool

from {{cookiecutter.agent_directory}}.app_utils.model import FakeChatModel


@tool
def list_orders(customer: str) -> str:
    """List a customer's orders. Use it for any question about orders."""
    return "[]"


@tool
async def update_order(
    order_id: str,
    quantity: int,
    rush: bool,
    status: Literal["open", "shipped"],
    body: dict[str, Any],
    tags: list[str],
    runtime: ToolRuntime,
    note: str = "",
) -> str:
    """Change an order."""
    return "ok"


def _reply(model: Any, text: str) -> Any:
    return model.invoke([HumanMessage(content=text)])


def test_a_request_mentioning_a_bound_tool_calls_it_with_the_subject() -> None:
    model = FakeChatModel().bind_tools([list_orders, update_order])
    reply = _reply(model, "Which orders are open for alice?")
    [call] = reply.tool_calls
    assert (call["name"], call["args"]) == ("list_orders", {"customer": "alice"})
    # A singular mention counts too, and the whole request is the subject without a marker.
    assert _reply(model, "Show my order history").tool_calls[0]["args"] == {
        "customer": "Show my order history"
    }


def test_every_required_argument_gets_a_value_of_its_type() -> None:
    model = FakeChatModel().bind_tools([update_order])
    call = _reply(model, "Please call update_order for ORD-1").tool_calls[0]
    # Only required arguments; the injected runtime is never one of them.
    assert call["args"] == {
        "order_id": "ORD-1",
        "quantity": 1,
        "rush": False,
        "status": "open",
        "body": {},
        "tags": [],
    }


def test_no_mention_no_call_and_generic_name_words_do_not_count() -> None:
    model = FakeChatModel().bind_tools([list_orders])
    for text in ("hi", "List everything you can", "What can you do?"):
        assert _reply(model, text).tool_calls == [], text
    text = _reply(model, "What can you do?").content
    assert text == (
        "I am a fake model. I can use these tools: list_orders (List a customer's orders). "
        "You said: What can you do?"
    )
    assert _reply(FakeChatModel(), "Which orders?").content == (
        "I am a fake model. You said: Which orders?"
    )


def test_after_a_tool_result_it_answers_with_it() -> None:
    model = FakeChatModel().bind_tools([list_orders])
    reply = model.invoke(
        [
            HumanMessage(content="orders for bob"),
            ToolMessage(content="ORD-7 open", tool_call_id="call_list_orders"),
        ]
    )
    assert reply.tool_calls == [] and reply.content == "Here is what I found: ORD-7 open"


def test_streaming_a_tool_call_sends_it_in_one_chunk() -> None:
    model = FakeChatModel().bind_tools([list_orders])
    chunks = list(model.stream([HumanMessage(content="orders for bob")]))
    with_calls = [c for c in chunks if c.tool_call_chunks]
    assert len(with_calls) == 1
    assert with_calls[0].tool_calls == [
        {
            "name": "list_orders",
            "args": {"customer": "bob"},
            "id": "call_list_orders",
            "type": "tool_call",
        }
    ]
