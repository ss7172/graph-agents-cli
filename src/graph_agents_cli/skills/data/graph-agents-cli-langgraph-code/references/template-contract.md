# Template contract

What the `langgraph` template guarantees, as implemented in `app/`. Agent code relies on these;
scaffolding files implement them.

## File layout (kubernetes target, runtime fastapi, cd argocd)

```
<name>/
├── app/
│   ├── agent.py                 # exports `graph` (unbound compiled StateGraph)
│   ├── fast_api_app.py          # exports `app`: /chat SSE, A2A, /health, /ready, /metrics, /threads, /playground (dev only), policy middleware, checkpointer binding
│   ├── app_utils/
│   │   ├── model.py             # get_model(), get_judge_model() via init_chat_model (timeout, retries); FakeChatModel (provider `fake`)
│   │   ├── chat.py              # the single invocation path shared by /chat, A2A and the playground: run lock (409), history repair, timeouts, step limit, error events, run records, retention
│   │   ├── checkpointer.py      # memory | postgres from CHECKPOINTER (fastapi only); one health-checked pool per process, connect/keepalive defaults, lease-fenced saver
│   │   ├── run_locks.py         # one run per thread: in-process locks plus Postgres leases (owner, fencing token, expiry) renewed by a heartbeat
│   │   ├── auth.py              # Principal (hashed_id(), public_attributes()), AuthPolicy, SharedBearerPolicy, JwtPolicy, check_startup(), require(), `auth` for langgraph.json
│   │   ├── threads.py           # thread ownership table (fastapi) / thread metadata (langgraph-server)
│   │   ├── api_client.py        # get_client(), ApiClient, ApiPolicy, ApiPolicyError, ApiCallError; enforces api-policy.yaml
│   │   ├── limits.py            # RUN_TIMEOUT_S, MODEL_*, RECURSION_LIMIT, request and metadata caps, SSE heartbeat, RETENTION_DAYS (a bad value stops startup)
│   │   ├── metrics.py           # Prometheus registry for /metrics (METRICS_TOKEN)
│   │   ├── middleware.py        # request id and JSON logging, body size cap, CORS, auth-error and thread-delete hooks (langgraph-server)
│   │   ├── telemetry.py         # opt-in tracing, capture policy
│   │   ├── db.py                # run records (`runs` table under postgres; `agent_runs` under langgraph-server; `running` until they end, reconciled to `interrupted`), lease table, schema setup under an advisory lock
│   │   ├── content.py           # message content helpers
│   │   ├── playground.py        # the /playground page (APP_ENV=dev only)
│   │   └── a2a.py               # agent card (A2A 1.0 interface only) and JSON-RPC executor bridging the SSE events; tasks per principal, A2A_TASK_TTL_S
│   ├── policies/
│   │   └── custom.py            # CustomPolicy stub (fails closed with HTTPException 503)
│   └── tools/
│       ├── __init__.py          # collects TOOLS from every module; warns on a module without API_CALLS
│       ├── weather.py           # get_weather (API_CALLS = []): an example, yours to replace or delete
│       └── example_api.py       # call_<api>_api: the first operation the policy's first API allows,
│                                #   any method (a JSON `body` for POST/PUT/PATCH); only with a policy
├── tests/
│   ├── conftest.py              # isolation: no .env, no app settings from the shell; `use_test_tools` fixture
│   ├── unit/test_fake_model.py  # the fake model calls whichever bound tool a request mentions
│   ├── unit/test_policy.py      # auth policies (shared-bearer, custom stub, startup checks, aliases)
│   ├── unit/test_jwt_policy.py  # jwt with locally generated keys: JWKS caching and rotation, claims, algorithms, 401/503
│   ├── unit/test_server_auth.py # langgraph-server handlers: owners, read-across, AUTH_ADMIN_ROLES, default deny
│   ├── unit/test_a2a_scoping.py # A2A tasks private per principal, TTL eviction
│   ├── unit/test_api_client.py  # the API client: fail closed, rules, auth modes, path templates, traversal, paging, every method against a local server, limits; every tool's API_CALLS
│   ├── unit/test_limits.py      # the limit settings and their parsing
│   ├── unit/test_logging.py     # JSON logs, request ids, hashed principals
│   ├── unit/test_threads.py     # ownership: owner / read-across role / stranger; tool-args redaction
│   ├── unit/test_telemetry.py   # no error message or stack trace leaves under metadata capture
│   ├── integration/test_server_e2e.py         # fastapi runtime in process (tool events with a test tool, error event, 503 stub, ownership)
│   ├── integration/test_runtime_guardrails.py # 409, timeouts, recursion limit, body and metadata caps, /ready, /metrics
│   ├── integration/test_server_runtime.py     # langgraph-server branch against a fake SDK client
│   ├── integration/test_postgres.py           # opt-in: TEST_POSTGRES_DSN (a server where the user may create databases)
│   ├── integration/test_chart.py              # kubernetes target: helm template/lint of every environment, expectations read from the values files (needs helm)
│   ├── eval/datasets/basic-dataset.json, eval/eval_config.yaml   # judges: {} (built-in rubrics); cases pass on the fake model
│   └── load_test/               # excluded from a plain `pytest`
├── deployment/helm/<name>/      # Chart.yaml, values.yaml, values-{dev,staging,prod}.yaml, templates/, charts/
├── deployment/argocd/           # application-{dev,staging,prod}.yaml (cd = argocd only)
├── .github/workflows/{pr_checks,staging,promote-to-prod}.yaml   # staging/promote only when cd != skip
├── .github/agent.env            # GRAPH_AGENTS_CLI_SPEC (where CI installs the CLI) + chart settings; data, never sourced
├── .github/CODEOWNERS           # deployment/ (not the dev/staging values), .github/, api-policy.yaml, tests/eval/, extensions, manifest
├── langgraph.json               # always generated: graphs, http.app, auth
├── Dockerfile                   # runtime-specific; runs as 1000:1000, read-only root filesystem compatible
├── .dockerignore                # keeps .env, .venv, .git, artifacts, tests, deployment out of the image
├── .env.example                 # full env contract
├── api-policy.yaml              # with --api-policy, or once `graph-agents-cli api add` declares an API
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
| `APP_ENV` | chart / `.env` | exactly `dev` enables `/playground`, `/docs`, the error `detail` and an optional jwt issuer and audience; anything else (`DEV`, ` dev`, unset) is a deployed environment |
| `MODEL_PROVIDER`, `MODEL_NAME` | `.env` / chart | agent model via `init_chat_model` |
| `OPENAI_BASE_URL` | `.env` / chart | only for `openai-compatible` |
| `OPENAI_API_KEY` \| `ANTHROPIC_API_KEY` \| `GOOGLE_API_KEY` \| `MODEL_API_KEY` | Secret | provider key |
| `JUDGE_MODEL_PROVIDER`, `JUDGE_MODEL_NAME`, `JUDGE_BASE_URL` | `.env` | judge; default to the agent's values |
| `JUDGE_API_KEY` | Secret | judge key; defaults to the provider key |
| `CHECKPOINTER` (`memory`\|`postgres`) | `.env`=memory, chart=postgres | fastapi only |
| `POSTGRES_DSN` | Secret (external) or chart env (subchart) | fastapi only |
| `DATABASE_URI`, `REDIS_URI` | Secret (external) or chart env (subchart) | langgraph-server only |
| `AUTH_POLICY` (`shared-bearer`\|`jwt`\|`custom`) | `.env` / chart | an unknown value never starts; `product-session` is read as `custom` (deprecated) |
| `API_KEY` | Secret | shared-bearer key |
| `AUTH_JWT_JWKS_URL` \| `AUTH_JWT_PUBLIC_KEY`, `AUTH_JWT_ISSUER`, `AUTH_JWT_AUDIENCE` | `.env` / chart | jwt only; issuer and audience required outside dev |
| `AUTH_JWT_ALGORITHMS` (`RS256,ES256`), `AUTH_JWT_PRINCIPAL_CLAIM` (`sub`), `AUTH_JWT_ROLES_CLAIM` (`roles`), `AUTH_JWT_LEEWAY_S` (60), `AUTH_JWT_JWKS_CACHE_S` (300), `AUTH_JWT_JWKS_ALLOW_HTTP` (false), `AUTH_JWT_ALLOW_HS` (false) | `.env` / chart | jwt only |
| `AUTH_JWT_SECRET` | Secret | jwt with HS* only (at least 32 bytes) |
| `AUTH_READ_ACROSS_ROLES` | chart | comma-separated roles allowed to read (never write) others' threads; empty default |
| `AUTH_ADMIN_ROLES` | chart | langgraph-server: roles allowed to manage assistants, crons and the store; empty = nobody |
| `AUTH_FORWARD_HEADERS` | `.env` / chart | langgraph-server with `LANGGRAPH_SERVER_URL`: request headers passed to the server's auth handler (default `authorization,cookie`) |
| `RUN_TIMEOUT_S` (300), `MODEL_TIMEOUT_S` (60), `MODEL_MAX_RETRIES` (2), `RECURSION_LIMIT` (50) | `.env` / chart | run guardrails |
| `MAX_REQUEST_BYTES` (1048576), `MAX_METADATA_KEYS` (16), `MAX_METADATA_VALUE_CHARS` (256), `SSE_HEARTBEAT_S` (15) | `.env` / chart | request limits (413 / 422) and SSE keep-alive |
| `MAX_MESSAGE_CHARS` (32000) | `.env` / chart | longest user message on `/chat` (422) and A2A (invalid params, -32602) |
| `RETENTION_DAYS` (0) | `.env` / chart | purge threads idle longer than N days, hourly; 0 keeps everything |
| `LOG_LEVEL` (INFO), `LOG_FORMAT` (`json`, `text` under dev) | `.env` / chart | structured logs with request id, run id, thread id, hashed principal; access lines without query strings, outbound calls by API/method/operation/path template (`httpx` and `httpcore` at WARNING), warnings as records |
| `METRICS_ENABLED` (true) | `.env` / chart | `GET /metrics` |
| `METRICS_TOKEN`, `PRINCIPAL_HASH_SALT` | Secret (add to `secrets.keys`) | bearer token required by `/metrics`; HMAC key of the principal hash |
| `CORS_ALLOW_ORIGINS` | `.env` / chart | comma list; empty = no CORS |
| `DB_POOL_MIN_SIZE` (1), `DB_POOL_MAX_SIZE` (10) | `.env` / chart | connection pool per process |
| `A2A_TASK_TTL_S` (3600) | `.env` / chart | in-memory A2A tasks dropped this long after their last update (0 = until restart; a value that is not a whole number >= 0 stops startup) |
| `APP_URL` | chart (`appUrl` / hostname) / `.env` | public base URL in the A2A agent card; unset = bind address (warned outside dev) |
| `A2A_DESCRIPTION`, `AGENT_VERSION` (0.1.0) | `.env` / chart | the A2A card's description (and its chat skill's) and version; `A2A_NAME` (the agent directory) is its name and mount |
| `<API>_BASE_URL` (each API's `base_url_env`) | `.env` / chart | one per API in `api-policy.yaml` |
| each `auth: bearer` API's `token_env` | Secret | joins `secrets.keys` at create |
| `API_POLICY_PATH` | `.env` | default `./api-policy.yaml` |
| `TRACING_ENABLED` (`true`\|`false`) | `.env`=false | tracing opt-in |
| `TRACE_CAPTURE` (`metadata`\|`full`) | `.env`=metadata | capture policy; any other value stops startup |
| `LANGSMITH_API_KEY` | Secret | |
| `LANGSMITH_PROJECT`, `LANGSMITH_ENDPOINT` | `.env` / chart | project defaults to the project name |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | chart | OTLP fallback |
| `PORT` | chart | default 8000 |

Client side (not in the app): `GRAPH_AGENTS_CLI_API_KEY`, the bearer credential `run` and `eval`
send locally and with `--url` (the `API_KEY`, or a JWT; `graph-agents-cli auth dev-token` mints a
local one for a `jwt` project).

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
| `tool.result` | `{"id": "...", "name": "...", "result": "...", "is_error": false}`; a failed call adds `"error_id"` and, outside `APP_ENV=dev`, its `result` is `"The tool call did not succeed. Reference: <error_id>."` (the error text is for the model only) |
| `message.end` | `{"thread_id": "...", "run_id": "...", "usage": {"input_tokens": n, "output_tokens": n}, "latency_ms": n, "status": "ok\|step_limit\|awaiting_approval"}`; with `awaiting_approval`, also `"approval": {"approval_id", "api", "method", "path", "query", "body", "operation_id", "reason", "approvers", "expires_at"}` |
| `error` | `{"code": "run_failed\|timeout\|recursion_limit\|thread_busy\|unavailable\|forbidden", "message": "...", "error_id": "...", "run_id": "..."}` (plus `detail` only under `APP_ENV=dev`) then the stream closes |

`message.end` has `"status": "ok"`, or `"step_limit"` when the run reached `RECURSION_LIMIT` and
ended with a reply saying so (the reply is the preceding `message.delta`; the run's work stays in
the thread). An `error` event replaces `message.end` on failure (`recursion_limit` only when the
step-limit reply could not be written). `"awaiting_approval"` ends a run paused before a call its
API's `approval` block gates (the `approval` object names the call; body fields on the optional
redact list are masked); it is resumed by deciding the approval (below), not by a new message.
Other graph interrupts have no status and no resume convention. Idle streams get a `: keep-alive` comment every
`SSE_HEARTBEAT_S`. Run records carry `running` while the run is in progress, then `ok`,
`step_limit`, `error`, `timeout`, `cancelled` (a client disconnect) or `interrupted` (the run
lost its lease, or its process died; `/metrics` counts the same final statuses). A run stopped
mid tool call leaves a call without a result: the next run answers it with an error result
right after the call before adding its turn, so the thread stays valid. A tool call whose
arguments are not valid JSON runs no tool: it arrives as `tool.call` with `"args": {}` and an
error `tool.result`, and the model is asked again in the same step (`AnswerInvalidToolCalls`).

Request rules: a second `/chat` on a thread with a run in progress gets 409
`{"code": "thread_busy"}`, and one on a thread whose run awaits an approval 409
`{"code": "approval_pending"}`; a body over `MAX_REQUEST_BYTES` gets 413; a message over
`MAX_MESSAGE_CHARS`, text with an unpaired surrogate, metadata outside the caps (or
with non-scalar values), `NaN`/`Infinity`, or a `thread_id` outside 1-128 characters of
`[A-Za-z0-9_.:-]` (a non-UUID under langgraph-server) gets 422, whose `detail` never echoes the
submitted values (`input`); a missing `thread_id` starts a thread with a random
server-generated id (ids are one namespace: an id another principal used first is theirs, so
client-chosen ids must be unguessable); a database or server outage gets
503 with a reference; an unhandled error gets 500 `{"detail": "Internal server error. Reference:
<id>.", "error_id": ...}`. Every response carries `X-Request-ID`. Client metadata is stored in the
run record, never in checkpoints, and exported to traces only under `TRACE_CAPTURE=full`.

Other routes:

- `GET /health` -> `{"status": "ok", "runtime": "fastapi|langgraph-server", "checkpointer": "memory|postgres"}` (liveness, no auth)
- `GET /ready` -> 200 `{"status": "ready"}` when the database (and run store) answer within 2 s, else 503 `{"status": "not_ready"}` (no auth)
- `GET /metrics` -> Prometheus text (no auth unless `METRICS_TOKEN`; 404 when `METRICS_ENABLED=false`)
- `GET /threads?limit=&offset=&scope=own|all` -> `[{thread_id, owner, created_at, updated_at}]`, most recent first (`thread.list`); `owner` is the hashed principal id; `scope=own` (default) is the caller's threads, a read-across role included; `scope=all` lists every principal's, for a role in `AUTH_READ_ACROSS_ROLES` only (403 otherwise)
- `GET /threads/{thread_id}/messages` -> ordered messages (ownership enforced); a failed tool call's message carries `error_id` and, outside dev, the same generic text as its `tool.result`
- `GET /threads/{thread_id}/approvals` -> the thread's approvals (the thread's owner, its approvers, read-across roles): the call, `reason`, `approvers`, `status` (`pending`, `approved`, `rejected`, `expired`), `expires_at`, decision time and comment
- `GET /approvals?status=&limit=&offset=` -> across threads, newest first: the caller's own approvals, the ones naming one of its roles, and every one for a read-across role (each row carries its `thread_id`)
- `POST /threads/{thread_id}/approvals/{approval_id}` with `{"decision": "approve"|"reject", "comment": "..."}` (`approval.decide`: `requester` is the principal who started the run, `role:<x>` any other principal with role x; a requester decides their own call only when `requester` is listed) -> the resumed run as SSE with the events above; 403 not an approver, 404 unknown, 409 not pending, 410 expired
- `DELETE /threads/{thread_id}` -> 204; the thread, its checkpoints, run records, approvals and A2A tasks (owner only; 409 while a run is in progress). Under langgraph-server it is the server's native route
- `GET /playground`, `/docs`, `/openapi.json` -> only when `APP_ENV=dev`
- A2A: card at `/a2a/<agent_directory>/.well-known/agent-card.json`, JSON-RPC at `/a2a/<agent_directory>`;
  the card advertises only the A2A 1.0 JSON-RPC interface (0.3 clients are served on the same URL
  via compat) and the security scheme of the active auth policy. A message with no text, an
  empty text part, a non-user role or over `MAX_MESSAGE_CHARS` is a JSON-RPC invalid-params
  error (-32602) on both protocol versions, and an unknown task is -32001 on both (no error
  log). `SendMessage` returns the reply as one text part
  of the `response` artifact; streamed, it arrives in chunks, the last with `lastChunk`, and the
  stored task keeps it as one part. A gated run moves the task to `input-required` with the
  approval in a data part; a message on the same task with the data part `{"approval_id": ...,
  "decision": "approve"|"reject"}` resumes it (same approver rules)

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

    def hashed_id(
        self,
    ) -> str: ...  # sha256 (HMAC with PRINCIPAL_HASH_SALT when set), first 16 hex chars
    def public_attributes(self) -> dict: ...  # attributes without "credentials"


