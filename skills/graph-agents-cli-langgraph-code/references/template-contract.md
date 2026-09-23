# Template contract

What the `langgraph` template guarantees, as implemented in `app/`. Agent code relies on these;
scaffolding files implement them.

## File layout (kubernetes target, runtime fastapi, cd argocd)

```
<name>/
├── app/
│   ├── agent.py                 # exports `graph` (unbound compiled StateGraph)
│   ├── fast_api_app.py          # exports `app`: /chat SSE, A2A, /health, /threads, /playground (dev only), policy middleware, checkpointer binding
│   ├── app_utils/
│   │   ├── model.py             # get_model(), get_judge_model() via init_chat_model; FakeChatModel (provider `fake`)
│   │   ├── chat.py              # the single invocation path shared by /chat, A2A and the playground; emits the SSE events
│   │   ├── checkpointer.py      # memory | postgres from CHECKPOINTER (fastapi only)
│   │   ├── auth.py              # Principal (public_attributes()), AuthPolicy, SharedBearerPolicy, JwtPolicy, get_policy(), `auth` for langgraph.json
│   │   ├── threads.py           # thread ownership table (fastapi) / thread metadata (langgraph-server)
│   │   ├── api_client.py        # get_client(), ApiClient, ApiPolicy, ApiPolicyError, ApiCallError; enforces api-policy.yaml
│   │   ├── telemetry.py         # opt-in tracing, capture policy
│   │   ├── db.py                # run records (`runs` table under postgres)
│   │   ├── content.py           # message content helpers
│   │   ├── playground.py        # the /playground page (APP_ENV=dev only)
│   │   └── a2a.py               # agent card (A2A 1.0 interface only) and JSON-RPC executor bridging the SSE events
│   ├── policies/
│   │   └── custom.py            # CustomPolicy stub (fails closed with HTTPException 503)
│   └── tools/
│       ├── __init__.py          # collects TOOLS from every module; warns on a module without API_CALLS
│       ├── weather.py           # get_weather (API_CALLS = [])
│       └── example_api.py       # call_<api>_api: one GET the policy's first API allows (API_CALLS);
│                                #   only with --api-policy, and only when that API allows such a GET
├── tests/
│   ├── unit/test_policy.py      # auth policies (shared-bearer, the jwt placeholder, the custom stub)
│   ├── unit/test_api_client.py  # the API client: fail closed, rules, auth modes, path templates, traversal, paging
│   ├── unit/test_threads.py     # ownership: owner / read-across role / stranger; tool-args redaction
│   ├── unit/test_telemetry.py   # no error message or stack trace leaves under metadata capture
│   ├── integration/test_server_e2e.py       # fastapi runtime in process (error event, 503 stub, ownership)
│   ├── integration/test_server_runtime.py   # langgraph-server branch against a fake SDK client
│   ├── eval/datasets/basic-dataset.json, eval/eval_config.yaml   # judges: {} (built-in rubrics); cases pass on the fake model
│   └── load_test/
├── deployment/helm/<name>/      # Chart.yaml, values.yaml, values-{dev,staging,prod}.yaml, templates/, charts/
├── deployment/argocd/           # application-{dev,staging,prod}.yaml (cd = argocd only)
├── .github/workflows/{pr_checks,staging,promote-to-prod}.yaml   # staging/promote only when cd != skip
├── .github/agent.env            # GRAPH_AGENTS_CLI_SPEC (where CI installs the CLI) + chart settings
├── .github/CODEOWNERS           # values-prod.yaml, application-prod.yaml -> production approvers (cd != skip)
├── langgraph.json               # always generated: graphs, http.app, auth
├── Dockerfile                   # runtime-specific
├── .dockerignore                # keeps .env, .venv, .git, artifacts, tests, deployment out of the image
├── .env.example                 # full env contract
├── api-policy.yaml              # only when --api-policy was given
├── graph-agents-cli-manifest.yaml
├── AGENTS.md | CLAUDE.md | GEMINI.md   # may declare `process:` (default AGENTS.md)
└── pyproject.toml, uv.lock
```

