---
name: graph-agents-cli-langgraph-code
description: >
  This skill should be used when the user wants to "write agent code",
  "build an agent with LangGraph", "add a tool", "add a node to the graph",
  "use a checkpointer", "stream events", "add human-in-the-loop",
  "add a subgraph", "switch the model provider", "implement the auth policy",
  "call an external API from a tool", or needs LangGraph and LangChain
  patterns for a graph-agents-cli project. Covers create_agent and
  StateGraph, tools with the API_CALLS declaration, memory vs postgres
  checkpointers and thread_id, streaming, interrupts, subgraphs,
  init_chat_model provider switching, the fake provider for tests, the auth
  policy adapter, the API client and api-policy.yaml, and telemetry
  opt-in. Do NOT use for scaffolding (graph-agents-cli-scaffold), evaluation
  (graph-agents-cli-eval), or deployment (graph-agents-cli-deploy).
metadata:
  author: graph-agents-cli contributors
  license: Apache-2.0
  version: "0.1.0"
  requires:
    bins:
      - graph-agents-cli
    install: "uv tool install git+https://github.com/ss7172/graph-agents-cli"
---

# LangGraph and LangChain patterns for graph-agents-cli projects

> **Prerequisite:** a scaffolded project (`graph-agents-cli info` succeeds). If not, load
> `/graph-agents-cli-scaffold` first. The template wires the chat API, the auth policy, the
> checkpointer binding, the API client, and telemetry; you write the graph and the tools.

## What you edit and what you leave alone

| Path | Category | Rule |
|---|---|---|
| `app/agent.py` | agent code | yours; exports `graph`, an unbound compiled `StateGraph` |
| `app/tools/**` | agent code | yours; one module per tool or tool group, each with `API_CALLS` |
| `app/policies/**` | agent code | yours; `AuthPolicy` implementations (`custom.py` ships as a fail-closed stub) |
| `app/prompts/**`, `app/graph/**` | agent code (reserved) | yours to create; `upgrade` never touches them |
| `app/fast_api_app.py`, `app/app_utils/**`, `Dockerfile`, `langgraph.json`, workflows, chart templates | scaffolding | template-owned; 3-way merged on upgrade; change only when the user asks and expect merge conflicts later |
| `.env`, `.env.*`, `api-policy.yaml`, `values-*.yaml`, `tests/eval/**` | config | yours; never overwritten by upgrade; never commit `.env` |

**Never change the model in code.** `app/app_utils/model.py` builds the model from
`MODEL_PROVIDER` and `MODEL_NAME` through `init_chat_model`; the agent code calls `get_model()`.

## Reference files

| File | Contents |
|---|---|
| `references/template-contract.md` | File layout, env contract, chat SSE API, auth policy interface, API client, exactly as the template implements them |
| `references/langgraph.md` | `create_agent`, `StateGraph`/`MessagesState`, tools, checkpointers and `thread_id`, streaming, interrupts (not wired to `/chat` in this milestone), subgraphs, testing with the `fake` provider |
| `references/langchain-models.md` | `init_chat_model` provider switching, provider env variables, tool-capable open models, the judge configuration |

---

## 1. The graph: `create_agent` first, explicit `StateGraph` when the flow has shape

The scaffolded `app/agent.py`:

```python
from langchain.agents import create_agent
from langgraph.graph.state import CompiledStateGraph

from app.app_utils.model import get_model
from app.tools import TOOLS

SYSTEM_PROMPT = "You are a helpful assistant."

graph: CompiledStateGraph = create_agent(
    model=get_model(),
    tools=TOOLS,
    system_prompt=SYSTEM_PROMPT,
)
```

Rules:

- `graph` is **compiled without a checkpointer**. Under `fastapi`, `fast_api_app.py` binds the
  checkpointer chosen by `CHECKPOINTER`; under `langgraph-server` the server binds its own
  persistence. Passing `checkpointer=` here breaks both runtimes.
- Keep the export name `graph`; `langgraph.json` points at `./app/agent.py:graph` and the app
  imports it by that name.
- Move to an explicit `StateGraph` when the conversation has fixed stages, branching, a
  human-approval step, or subgraphs. `references/langgraph.md` has the pattern; keep the same
  export and stay unbound.

## 2. Tools and the `API_CALLS` declaration

Tools are plain functions decorated with `@tool` (or a docstring-typed function; `create_agent`
accepts both). A tool that calls an external API **must** go through
`app_utils.api_client.get_client("<api>")` and **must** declare its calls at module level so
`lint` can check them against `api-policy.yaml`:

