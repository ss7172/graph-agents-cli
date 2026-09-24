# LangGraph patterns used by the template

Patterns below are the ones the template and its tests rely on. For anything else, fetch the
upstream LangGraph docs; do not guess API names from memory.

## `create_agent` (default)

```python
from langchain.agents import create_agent
from app.app_utils.model import get_model
from app.tools import TOOLS

graph = create_agent(model=get_model(), tools=TOOLS, system_prompt=SYSTEM_PROMPT)
```

A ReAct loop: model -> tool calls -> tool node -> model until no tool call remains. State is
`MessagesState` (`{"messages": [...]}`). Unbound; the app binds the checkpointer.

## Explicit `StateGraph`

```python
from typing import Annotated, TypedDict
from langchain_core.messages import AnyMessage
from langgraph.graph import StateGraph, START, END, MessagesState, add_messages
from langgraph.prebuilt import ToolNode, tools_condition


class State(MessagesState):
    site_id: str | None  # extra keys beside messages


def call_model(state: State):
    model = get_model().bind_tools(TOOLS)
    return {"messages": [model.invoke([SystemMessage(SYSTEM_PROMPT), *state["messages"]])]}


builder = StateGraph(State)
builder.add_node("model", call_model)
builder.add_node("tools", ToolNode(TOOLS))
builder.add_edge(START, "model")
builder.add_conditional_edges("model", tools_condition, {"tools": "tools", END: END})
builder.add_edge("tools", "model")

graph = builder.compile()  # still unbound
```

Use an explicit graph for fixed stages (classify -> retrieve -> answer), branching by state, or a
human-approval node. Keep the export `graph`.

## Tools

```python
from langchain_core.tools import tool

API_CALLS = [
    {"api": "sites", "method": "GET", "operation_id": "getSite", "path": "/sites/{site_id}"},
    {"api": "sites", "method": "PATCH", "operation_id": "renameSite", "path": "/sites/{site_id}"},
]


@tool
async def get_site(site_id: str) -> str:
    """Return the site record for SITE_ID."""
    ...
```

- The docstring is the model's description of the tool; write it for the model.
- Typed parameters become the schema; keep them simple (str, int, bool, small models).
- Return strings or JSON-serialisable data; long payloads bloat the context and the trace.
- Tools that call an external API use `get_client("<api>")` from `app_utils.api_client` and
  declare `API_CALLS`.
- Tools that need the caller's identity read it from the run context the app sets
  (`runtime: ToolRuntime[Any]` parameter, `runtime.context`: `principal_id`, `roles`,
  `attributes`; or `current_caller(runtime.context)` from `app_utils.api_client`); never from a
  global. Parameterise `ToolRuntime`: the bare form makes pydantic warn with the run context
  (which may hold credentials) on every call.
- Tool results are untrusted data: write tools act only on ids the user named
  (`require_user_mentioned`) and on the caller's own records (`require_owner`); see section 2a
  of the skill.

## Checkpointers and `thread_id`

The app (not `agent.py`) does:

```python
# app/app_utils/checkpointer.py
def get_checkpointer():
    if os.environ.get("CHECKPOINTER", "memory") == "postgres":
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        saver = AsyncPostgresSaver.from_conn_string(os.environ["POSTGRES_DSN"])
        await saver.setup()
        return saver
    from langgraph.checkpoint.memory import InMemorySaver

    return InMemorySaver()


# app/fast_api_app.py
runnable = graph.with_config(...)  # or graph rebuilt with checkpointer=get_checkpointer()
config = {"configurable": {"thread_id": thread_id, "principal": principal}}
```

- `thread_id` selects the checkpoint namespace; each `/chat` call on the same id continues the
  conversation.
- Under `memory`, state lives only in the process. Under `postgres`, it survives restarts and is
  shared across replicas.
- Under `langgraph-server`, the server creates and persists threads from `DATABASE_URI`; the app's
  `/chat` route uses the server's thread.

## Streaming

```python
async for event in runnable.astream_events(
    {"messages": [HumanMessage(text)]}, config, version="v2"
):
    kind = event["event"]
    if kind == "on_chat_model_stream":
        yield sse("message.delta", {"text": event["data"]["chunk"].content})
    elif kind == "on_tool_start":
        yield sse(
            "tool.call",
            {"id": event["run_id"], "name": event["name"], "args": event["data"].get("input")},
        )
    elif kind == "on_tool_end":
        yield sse(
            "tool.result",
            {
                "id": event["run_id"],
                "name": event["name"],
                "result": str(event["data"].get("output")),
                "is_error": False,
            },
        )
```