class AuthPolicy(Protocol):
    async def authenticate(self, request: Request) -> Principal: ...  # raise HTTPException(401)
    async def authorize(
        self, principal: Principal, action: str, resource: str | None
    ) -> None: ...  # raise HTTPException(403)


ACTIONS = {
    "chat.send",
    "thread.read",
    "thread.list",
    "thread.delete",
    "run.read",
    "a2a.invoke",
    "card.read",
}
```

`get_policy()` returns the instance for `AUTH_POLICY` (the registry is `app/policies/__init__.py`).
`check_startup()` builds it when the app is assembled and when LangGraph Server loads `auth`: an
unknown policy never starts, and a policy whose optional `startup_problems() -> list[str]` reports
a problem stops the process outside `APP_ENV=dev` (under dev the problem is logged and requests
get 503). `SharedBearerPolicy` checks `Authorization: Bearer <API_KEY>` (constant-time compare)
and returns `Principal(id="shared")`; an unset `API_KEY` is 503. `JwtPolicy` (`jwt`) maps a
verified OIDC/JWT bearer token to a per-user principal (401 with an RFC 6750 challenge for a
missing or invalid token, 503 when misconfigured or the issuer's keys are unavailable). `CustomPolicy`
lives in `app/policies/custom.py` and fails closed with an `HTTPException(503)` carrying the
implementation instructions (`require()` also maps a `NotImplementedError` to 503) until
implemented. `Principal.attributes` may hold secrets only under `credentials` (api name ->
credential, forwarded by `auth: forward` APIs); `Principal.public_attributes()` drops them and is
what may be persisted, logged, traced or passed into LangGraph Server run context. Under
`fastapi`, thread ownership is enforced by the app-owned `threads` table check before any
checkpointer access. `auth` (a `langgraph_sdk.Auth`) is built from the same policy for
`langgraph.json`. Clients: a bearer credential in `GRAPH_AGENTS_CLI_API_KEY` (never on the command
line), `--header 'Name: value'` or `--cookie name=value` for what a custom policy reads.

## API client (`app/app_utils/api_client.py`) and `api-policy.yaml`

```yaml
# api-policy.yaml (owned by the project; example values, not defaults). Unknown keys are errors.
apis:
  incidents:                             # [a-z][a-z0-9_]*, at most 32 characters
    base_url_env: INCIDENTS_API_BASE_URL # required; the URL may carry a path prefix
    auth: bearer                         # required: none | bearer | forward
    token_env: INCIDENTS_API_TOKEN       # required iff auth: bearer
    # forward_header: Authorization      # auth: forward only (the default)
    allowed_methods: [GET, POST]         # required, explicit (no default); ["*"] = every method
    allowed_operations:                  # optional; omit = every operation within allowed_methods
      - operationId: getIncident
        path: /incidents/{incident_id}   # both pinned: both must match
      - operationId: acknowledgeIncident
        path: /incidents/{incident_id}/ack
        methods: [POST]
      - path: /sites/{siteId}/topology
        methods: [GET]
    denied_operations:                   # same entry shape; denials win and fail closed
      - operationId: closeIncident
        path: /incidents/{incident_id}/close
    openapi: docs/incidents-openapi.yaml # optional; lint validates declared calls against it
    timeouts_ms: {connect: 2000, read: 5000}
    pagination: {page_size_param: pageSize, max_page_size: 200}   # enforced at runtime
    limits: {max_calls_per_run: 20, rate_per_minute: 120}         # optional; per run / per replica
    approval:                            # optional: calls a human approves before they are sent
      required_for:                      # methods and/or operations (entry shape above)
        methods: [POST]
        operations:
          - operationId: acknowledgeIncident
            path: /incidents/{incident_id}/ack
      approvers: [requester, "role:oncall-lead"]   # requester and/or role:<name>
      timeout_s: 900                     # 30-86400; then the approval expires (rejected)