```python
# app/tools/incidents.py
import json
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from app.app_utils.api_client import get_client

# Static declaration read by `graph-agents-cli lint` (the CLI parses this literal with `ast`;
# the module is never imported by lint). Use [] when the module calls no external API.
API_CALLS: list[dict[str, str]] = [
    {"api": "incidents", "method": "GET", "operation_id": "getIncident",
     "path": "/incidents/{incident_id}"},
]


@tool
async def get_incident(incident_id: str, runtime: ToolRuntime) -> str:
    """Return the incident record for INCIDENT_ID."""
    context: Any = getattr(runtime, "context", None)  # the caller, for auth: forward
    client = get_client("incidents", context=context)
    # Pass the declared template plus path_params: the client encodes the value as one
    # segment and refuses `.`/`..`/slashes, so model input cannot reach another endpoint.
    # Never f-string user or model input into `path`.
    # ApiPolicyError / ApiCallError propagate: agent.py's middleware turns them into a
    # tool error the model reads.
    data = await client.get(
        "/incidents/{incident_id}",
        operation_id="getIncident",
        path_params={"incident_id": incident_id},
    )
    return data if isinstance(data, str) else json.dumps(data)


TOOLS = [get_incident]
```

The convention, as the template implements it (`app/tools/weather.py`, and `app/tools/example_api.py`
when the project declares an API policy):

- **Every** module under `app/tools/` (except `__init__.py`) declares two module-level names:
  `API_CALLS`, a **literal** list of `{"api", "method", "operation_id", "path"}` dicts (`api`
  and `method` required, plus `operation_id` and/or `path`; `[]` when it calls no external API;
  an annotated assignment `API_CALLS: list[...] = [...]` is fine), and `TOOLS`, the list of tool
  objects it contributes. `app/tools/__init__.py` collects `TOOLS` from every module and warns
  about a module without `API_CALLS`.
- `graph-agents-cli lint` runs the CLI's own checker (`dev/policy_check.py`), which reads
  `API_CALLS` **statically with `ast`** (no import, no model SDK loaded), validates
  `api-policy.yaml` with the same strict schema the runtime uses, and checks each entry against
  the named API (`allowed_methods`, `allowed_operations`, `denied_operations`) and, when the API
  names an `openapi:` spec, against that spec (by `operationId`, or by `path` + `method`). A
  computed (non-literal) `API_CALLS` is invalid, and a leftover `PRODUCT_CALLS` is an error with
  a rename hint.
- `get_client(name)` fails closed: no `api-policy.yaml`, an invalid one, or an undeclared API
  raises `ApiPolicyError`. `client.request(method, path, operation_id=None, path_params=None,
  params=None, json_body=None, headers=None)` (and `client.get(...)`) refuses, before sending,
  any method or operation outside the policy with `ApiPolicyError`. Policy `path` entries are
  templates (`{param}` matches one segment, for `lint` and the client alike); an entry pinning
  both `operationId` and `path` needs both to match; denials win. Pass `path` as the declared
  template and the values in `path_params`; a concrete path is validated (no dot segments,
  encoded slashes, empty segments, query or fragment). A base URL with a path prefix works (the
  path is joined under it), `pagination.max_page_size` is enforced, redirects are never followed.
  Let the errors propagate: the scaffolded `agent.py` middleware turns them into a
  `ToolMessage(status="error")` the model can read; never swallow them silently.
- Credentials come from the policy, never from the tool: `auth: bearer` sends the API's
  `token_env`; `auth: forward` sends the calling principal's own
  `attributes["credentials"][<api>]` (set by the auth policy) in `forward_header`
  (`Authorization` by default), and refuses to send when the caller has none; `auth: none`
  sends nothing. `forward` is refused under `langgraph-server` (the server persists run context).
- No generic "call any URL" tool. If a tool needs a new operation, add it to `API_CALLS` **and**
  ask the owner of `api-policy.yaml` to allow it; the policy is a reviewed security boundary.
- Unit-test tools with an `httpx.MockTransport` passed as `get_client(..., transport=...)` (or
  `respx`) and the `fake` model; never against a live API.

## 3. Checkpointers, threads, and run records

- `CHECKPOINTER=memory` (the `.env.example` default): `InMemorySaver`; threads and run records
  live in the process and vanish on restart. No database for local development.