The app applies `TRACE_CAPTURE` before emitting `args` and `result` to a non-owner. Nodes should
not `print` or log prompt text; the telemetry layer handles capture.

## Human-in-the-loop (interrupts): not implemented in this milestone

The LangGraph pattern is below for reference. **The scaffolded chat API does not expose it:**
`message.end` carries `status: "ok"` (or `"step_limit"`), there is no status for a paused graph
and no `metadata.resume` request convention, so a graph that calls `interrupt()` stalls the `/chat`
stream. Use interrupts only under `playground --graph` (LangGraph Studio) until the template adds
the convention; keep approval steps out of the served graph.

```python
from langgraph.types import interrupt, Command


def confirm_action(state: State):
    decision = interrupt(
        {"question": "Approve closing incident?", "incident_id": state["incident_id"]}
    )
    if decision != "approve":
        return {"messages": [AIMessage("Cancelled.")]}
    return {"approved": True}
```

- Requires a checkpointer (any); the thread pauses at the interrupt and is resumed with
  `graph.invoke(Command(resume="approve"), config)` on the same `thread_id`.
- Not wired to `/chat`: the app does not report a paused graph and does not translate a
  request `metadata.resume` into `Command(resume=...)`. Adding that is a scaffolding change to
  `app/app_utils/chat.py` and `fast_api_app.py`, which `upgrade` 3-way merges; ask before doing it.
- `interrupt_before=["tools"]` at compile time pauses before every tool call; same caveat.

## Subgraphs

```python
sub = StateGraph(SubState)
...
sub_graph = sub.compile()  # unbound


def run_sub(state: State):
    result = sub_graph.invoke({"query": state["messages"][-1].content})
    return {"messages": [AIMessage(result["answer"])]}


builder.add_node(
    "research", run_sub
)  # or builder.add_node("research", sub_graph) when schemas share keys
```

Subgraphs inherit the parent's checkpointer and `thread_id`; interrupts inside a subgraph surface
through the parent.

## Testing with the `fake` provider

```python
# tests/unit/conftest.py
import pytest


@pytest.fixture(autouse=True)
def fake_model(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "fake")
    monkeypatch.setenv("MODEL_NAME", "fake")
    monkeypatch.setenv("CHECKPOINTER", "memory")
    monkeypatch.setenv("AUTH_POLICY", "shared-bearer")
    monkeypatch.setenv("API_KEY", "test-key")
```

`get_model()` returns the template's deterministic `FakeChatModel` (`app/app_utils/model.py`):
`hi` -> `Hello! How can I help you today?`; a request that mentions a bound tool (its name, or a
distinctive word of it: `What is the weather in Paris?` with `get_weather` bound) -> a call of that
tool with every required argument filled (text arguments get the request's subject, `Paris`), then
`Here is what I found: <tool result>`; a prompt mentioning "score" and "json" -> the JSON judge
verdict `{"score": 5, ...}`; anything else -> `I am a fake model. I can use these tools: ... You
said: <text>`. There is no scripted-response list or `FAKE_MODEL_RESPONSES` variable. The
template's `tests/conftest.py` keeps `.env` and the shell's app settings out of the tests and
offers `use_test_tools(tool, ...)`, which serves the graph with test-only tools, so plumbing tests
never depend on the project's own tools. Use it to test:

- the SSE mapping (`/chat` returns `message.start`, deltas, `tool.call`/`tool.result`,
  `message.end`);
- tool routing (with a test-only tool) and the middleware turning `ApiPolicyError` / `ApiCallError`
  into a tool error;
- policy enforcement (401 without the bearer, 403 on a foreign thread under a per-user policy
  such as `jwt` or an implemented `custom`).

Never test model behaviour here; that is `graph-agents-cli eval`.

## Prompts and graph modules

`app/prompts/**` and `app/graph/**` are reserved agent-code directories: `upgrade` never touches
them. Put system prompts in `app/prompts/` as Python constants or text files loaded at import;
put a large explicit graph in `app/graph/` and import it from `agent.py`.