```

- `get_client(name)` returns a policy-enforcing async client for one declared API. It fails
  closed: no file, an invalid file or an undeclared API raise `ApiPolicyError`; there is no
  unrestricted fallback. `request(method, path, operation_id=None, path_params=None, params=None,
  json_body=None, headers=None)` (and `get`, `head`, `post`, `put`, `patch`, `delete`,
  `options`) sends any allowed method with a JSON body, query parameters and headers, and
  refuses, before sending, any method or operation outside the policy (`ApiPolicyError`, a tool
  error the model can read); configuration or HTTP failures raise `ApiCallError` (a non-2xx
  response sets `status_code` and `body`, the start of the error body with the sent credential
  redacted). An empty response body returns `""`. Tool-supplied `Host`, method-override,
  `X-Forwarded-*`, `Forwarded`, `X-Original-URL`, `X-Rewrite-URL` and hop-by-hop headers are
  dropped, and a `_method` query parameter or top-level JSON body key is refused. Each call is
  logged by API, method, operation id and path template, never its values.
- The caller, for write tools: `current_caller(context)` (fails closed without a principal),
  `require_owner(owner_id, context=..., allow_roles=())` and `require_user_mentioned(value,
  runtime)` raise `ApiPolicyError` (a tool error) for a record that is not the caller's or an id
  the user's latest message does not name.
- `limits` (optional, per API): `max_calls_per_run` counts the calls to that API in one agent
  run (the run id from the LangGraph run's config metadata, else the request's; `get_client(...,
  run_id=...)` names it explicitly; calls outside any run share one count) and
  `rate_per_minute` is a token bucket per process, so per replica. Both are checked just before
  sending and raise `ApiPolicyError`. A run's counters are dropped when a `/chat` or A2A run
  ends (`end_run`), and otherwise (LangGraph Server runs included) after an hour without a
  call; at most 10 000 runs are tracked.
- `approval` (per API): a call `required_for` covers (its method, or an `operations` entry that
  holds like a denial: the entry's path whatever label the call gives, its operationId, or a
  call leaving out what the entry knows it by) pauses the run in the client before sending
  (LangGraph `interrupt()` with the approval; the canonical request is hashed). Approved: the client re-hashes the request it is about to send, refuses
  it (nothing sent) when it differs, and sends it once. Rejected or expired: nothing is sent and
  the tool gets a "not approved" error. A decision is bound to its call (API, method, path) on
  resume, whatever the policy says about gating it by then: a rejected or expired call is never
  sent, an approved one only while the policy still allows it and gates it with the same
  approvers, and a call still pending when another decision resumes the run pauses again for
  its own approval (a call the policy now refuses is refused, and its approval expires when
  the run ends). The ledger binds a decision to its tool call as well (the model message and
  call id, else the task's interrupt): a tool call that runs again with no decision waiting
  (LangGraph Server's own API continuing a paused run without input or replaying it from a
  checkpoint, a copied thread) has a call an approval was asked for refused, whatever the
  policy now says; an approved one is sent only by the run its decision resumed, once. Under
  `langgraph-server` the auth handler also refuses (403) a native run without input or from a
  checkpoint on a thread that has approvals or waits on a gated call, and a copy of a thread
  that has approvals. Only calls the policy allows are gated (approval never widens access). An `approval` key on an operation entry is refused ("not valid on an
  operation entry; gate the operation with apis.<name>.approval.required_for.operations").
  The tool re-runs from its start on resume: keep it idempotent up to the call and the request
  deterministic. Approvals are stored in an `approvals` table beside the checkpoints.
- Matching: an allowed entry pinning several fields needs all of them to match. Denials win
  and hold on the endpoint: a denial covers every call to a path its `path` covers (with its
  `methods`), whatever `operation_id` the call names, and every call naming its
  `operationId`; failing closed, it also covers a call that leaves out what it knows the
  operation by (no `operation_id` against a denial by `operationId` alone, no path in
  `API_CALLS` against a denial pinning a path). With `openapi:`, `lint` refuses a declared
  `operation_id` the spec does not give that method and path. Paths are compared after
  decoding percent-encoded unreserved characters and ignoring one trailing slash; allows are
  case-sensitive, denials and gates are not, and a denial or gate also covers a literal
  segment's dot-suffixed spellings (`cancel.json`, `cancel.`), which servers that route format
  suffixes or drop a trailing dot send to the same endpoint; allows never match that way. A
  segment with a control character or whitespace at either end, also percent-encoded
  (`cancel%20`, `7%00`), is refused in declared paths (`lint`) and in the paths sent. `pagination.max_page_size` applies to every value of the
  parameter, in any letter case. Repeated YAML keys are errors, like unknown keys.
- `auth: bearer` sends `Authorization: Bearer $<token_env>`; `auth: forward` sends the calling
  principal's `attributes["credentials"][<api>]` in `forward_header` (the principal comes from
  the run context, or `get_client(..., context=runtime.context)`) and sends nothing when the
  caller has none; `forward` is refused at create and lint under `langgraph-server`.
- Every `*.py` under `app/tools/` (subpackages included, the top-level `__init__.py` excluded) declares one module-level **literal**
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
- The policy belongs to the project and evolves with it: `create --api-policy` only seeds it,
  `scaffold enhance` and `upgrade` leave it untouched, and `graph-agents-cli api` (`add`,
  `access`, `allow`, `deny`, `revoke`, `limits`, `remove`, `show`, `check`) changes it with a
  diff, keeping comments, the manifest (`api_policy`, `secrets.keys`), `.env.example` and the
  chart values in step. There is no default access level: `api add --access
  read-only|read-write|custom` is required and writes the methods explicitly. Widening access is
  a reviewed change (CODEOWNERS covers `api-policy.yaml`). A project on the retired
  `product-policy.yaml` / `product_api:` format stops `create`, `enhance`, `upgrade` and `lint`
  with migration steps (exit 3).

## Manifest (`graph-agents-cli-manifest.yaml`)

```yaml
name: my-agent
cli_version: 0.2.0
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