- `CHECKPOINTER=postgres` with `POSTGRES_DSN`: `langgraph-checkpoint-postgres`; the app runs
  `setup()` at startup and writes run records to its own `runs` table. This is the deployed
  default on Kubernetes; the chart sets it.
- Under `langgraph-server` the server owns persistence from `DATABASE_URI` and `REDIS_URI`;
  `CHECKPOINTER` is ignored.
- Continuity is the `thread_id` in the `/chat` request (`config={"configurable": {"thread_id": ...}}`
  inside the app). A missing `thread_id` starts a new thread; the response's `message.start` and
  `message.end` events carry the id back.
- Under a per-user policy (`jwt` or `custom`), thread ownership is enforced in-app under both runtimes (`threads`
  side table under `fastapi`; the thread metadata the app writes at creation under
  `langgraph-server`, because the SDK loopback bypasses the server's own auth filters); a thread
  id alone never crosses a principal boundary. Roles in `AUTH_READ_ACROSS_ROLES` may read other
  principals' threads (without tool arguments under `TRACE_CAPTURE=metadata`) but never continue
  or delete them.
- The agent's database is agent-owned: its own credentials and migrations. Never connect the
  graph to another application's operational database.

## 4. Streaming

The app streams the graph with `graph.astream_events(...)` (or `stream_mode=["messages",
"updates"]`) and maps LangGraph events onto the SSE contract: `message.delta` for text chunks,
`tool.call` and `tool.result` for tool nodes, `message.end` with `usage` and `latency_ms`. Keep
nodes and tools async-friendly; a blocking tool stalls the stream.

`graph-agents-cli run "prompt" -v` prints every event; use it to confirm a new node or tool emits
what you expect.

## 5. Human-in-the-loop with interrupts (not implemented in this milestone)

LangGraph's `interrupt()` (or `interrupt_before=[...]` at compile time) pauses a thread until it
is resumed with `Command(resume=...)`. **The scaffolded chat API does not expose this yet:**
`message.end` always carries `"status": "ok"` (an `error` event replaces it on failure), there is
no `interrupted` status and no `metadata.resume` request convention, so a graph that interrupts
stalls the `/chat` stream instead of pausing cleanly. Until the template wires it, keep approval
steps out of the served graph (ask before acting via a tool that returns a question, or gate the
action in the product) and use interrupts only in `playground --graph` (LangGraph Studio) for
graph debugging. `references/langgraph.md` shows the LangGraph pattern for when the convention is
added.

## 6. Subgraphs

Compile a subgraph and add it as a node of the parent. Share state keys explicitly; a subgraph
with its own schema is wrapped in a function node that maps state in and out. Subgraphs inherit
the parent's checkpointer; do not bind one on the subgraph.

## 7. Model provider switching

`app/app_utils/model.py`:

```python
from langchain.chat_models import init_chat_model

def get_model():
    provider = os.environ["MODEL_PROVIDER"]          # openai | anthropic | gemini | openai-compatible | fake
    name = os.environ["MODEL_NAME"]
    ...
    return init_chat_model(name, model_provider=_LANGCHAIN_PROVIDER[provider], **kwargs)
```

- Switch providers by editing `.env` (`MODEL_PROVIDER`, `MODEL_NAME`, the provider key, and
  `OPENAI_BASE_URL` for `openai-compatible`), never `agent.py`.
- `openai-compatible` needs a tool-capable model (Llama 3.1+, Qwen 2.5+, Mistral families); small
  or old models loop or emit malformed calls.
- The judge is built the same way from `JUDGE_*` and defaults to the agent's configuration.
- Selecting a hosted provider is an **egress decision**: prompts, tool results, and assembled
  context go to that provider. Do not change it on your own.

## 8. The `fake` provider for tests

`MODEL_PROVIDER=fake` returns the template's own `FakeChatModel` (`app/app_utils/model.py`), a
deterministic `BaseChatModel` whose reply depends only on the input, so it is stable across calls
and safe under concurrency; it supports `bind_tools`, streaming and usage metadata. It is
test-only and never offered by `create`. Its replies:

| Input | Reply |
|---|---|
| last message is a tool result | `Here is what I found: <tool result>` |
| a question mentioning "weather" while a `get_weather` tool is bound | a `get_weather(query=<place>)` tool call (`<place>` parsed after "in") |
| a judge prompt (mentions "score" and "json") | `{"score": 5, "explanation": "fake judge: deterministic pass"}` |
| a greeting (`hi`, `hello`, `hey`, `good morning`...) | `Hello! How can I help you today?` |
| anything else | `I am a fake model. I can check the weather. You said: <text>` |

There is no `FAKE_MODEL_RESPONSES` variable and no scripted-response list. Use it in unit tests
and CI for the graph's plumbing (state, tool routing, the SSE mapping, policy enforcement), not
for behaviour. Behaviour belongs in eval (`/graph-agents-cli-eval`); the scaffolded
`basic-dataset.json` is written so every case passes on the fake model. `references/langgraph.md`
shows the fixture. `JUDGE_MODEL_PROVIDER=fake` makes the judge score every metric at the scale
maximum.

## 9. The auth policy adapter

`app/app_utils/auth.py` defines `Principal` and the `AuthPolicy` protocol
(`authenticate(request) -> Principal`, `authorize(principal, action, resource)`), and
`get_policy()` selects the implementation from `AUTH_POLICY`. `SharedBearerPolicy` (default)
checks `Authorization: Bearer <API_KEY>` and returns `Principal(id="shared")`. `JwtPolicy`
(`AUTH_POLICY=jwt`) gives each user a principal from a verified OIDC/JWT bearer token
(`AUTH_JWT_*` settings). `CustomPolicy` in `app/policies/custom.py` fails closed with an
`HTTPException(503)` whose `detail` carries the implementation instructions (`require()` also
maps a `NotImplementedError` to 503) until you implement it: validate whatever credential your
callers carry (for example an existing application's session cookie), load roles and
permissions on every request, honour `AUTH_READ_ACROSS_ROLES`. To let tools call an
`auth: forward` API with the caller's own credential, put it in
`attributes["credentials"][<api>]`; it is the only attribute that may hold a secret
(`Principal.public_attributes()` is what may be persisted, logged or traced). Then set
`auth_policy_implemented: true` in the manifest; `deploy --env staging|prod` refuses until you do.
The same policy object is applied as ASGI middleware under `fastapi` and as the server auth
handler under `langgraph-server` (`langgraph.json` `auth`).

## 10. Telemetry

`app/app_utils/telemetry.py` does nothing unless `TRACING_ENABLED=true`. When enabled with
`LANGSMITH_API_KEY` it traces to LangSmith; without it, over OTLP to
`OTEL_EXPORTER_OTLP_ENDPOINT`. `TRACE_CAPTURE=metadata` (default) records structure, timing,
tokens, tool names, and `principal.hashed_id()`; `full` adds prompts, completions, tool arguments
and results. Do not add ad-hoc exporters or `print` prompts in nodes. See
`/graph-agents-cli-observability`.

---

## Common mistakes

| Mistake | Fix |
|---|---|
| `create_agent(..., checkpointer=InMemorySaver())` in `agent.py` | remove it; the app binds the checkpointer per `CHECKPOINTER` |
| `ChatOpenAI(model="...")` or a hard-coded model in `agent.py` | `get_model()`; the model is `.env` configuration |
| `httpx.get(f"{base}/anything")` inside a tool | `get_client("<api>").request(...)` with an `API_CALLS` entry |
| Tool module without a literal `API_CALLS` (or `TOOLS`) | add the declaration (`[]` when it calls no external API) |
| `API_CALLS` built at runtime (comprehension, function call) | `lint` reads it with `ast` and reports it invalid; write the literal list |
| `interrupt()` in the served graph expecting the client to resume | not wired to `/chat` in this milestone; see section 5 |
| Catching `ApiPolicyError` and returning `""` | return the refusal text so the model can adapt |
| `pytest` asserting on model wording | move it to an eval case |
| Editing `fast_api_app.py` to add a route | ask first; it is scaffolding and will conflict on upgrade; prefer a tool or a node |

## Not covered by this skill

- Scaffold flags, the runtime x checkpointer x target table, upgrade rules: `/graph-agents-cli-scaffold`.
- Dataset schema, expect checks, judge metrics, exit codes: `/graph-agents-cli-eval`.
- Helm values, secrets, GitOps, `infra check`: `/graph-agents-cli-deploy`.
- Trace destinations and capture policy details: `/graph-agents-cli-observability`.
- The LangGraph and LangChain APIs in full: fetch the upstream docs for anything not in
  `references/`.

## Migration note

This skill replaces the ADK code skill of google-agents-cli. ADK `Agent`/`App`, callbacks, session
state, and the Vertex AI model wiring have no equivalent here; the LangGraph graph, tools with
`API_CALLS`, the checkpointer, and `init_chat_model` take their place.