Under `langgraph-server` the same routes are provided by `app/fast_api_app.py` mounted through
`langgraph.json` `"http": {"app": "./app/fast_api_app.py:app"}` beside the native
Assistants/Threads/Runs API, and `"auth": {"path": "./app/app_utils/auth.py:auth"}` registers the
policy as the server's authentication handler. The chart sets `LANGGRAPH_SERVER=1` so the app
detects the mounted runtime. `app/fast_api_app.py` must not use
`from __future__ import annotations`: LangGraph Server executes the file as `user_router_module`
without registering it in `sys.modules`, and pydantic cannot resolve string annotations for such a
module (the `ChatBody` schema is then "not fully defined" and the server's OpenAPI generation
fails at startup). There is no `tools/policy_check.py` in the project; `lint` uses the CLI's own
checker (below).

## Environment contract

Rendered into `.env.example` and the chart's `values.yaml` `env:` map.

| Variable | Set by | Meaning |
|---|---|---|
| `APP_ENV` | chart / `.env` | `dev` enables `/playground`; anything else disables it |
| `MODEL_PROVIDER`, `MODEL_NAME` | `.env` / chart | agent model via `init_chat_model` |
| `OPENAI_BASE_URL` | `.env` / chart | only for `openai-compatible` |
| `OPENAI_API_KEY` \| `ANTHROPIC_API_KEY` \| `GOOGLE_API_KEY` \| `MODEL_API_KEY` | Secret | provider key |
| `JUDGE_MODEL_PROVIDER`, `JUDGE_MODEL_NAME`, `JUDGE_BASE_URL` | `.env` | judge; default to the agent's values |
| `JUDGE_API_KEY` | Secret | judge key; defaults to the provider key |
| `CHECKPOINTER` (`memory`\|`postgres`) | `.env`=memory, chart=postgres | fastapi only |
| `POSTGRES_DSN` | Secret (external) or chart env (subchart) | fastapi only |
| `DATABASE_URI`, `REDIS_URI` | Secret (external) or chart env (subchart) | langgraph-server only |
| `AUTH_POLICY` (`shared-bearer`\|`jwt`\|`custom`) | `.env` / chart | `product-session` is read as `custom` (deprecated) |
| `API_KEY` | Secret | shared-bearer key |
| `AUTH_JWT_JWKS_URL` \| `AUTH_JWT_PUBLIC_KEY`, `AUTH_JWT_ISSUER`, `AUTH_JWT_AUDIENCE` | `.env` / chart | jwt only; issuer and audience required outside dev |
| `AUTH_READ_ACROSS_ROLES` | chart | comma-separated roles allowed to read (never write) others' threads; empty default |
| `AUTH_ADMIN_ROLES` | chart | langgraph-server: roles allowed to manage assistants, crons and the store; empty = nobody |
| `APP_URL` | chart (`appUrl` / hostname) / `.env` | public base URL in the A2A agent card; unset = bind address (warned outside dev) |
| `<API>_BASE_URL` (each API's `base_url_env`) | `.env` / chart | one per API in `api-policy.yaml` |
| each `auth: bearer` API's `token_env` | Secret | joins `secrets.keys` at create |
| `API_POLICY_PATH` | `.env` | default `./api-policy.yaml` |
| `TRACING_ENABLED` (`true`\|`false`) | `.env`=false | tracing opt-in |
| `TRACE_CAPTURE` (`metadata`\|`full`) | `.env`=metadata | capture policy |
| `LANGSMITH_API_KEY` | Secret | |
| `LANGSMITH_PROJECT`, `LANGSMITH_ENDPOINT` | `.env` / chart | project defaults to the project name |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | chart | OTLP fallback |
| `PORT` | chart | default 8000 |

Client side (not in the app): `GRAPH_AGENTS_CLI_API_KEY` for `run --url` / `eval generate --url`.

## Chat API (both runtimes)

`POST /chat` with header `Accept: text/event-stream`.

Request body:

```json
{ "thread_id": "optional-uuid", "message": "user text", "metadata": {} }
```

Response: SSE. Each event is `event: <type>\ndata: <json>\n\n`.

| event | data |
|---|---|
| `message.start` | `{"thread_id": "...", "run_id": "..."}` |
| `message.delta` | `{"text": "..."}` |
| `tool.call` | `{"id": "...", "name": "...", "args": {...}}` (args omitted when `TRACE_CAPTURE=metadata` and the caller is not the owner; always present to the caller) |
| `tool.result` | `{"id": "...", "name": "...", "result": "...", "is_error": false}` |
| `message.end` | `{"thread_id": "...", "run_id": "...", "usage": {"input_tokens": n, "output_tokens": n}, "latency_ms": n, "status": "ok"}` |
| `error` | `{"code": "...", "message": "..."}` then the stream closes |

`"status": "ok"` is the only `message.end` status in this milestone (an `error` event replaces
`message.end` on failure); there is no `interrupted` status and no `metadata.resume` convention.

Other routes:

- `GET /health` -> `{"status": "ok", "runtime": "fastapi|langgraph-server", "checkpointer": "memory|postgres"}`
- `GET /threads/{thread_id}/messages` -> ordered messages (ownership enforced)
- `GET /playground` -> HTML page, only when `APP_ENV=dev`
- A2A: card at `/a2a/<agent_directory>/.well-known/agent-card.json`, JSON-RPC at `/a2a/<agent_directory>`;
  the card advertises only the A2A 1.0 JSON-RPC interface (0.3 clients are served on the same URL
  via compat) and the security scheme of the active auth policy

`eval generate` derives `response`, `tool_calls`, `usage`, `latency_ms`, and `status` from these
events; the A2A executor bridges the same events to task artifacts.

## Auth policy (`app/app_utils/auth.py`)

```python
@dataclass
class Principal:
    id: str
    roles: list[str] = field(default_factory=list)
    permissions: set[str] = field(default_factory=set)
    attributes: dict[str, Any] = field(default_factory=dict)
    def hashed_id(self) -> str: ...   # sha256, first 16 hex chars, used in traces

class AuthPolicy(Protocol):
    async def authenticate(self, request: Request) -> Principal: ...        # raise HTTPException(401)
    async def authorize(self, principal: Principal, action: str, resource: str | None) -> None: ...  # raise HTTPException(403)

ACTIONS = {"chat.send", "thread.read", "thread.list", "thread.delete", "run.read", "a2a.invoke", "card.read"}
```

`get_policy()` returns the instance for `AUTH_POLICY`. `SharedBearerPolicy` checks
`Authorization: Bearer <API_KEY>` (constant-time compare) and returns `Principal(id="shared")`.
`JwtPolicy` (`jwt`) maps a verified OIDC/JWT bearer token to a per-user principal. `CustomPolicy`
lives in `app/policies/custom.py` and fails closed with an `HTTPException(503)` carrying the
implementation instructions (`require()` also maps a `NotImplementedError` to 503) until
implemented. `Principal.attributes` may hold secrets only under `credentials` (api name ->
credential, forwarded by `auth: forward` APIs); `Principal.public_attributes()` drops them and is
what may be persisted, logged, traced or passed into LangGraph Server run context. Under
`fastapi`, thread ownership is enforced by the app-owned `threads` table check before any
checkpointer access. `auth` (a `langgraph_sdk.Auth`) is built from the same policy for
`langgraph.json`. Client flags: `--header 'Authorization: Bearer ...'`, `--cookie name=value`.

## API client (`app/app_utils/api_client.py`) and `api-policy.yaml`

```yaml
# api-policy.yaml (owned by the project; example values, not defaults). Unknown keys are errors.
apis:
  incidents:                             # [a-z][a-z0-9_]*, at most 32 characters
    base_url_env: INCIDENTS_API_BASE_URL # required; the URL may carry a path prefix
    auth: bearer                         # required: none | bearer | forward
    token_env: INCIDENTS_API_TOKEN       # required iff auth: bearer
    # forward_header: Authorization      # auth: forward only (the default)
    allowed_methods: [GET]               # required, non-empty; ["*"] = every method
    allowed_operations:                  # optional; omit = every operation within allowed_methods
      - operationId: getIncident
        path: /incidents/{incident_id}   # both pinned: both must match
      - path: /sites/{siteId}/topology
        methods: [GET]
    denied_operations: []                # same entry shape; denials win and fail closed
    openapi: docs/incidents-openapi.yaml # optional; lint validates declared calls against it
    timeouts_ms: {connect: 2000, read: 5000}
    pagination: {page_size_param: pageSize, max_page_size: 200}   # enforced at runtime
```

- `get_client(name)` returns a policy-enforcing async client for one declared API. It fails
  closed: no file, an invalid file or an undeclared API raise `ApiPolicyError`; there is no
  unrestricted fallback. `request(method, path, operation_id=None, path_params=None, ...)`
  refuses, before sending, any method or operation outside the policy (`ApiPolicyError`, a tool
  error the model can read); configuration or HTTP failures raise `ApiCallError`.
- Matching: an entry pinning several fields needs all of them to match. Denials win and fail
  closed: a call that does not name a field a denial pins (no `operation_id` against an
  `operationId` denial) is refused by it. Paths are compared after decoding percent-encoded
  unreserved characters and ignoring one trailing slash; allows are case-sensitive, denials are
  not. `pagination.max_page_size` applies to every value of the parameter, in any letter case.
  Repeated YAML keys are errors, like unknown keys.
- `auth: bearer` sends `Authorization: Bearer $<token_env>`; `auth: forward` sends the calling
  principal's `attributes["credentials"][<api>]` in `forward_header` (the principal comes from
  the run context, or `get_client(..., context=runtime.context)`) and sends nothing when the
  caller has none; `forward` is refused at create and lint under `langgraph-server`.
