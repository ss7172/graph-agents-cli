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
  version: "0.2.0"
  requires:
    bins:
      - graph-agents-cli
    install: "uv tool install git+https://github.com/ss7172/graph-agents-cli@v0.2.0"
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
| `references/template-contract.md` | File layout, env contract (every setting and its default), chat SSE API and error events, routes (`/ready`, `/metrics`, `/threads`), request rules, auth policy interface, API client, manifest, exactly as the template implements them |
| `references/langgraph.md` | `create_agent`, `StateGraph`/`MessagesState`, tools, checkpointers and `thread_id`, streaming, interrupts (not wired to `/chat` in this milestone), subgraphs, testing with the `fake` provider |
| `references/langchain-models.md` | `init_chat_model` provider switching, provider env variables, tool-capable open models, the judge configuration |

---

## 1. The graph: `create_agent` first, explicit `StateGraph` when the flow has shape

The scaffolded `app/agent.py`:

```python
from langchain.agents import create_agent
from langgraph.graph.state import CompiledStateGraph

from app.app_utils.api_client import ApiCallError, ApiPolicyError
from app.app_utils.limits import recursion_limit
from app.app_utils.model import get_model
from app.tools import get_tools

SYSTEM_PROMPT = "You are a helpful assistant. ..."


@dataclass
class AgentContext:  # per-run context: who is calling (public attributes only)
    principal_id: str = "anonymous"
    roles: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)


class SurfaceApiErrors(
    AgentMiddleware
):  # ApiPolicyError / ApiCallError -> ToolMessage(status="error")
    ...


graph: CompiledStateGraph = create_agent(
    model=get_model(),
    tools=get_tools(),
    system_prompt=SYSTEM_PROMPT,
    middleware=[SurfaceApiErrors()],
    context_schema=AgentContext,
    name="my-agent",
).with_config({"recursion_limit": recursion_limit()})  # RECURSION_LIMIT, default 25
```

Rules:

- `graph` is **compiled without a checkpointer**. Under `fastapi`, `fast_api_app.py` binds the
  checkpointer chosen by `CHECKPOINTER`; under `langgraph-server` the server binds its own
  persistence. Passing `checkpointer=` here breaks both runtimes.
- Keep the export name `graph`; `langgraph.json` points at `./app/agent.py:graph` and the app
  imports it by that name. Keep the `recursion_limit` config and the `SurfaceApiErrors`
  middleware when you rewrite it: the first stops a looping run, the second turns API refusals
  into tool errors the model can read.
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
    {
        "api": "incidents",
        "method": "GET",
        "operation_id": "getIncident",
        "path": "/incidents/{incident_id}",
    },
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

- **Every** `*.py` under `app/tools/` (subpackages and their `__init__.py` included, the
  top-level `__init__.py` excluded; `_`-prefixed modules included, since `get_tools()` imports
  them too) declares two module-level names:
  `API_CALLS`, a **literal** list of `{"api", "method", "operation_id", "path"}` dicts (`api`
  and `method` required, plus `operation_id` and/or `path`; `[]` when it calls no external API;
  an annotated assignment `API_CALLS: list[...] = [...]` is fine), and `TOOLS`, the list of tool
  objects it contributes. `app/tools/__init__.py` collects `TOOLS` from every module and warns
  about a module without `API_CALLS`.
- `graph-agents-cli lint` runs the CLI's own checker (`dev/policy_check.py`), which reads
  `API_CALLS` **statically with `ast`** (no import, no model SDK loaded), validates
  `api-policy.yaml` with the same strict schema the runtime uses, and checks each entry against
  the named API (`allowed_methods`, `allowed_operations`, `denied_operations`) and, when the API
  names an `openapi:` spec, against that spec (by `operationId`, or by `path` + `method`). It
  reads the one module-level literal only, so a computed (non-literal) `API_CALLS` is invalid,
  and so is anything that binds or changes it elsewhere (`+=`, `.append()`, an item assignment,
  a second or conditional assignment, an import): declare every call in the single literal. A
  leftover `PRODUCT_CALLS` is an error with a rename hint.