- Every module under `app/tools/` declares a module-level **literal**
  `API_CALLS = [{"api": ..., "method": ..., "operation_id": ..., "path": ...}]` (`[]` when it
  calls no external API) and `TOOLS`. `graph-agents-cli lint` runs the CLI's
  `dev/policy_check.py`, which reads those literals with `ast` (no import, no model SDK),
  validates `api-policy.yaml` with the runtime's own schema rules (the two copies are kept
  byte-identical by a CLI test), and checks every call against its API and, when `openapi:` is
  set (resolved relative to the project root), the spec by `operationId` or `path` + `method`
  (a call declared by `operation_id` alone is judged with the spec's path for it).
  `pr_checks` fails on a violation.
- Both Dockerfiles copy `api-policy.yaml` into the image when the project has one. The manifest
  records `api_policy: {policy_file: api-policy.yaml}`; the key is absent without a policy. Any
  other `policy_file` is a config error (exit 3): the agent loads only `api-policy.yaml`.
- The CLI never edits the policy after scaffolding; `scaffold enhance` and `upgrade` leave it
  untouched. A project on the retired `product-policy.yaml` / `product_api:` format stops
  `create`, `enhance`, `upgrade` and `lint` with migration steps (exit 3).

## Manifest (`graph-agents-cli-manifest.yaml`)

```yaml
name: my-agent
cli_version: 0.1.0
agent_directory: app
base_template: langgraph
generated_at: 2026-09-22T00:00:00+00:00
language: python
create_params:
  deployment_target: kubernetes     # kubernetes | none
  runtime: fastapi                  # fastapi | langgraph-server
  model_provider: openai            # openai | anthropic | gemini | openai-compatible
  model: gpt-5-mini
  checkpointer: postgres            # memory | postgres  (deployed default)
  registry: ghcr.io/my-org
  cd: skip                          # argocd | helm-push | skip
  auth_policy: shared-bearer        # shared-bearer | jwt | custom
  auth_policy_implemented: true     # false while the custom stub is in place
  agent_guidance_filename: AGENTS.md
environments:
  dev:     { context: "", namespace: my-agent-dev }
  staging: { context: "", namespace: my-agent-staging }
  prod:    { context: "", namespace: my-agent-prod }
secrets:
  keys: [OPENAI_API_KEY, JUDGE_API_KEY, POSTGRES_DSN, API_KEY, LANGSMITH_API_KEY]
  owner: "platform-team"
api_policy:
  policy_file: api-policy.yaml      # absent when no policy is declared
process: null                       # or a path to a governing process document
```