- `get_client(name)` fails closed: no `api-policy.yaml`, an invalid one, or an undeclared API
  raises `ApiPolicyError`. `client.request(method, path, operation_id=None, path_params=None,
  params=None, json_body=None, headers=None)` (and `client.get(...)`) refuses, before sending,
  any method or operation outside the policy with `ApiPolicyError`. Policy `path` entries are
  templates (`{param}` matches one segment, for `lint` and the client alike); an entry pinning
  both `operationId` and `path` needs both to match. Denials win and fail closed: a call that
  does not name a field a denial pins is refused by it, so when the API has a denial by
  `operationId` alone, pass `operation_id=` on every call and declare it in `API_CALLS` (or pin
  the denial's `path`). Paths match after decoding percent-encoded unreserved characters and
  ignoring one trailing slash; denials also ignore letter case. Pass `path` as the declared
  template and the values in `path_params`; a concrete path is validated (no dot segments,
  encoded slashes, empty segments, query or fragment). A base URL with a path prefix works (the
  path is joined under it), `pagination.max_page_size` is enforced (every value of the
  parameter, in any letter case and any `params` shape), redirects are never followed.
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
- `CHECKPOINTER=postgres` with `POSTGRES_DSN`: `langgraph-checkpoint-postgres` on one
  health-checked connection pool per process (`DB_POOL_MIN_SIZE` / `DB_POOL_MAX_SIZE`); the app
  runs the schema setup at startup under a Postgres advisory lock (replicas may start together)
  and writes run records to its own `runs` table. This is the deployed default on Kubernetes; the
  chart sets it. `GET /ready` answers 503 while the database does not.
- Under `langgraph-server` the server owns persistence from `DATABASE_URI` and `REDIS_URI`;
  `CHECKPOINTER` is ignored; the app keeps its run records in an `agent_runs` table there.
- **One run per thread:** a second `/chat` on a thread whose run is still in progress gets 409
  `{"code": "thread_busy"}` (a Postgres advisory lock across replicas; it needs session-level
  locks, so no transaction-mode PgBouncer). Clients retry after the run ends.
- `GET /threads` lists the caller's threads; `DELETE /threads/{thread_id}` deletes a thread with
  its checkpoints and run records (owner only). `RETENTION_DAYS=N` purges threads idle for more
  than N days, hourly (0 keeps everything).
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

A failed run ends with an `error` event `{code, message, error_id, run_id}` (`code`:
`run_failed`, `timeout`, `recursion_limit`, `thread_busy`, `unavailable`, `forbidden`); the
detail is only in the server log under `error_id` (and in `detail` under `APP_ENV=dev`). Idle
streams get `: keep-alive` comments every `SSE_HEARTBEAT_S`; a client disconnect cancels the run.

`graph-agents-cli run "prompt" -v` prints every event; use it to confirm a new node or tool emits
what you expect.

## 4a. Guardrails

The app enforces limits you should design for rather than work around: `RUN_TIMEOUT_S` (300; the
run is cancelled with status `timeout`), `MODEL_TIMEOUT_S` (60) and `MODEL_MAX_RETRIES` (2) per
model request, `RECURSION_LIMIT` (25 graph steps), `MAX_REQUEST_BYTES` (413) and the `/chat`
metadata caps (422). A stopped run (timeout, disconnect, error) answers its open tool calls with
an error result, so the thread's next turn is valid. Long tools must finish well inside
`RUN_TIMEOUT_S`, or raise it deliberately in `.env` and the chart values.

## 5. Human-in-the-loop with interrupts (not implemented in this milestone)

LangGraph's `interrupt()` (or `interrupt_before=[...]` at compile time) pauses a thread until it
is resumed with `Command(resume=...)`. **The scaffolded chat API does not expose this yet:**
`message.end` always carries `"status": "ok"` (an `error` event replaces it on failure), there is
no `interrupted` status and no `metadata.resume` request convention, so a graph that interrupts
stalls the `/chat` stream instead of pausing cleanly. Until the template wires it, keep approval
steps out of the served graph (ask before acting via a tool that returns a question, or gate the
action in the client application) and use interrupts only in `playground --graph` (LangGraph Studio) for
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
    provider = os.environ[
        "MODEL_PROVIDER"
    ]  # openai | anthropic | gemini | openai-compatible | fake
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
`get_policy()` selects the implementation from `AUTH_POLICY` (the registry is
`app/policies/__init__.py`). Startup fails closed: an unknown `AUTH_POLICY` never starts, and a
policy whose optional `startup_problems() -> list[str]` returns anything stops the process
outside `APP_ENV=dev`. `SharedBearerPolicy` (default) checks `Authorization: Bearer <API_KEY>` and
returns `Principal(id="shared")`. `JwtPolicy` (`AUTH_POLICY=jwt`) gives each user a principal from
a verified OIDC/JWT bearer token (`AUTH_JWT_*` settings: JWKS URL or PEM key, issuer and audience
required outside dev, an algorithm allow-list without `none`, principal and roles claims with
dotted paths; see `references/template-contract.md`). `CustomPolicy` in `app/policies/custom.py` fails closed with an
`HTTPException(503)` whose `detail` carries the implementation instructions (`require()` also
maps a `NotImplementedError` to 503) until you implement it: validate whatever credential your
callers carry (for example an existing application's session cookie), load roles and
permissions on every request, raise 401 with `WWW-Authenticate` for a missing or invalid
credential and 503 when the issuer is unreachable, and never log the credential. Roles are
matched against `AUTH_READ_ACROSS_ROLES` and `AUTH_ADMIN_ROLES` (who may manage assistants, crons
and the store under `langgraph-server`; empty = nobody). A2A tasks and threads belong to the
principal's `id`, so it must be stable and unique. To let tools call an
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
tokens, tool names, and `principal.hashed_id()` (HMAC-keyed with `PRINCIPAL_HASH_SALT` when set);
`full` adds prompts, completions, tool arguments and results, and the client's `/chat` metadata.
Logs are JSON outside `APP_ENV=dev` (`LOG_FORMAT`, `LOG_LEVEL`) with the request id, run id,
thread id and hashed principal; use `logging.getLogger(__name__)` and never log prompts,
credentials or tool arguments. Do not add ad-hoc exporters or `print` prompts in nodes. See
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
