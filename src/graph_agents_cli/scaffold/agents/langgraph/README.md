# {{cookiecutter.project_name}}

A LangGraph agent scaffolded by graph-agents-cli.

| Setting | Value |
|---|---|
| Runtime | `{{cookiecutter.runtime}}` |
| Model | `{{cookiecutter.model_provider}}` / `{{cookiecutter.model}}` (env-driven, see `.env.example`) |
| Deployment target | `{{cookiecutter.deployment_target}}` |
| CD mode | `{{cookiecutter.cd}}` |
| Auth policy | `{{cookiecutter.auth_policy}}` |
| Checkpointer (deployed) | `{{cookiecutter.checkpointer}}` |

## Quick start

```bash
cp .env.example .env                  # set {{cookiecutter.provider_key_var}} (or MODEL_PROVIDER=fake to try it without a key)
graph-agents-cli login --write-env    # checks the setup; prompts for missing keys{% if cookiecutter.auth_policy == 'shared-bearer' %}, generates API_KEY{% endif %}
graph-agents-cli install              # uv sync from the committed uv.lock
{%- if cookiecutter.auth_policy == 'jwt' %}
export GRAPH_AGENTS_CLI_API_KEY="$(graph-agents-cli auth dev-token --sub alice --roles user)"
{%- endif %}
graph-agents-cli run "What's the weather in San Francisco?"   # the example tool (tools/weather.py)
graph-agents-cli eval run             # generate traces, grade them, enforce the gate
graph-agents-cli playground           # http://127.0.0.1:8000/playground (APP_ENV=dev)
```

{%- if cookiecutter.auth_policy == 'shared-bearer' %}
`API_KEY` is the shared bearer key every client sends (`Authorization: Bearer ...`); the local
server answers 503 until it is set. `login --write-env` generates one, or run
`python -c "import secrets; print(secrets.token_hex(32))"`. `run` and `eval` send the `API_KEY`
in `.env`; for a deployed agent put its key in `GRAPH_AGENTS_CLI_API_KEY` (kept out of argv and
shell history, unlike `--header`).
{%- elif cookiecutter.auth_policy == 'jwt' %}
Every request needs a JWT the server can verify. For local runs without an identity provider,
`graph-agents-cli auth dev-token --sub <user> [--roles r1,r2]` creates a dev key pair in
`.graph-agents-cli/dev-jwt/` (git ignored), fills the blank `AUTH_JWT_PUBLIC_KEY`,
`AUTH_JWT_ISSUER` and `AUTH_JWT_AUDIENCE` in `.env`, and prints a token; it refuses unless
`APP_ENV=dev`. `run` and `eval` send whatever `GRAPH_AGENTS_CLI_API_KEY` holds as the bearer
token, which keeps it out of argv and shell history (do not pass tokens with `--header`). Mint one
token per test user to exercise thread ownership and roles; restart a kept server
(`graph-agents-cli run --stop-server`) after the first `dev-token`. Deployed environments verify
tokens from your identity provider (`AUTH_JWT_JWKS_URL`, see Authentication below); never deploy
the dev key.
{%- else %}
Authentication follows `AUTH_POLICY={{cookiecutter.auth_policy}}` (see Authentication below): until
`{{cookiecutter.agent_directory}}/policies/custom.py` is implemented every request gets 503. Send
what your policy reads with `run --header 'Name: value'` or `--cookie name=value`.
{%- endif %}
Local development needs no database: `.env.example` sets `CHECKPOINTER=memory`. The example tool
and the eval dataset are starting points: replace or delete `{{cookiecutter.agent_directory}}/tools/weather.py`
and its eval case when you write your own; the tests under `tests/` do not depend on either.

## Layout

```
{{cookiecutter.agent_directory}}/
├── agent.py                 # exports `graph` (compiled LangGraph agent, no checkpointer bound)
├── fast_api_app.py          # exports `app`: the HTTP API below
├── app_utils/               # auth, api_client, approvals, chat, threads, db, limits, metrics, middleware, model, telemetry, a2a
├── policies/                # AuthPolicy implementations (custom.py is a fail-closed stub)
└── tools/                   # every module declares API_CALLS and TOOLS
tests/{unit,integration,eval,load_test}
{%- if cookiecutter.deployment_target == 'kubernetes' %}
deployment/helm/{{cookiecutter.project_name}}/   # chart, values.yaml, values-{dev,staging,prod}.yaml
{%- if cookiecutter.cd == 'argocd' %}
deployment/argocd/           # application-{dev,staging,prod}.yaml
{%- endif %}
{%- endif %}
.github/                     # workflows, agent.env (their settings), CODEOWNERS
langgraph.json               # graph, custom app and auth handler (LangGraph Studio / Server)
api-policy.yaml              # when present: the external APIs tools may call, and how (`api show`)
Dockerfile                   # {{cookiecutter.runtime}} image (runs as uid 1000)
.env.example                 # the full environment contract, with defaults
graph-agents-cli-manifest.yaml
```

## Commands

| Command | Purpose |
|---|---|
| `graph-agents-cli playground` | Run the app with reload; `--graph` opens LangGraph Studio (bypasses the auth policy) |
| `graph-agents-cli run "prompt" [--mode a2a] [--url URL] [--thread-id ID]` | One-shot chat; `--url` targets a deployed agent. A bearer credential goes in `GRAPH_AGENTS_CLI_API_KEY` (locally and with `--url`), never on the command line |
{%- if cookiecutter.auth_policy == 'jwt' %}
| `graph-agents-cli auth dev-token --sub USER [--roles R,...] [--ttl 12h]` | A token for local runs (dev key in `.graph-agents-cli/dev-jwt/`, `APP_ENV=dev` only) |
{%- endif %}
| `uv run pytest` | Unit and integration tests with the deterministic `fake` model and the in-memory checkpointer (`TEST_POSTGRES_DSN` opts the Postgres tests in) |
| `graph-agents-cli eval run` | Generate traces (`artifacts/traces/`) and grade them against the gate in `tests/eval/eval_config.yaml` |
| `graph-agents-cli lint` | ruff plus the API-policy check of every tool's `API_CALLS` |
| `graph-agents-cli api show` / `api check` | The effective outbound API policy and every tool's declared calls / the policy check alone |
| `graph-agents-cli api add\|access\|allow\|deny\|revoke\|limits\|remove ...` | Change `api-policy.yaml` (diff first, `--dry-run` to preview; see Outbound API access) |
| `graph-agents-cli build` | `docker build` with the runtime's Dockerfile |
{%- if cookiecutter.deployment_target == 'kubernetes' %}
| `graph-agents-cli infra check --env <env>` | Read-only report of cluster, GitHub and placeholder prerequisites |
| `graph-agents-cli secrets apply --env <env>` | Create or update the environment's app Secret from the allow-listed keys of `.env.<env>` |
| `graph-agents-cli secrets status --env <env>` | Which keys the Secret holds (never values); exit 1 when a required key is missing |
| `graph-agents-cli deploy --env <env> [--image REF] [--status] [--restart] [--dry-run]` | Deploy per the CD mode (see below) |
{%- endif %}

## The API

| Route | Behaviour |
|---|---|
| `POST /chat` | `{"thread_id": "optional", "message": "...", "metadata": {}}` with `Accept: text/event-stream`; streams `message.start`, `message.delta`, `tool.call`, `tool.result`, `message.end` (usage, latency, status) or `error`. Omit `thread_id` to start a thread (the server generates a random id); send it to continue one. A run that pauses for approval ends with `message.end` status `awaiting_approval` and `approval`; while it waits, a new message gets 409 `{"code": "approval_pending"}` |
| `GET /threads` | The caller's threads, most recent first (`?limit=1..100&offset=`), each with its `owner` hashed; `?scope=all` lists every principal's, for a role in `AUTH_READ_ACROSS_ROLES` only |
| `GET /threads/{id}/messages` | A thread's messages (owner, or a role in `AUTH_READ_ACROSS_ROLES`) |
| `GET /threads/{id}/approvals` | The thread's approvals of gated API calls (owner and read-across roles: all; an approver: the ones it may decide) |
| `POST /threads/{id}/approvals/{approval_id}` | `{"decision": "approve" \| "reject", "comment": "..."}` by an approver; streams the resumed run with the `/chat` events. 403 not an approver, 409 decided already, 410 expired |
| `GET /approvals` | Approvals across threads: the caller's own and the ones a role of theirs may decide (`?status=pending\|approved\|rejected\|expired&limit=&offset=`) |
| `DELETE /threads/{id}` | Delete a thread, its checkpoints, run records, approvals and A2A tasks (owner only; 409 while a run is in progress) |
| `GET /health` | Liveness: `{"status": "ok", "runtime", "checkpointer"}` (no auth) |
| `GET /ready` | Readiness: 200 when the database is set up and answers within 2 s, else 503 (no auth) |
| `GET /metrics` | Prometheus text (no auth unless `METRICS_TOKEN` is set; `METRICS_ENABLED=false` turns it off) |
| `/a2a/{{cookiecutter.agent_directory}}` | A2A JSON-RPC; card at `/a2a/{{cookiecutter.agent_directory}}/.well-known/agent-card.json` (description `A2A_DESCRIPTION`, version `AGENT_VERSION`); tasks are private to their principal and kept in memory per replica for `A2A_TASK_TTL_S` (an unknown task is -32001, A2A 0.3 included); `SendMessage` returns the reply as one text part |
| `/playground`, `/docs`, `/openapi.json` | Only under `APP_ENV=dev` |
{%- if cookiecutter.runtime == 'langgraph-server' %}

Under LangGraph Server these routes are mounted beside the native Assistants/Threads/Runs API
(`langgraph.json` `http.app`) and the same policy is the server's auth handler (`langgraph.json`
`auth`). Thread ids must be UUIDs, and `DELETE /threads/{id}` is the server's own route with the
same owner rule. Assistants, crons and store writes need a role in `AUTH_ADMIN_ROLES`; every
other native action a handler does not allow is denied. The server image disables the server's
unauthenticated `/docs`, `/openapi.json`, `/info` and `/metrics`.
{%- endif %}

Behaviour and its settings (defaults in `.env.example`; a value that does not parse stops the app
at startup):

- **One run per thread:** a second `/chat` on a busy thread gets 409 `{"code": "thread_busy"}`.
  Under postgres the lock is a lease every replica honours: a replica that dies frees its
  threads 30 s later, and a run that can no longer renew its lease stops before it writes.
- **Guardrails:** a run is cancelled after `RUN_TIMEOUT_S` (300); each model request has
  `MODEL_TIMEOUT_S` (60) and `MODEL_MAX_RETRIES` (2). `RECURSION_LIMIT` (50, room for 24
  sequential tool calls) caps graph steps: a run that reaches it ends with a reply saying so
  (`message.end` status `step_limit`) and keeps its work in the thread. A client disconnect
  cancels the run. Idle streams get a keep-alive comment every `SSE_HEARTBEAT_S` (15).
- **Valid history:** a run stopped mid tool call (a timeout, a crash, an outage) leaves a call
  without a result; the next run answers it with an error result right after the call before
  adding its turn, so model providers accept the thread. A tool call whose arguments are not
  valid JSON (some OpenAI-compatible models return them) runs no tool: the agent answers it
  with an error result and asks the model again, at most twice (`AnswerInvalidToolCalls`).
- **Run records:** written as `running` when a run starts and updated when it ends (`ok`,
  `awaiting_approval`, `step_limit`, `error`, `timeout`, `cancelled`, `interrupted`); runs of a
  process that died are marked `interrupted` within about a minute.
- **Limits:** bodies over `MAX_REQUEST_BYTES` get 413; a message over `MAX_MESSAGE_CHARS`
  (32 000) gets 422 on `/chat` and an invalid-params error over A2A; metadata beyond
  `MAX_METADATA_KEYS` / `MAX_METADATA_VALUE_CHARS` gets 422. A 422 never echoes the submitted
  values.
- **Thread ids** are shared by every caller: an id another principal used first is theirs
  (403). Let the server generate ids, or use unguessable ones (UUID4).
- **Errors:** the `error` event is `{"code", "message", "error_id", "run_id"}`; an unhandled error
  answers 500 with an `error_id`. Details are only in the server log under that id. A failed
  tool call's `tool.result` carries an `error_id` and, outside `APP_ENV=dev`, a generic text
  (the error text is for the model only). Every response carries `X-Request-ID`. An
  unreachable database answers 503 within a few seconds and logs one line.
- **Retention:** `RETENTION_DAYS=N` deletes threads idle for more than N days (hourly; 0 keeps
  everything).
{%- if cookiecutter.runtime == 'langgraph-server' %}
- **Logging:** LangGraph Server formats the lines (its `LOG_JSON` and `LOG_LEVEL`). Its access
  lines lose their `query_string` field; outbound API calls are logged by API, method,
  operation id and path template, never their values (`httpx` and `httpcore` log at WARNING
  only); warnings are records too, with the values pydantic echoes redacted.
{%- else %}
- **Logging:** JSON lines outside `APP_ENV=dev` (`LOG_FORMAT`, `LOG_LEVEL`) with request id, run
  id, thread id and a hashed principal (HMAC-keyed with `PRINCIPAL_HASH_SALT` when set). Access
  lines drop query strings; outbound API calls are logged by API, method, operation id and path
  template, never their values (`httpx` and `httpcore` log at WARNING only); warnings are JSON
  records too, with the values pydantic echoes redacted.
{%- endif %}
- **CORS:** off unless `CORS_ALLOW_ORIGINS` lists origins.
- **Database:** one health-checked pool per process (`DB_POOL_MIN_SIZE`, `DB_POOL_MAX_SIZE`);
  connections get `connect_timeout=5` and TCP keepalives unless the DSN sets them. The app
  starts even while Postgres is unreachable (`/ready` 503 until it answers) and is ready again
  seconds after Postgres is.

No inbound rate limiting is built in: configure it at the gateway or ingress (outbound calls
can be limited per API, see below).

**Tool results are untrusted input.** Text a tool returns (a customer's note, an upstream error)
can carry instructions meant to steer the agent into acting on someone else's records.
`agent.py` fences every tool result the model reads (`UntrustedToolResults`) and its prompt
forbids following instructions found there; write tools should also call
`require_user_mentioned(record_id, runtime)` and, under a per-user auth policy,
`require_owner(owner_id, context=runtime.context)` (from `app_utils.api_client`), and
write-capable APIs should authorize the user themselves (`auth: forward`). This lowers the risk
without removing it. The control that holds is a person's approval of each write before it is
sent: gate write methods or operations with `approval` in `api-policy.yaml` (see Human approval of
calls below).

## Model and judge

`MODEL_PROVIDER` / `MODEL_NAME` (and `OPENAI_BASE_URL` for `openai-compatible`) select the agent model
through LangChain's `init_chat_model`; the provider key lives in `{{cookiecutter.provider_key_var}}`. The eval judge
uses `JUDGE_MODEL_PROVIDER`, `JUDGE_MODEL_NAME`, `JUDGE_BASE_URL`, `JUDGE_API_KEY` and defaults to the agent's
values. Selecting a hosted provider sends prompts, tool results and context to that provider: decide what may
leave and publish a privacy notice before connecting one.

## Outbound API access

Tools reach external APIs only through `get_client("<api>")` of
`{{cookiecutter.agent_directory}}/app_utils/api_client.py`, which enforces `api-policy.yaml` and refuses, before
sending, any API, method or operation outside it. It fails closed: without the file every outbound call is
refused. The client sends every method the policy allows (`request()`, or `get`, `post`, `put`, `patch`,
`delete`, `head`, `options`) with a JSON body, query parameters and headers.

Each API declares `base_url_env` (the URL may carry a path prefix), `auth` (`none`, `bearer` with
`token_env`, or `forward`, which sends the caller's own `attributes["credentials"][<api>]`; not available
under langgraph-server, which would persist it), the required `allowed_methods`, and optional
`allowed_operations` / `denied_operations` (an allowed entry pinning both `operationId` and `path` needs
both to match), `openapi`, `timeouts_ms`, `pagination` (`max_page_size` is enforced for every spelling of the
parameter) and `limits`: `max_calls_per_run` (calls to that API within one agent run) and
`rate_per_minute` (a token bucket per process, so per replica). A call over a limit is refused before it is
sent, with a reason the model can read. `approval` names the calls a person must approve before they are
sent (see Human approval of calls). Unknown and repeated keys are errors, so a typo never widens access. Denials
win and hold on the endpoint: a denial pinning a path refuses every call to it whatever `operation_id` the
call gives, and a call that leaves out what a denial knows the operation by is refused by it, so a denial by
`operationId` alone refuses every call without `operation_id` (name it on the call and in `API_CALLS`), but
only knows that label: pin the denial's `path` too. With `openapi`, `lint` also refuses a declared
`operation_id` the spec does not give that method and path. Paths match after decoding percent-encoded
unreserved characters and ignoring one trailing slash; letter case counts for allows and is ignored for
denials and approval gates, which also cover a literal segment's dot-suffixed spellings (a denial of
`/orders/{order_id}/cancel` refuses `/orders/7/cancel.json` and `cancel.`, which servers that route format
suffixes or drop a trailing dot send to the same endpoint). A `;` in a request path is refused. Pass model
input as `path_params` of a declared template, never as part of a concrete path.
`graph-agents-cli api show` lists the APIs `api-policy.yaml` declares now, what each allows, and every
tool's declared calls; without the file, declare the first API with `graph-agents-cli api add` (below). When
`{{cookiecutter.agent_directory}}/tools/example_api.py` exists (a project created with a policy), it shows
the pattern with one call the policy allows: replace it with your own.
Every `*.py` under `{{cookiecutter.agent_directory}}/tools/` (subpackages included) declares `API_CALLS` as one
module-level literal list; `graph-agents-cli lint` fails on an undeclared or disallowed call (and prints the
`graph-agents-cli api` command that would allow it), and on `API_CALLS` changed anywhere else (`+=`,
`.append()`, a conditional assignment), because it cannot read those calls.

### Human approval of calls

An API's `approval` block holds the calls it names until a person approves them. It is the control for
write actions and for instructions planted in data the agent reads: whatever the model was talked into, the
request waits for someone who sees exactly what it does.

```yaml
    approval:
      required_for:              # at least one of:
        methods: [POST, PATCH, PUT, DELETE]      # these methods ("*": every one), and/or
        operations:                              # entries shaped like allowed_operations
          - operationId: cancelOrder
            path: /orders/{order_id}/cancel
      approvers: [requester]     # "requester" and/or "role:<name>"
      timeout_s: 900             # 30..86400 (default 900); unanswered in time = rejected
```

- **What it gates.** A call is gated when its method is in `required_for.methods` or an entry of
  `required_for.operations` covers it (a `path` gates every call to that path whatever `operation_id` it
  names). Approval never widens access: a gated call must still pass `allowed_methods`, the allowed and
  denied operations and the limits, and a denial still wins. Gate the writes that matter (every write
  method, or the operations that act on other people's records); reads usually need no gate.
- **Who approves.** `requester` lets the principal who started the run (the thread's owner) confirm it;
  `role:<name>` lets any principal holding that role decide, never the requester itself (four eyes), unless
  `requester` is listed too. For example `approvers: [requester]` asks the user to confirm each order
  change; `approvers: ["role:support-lead"]` makes a second person approve every refund. Deciding also needs
  the auth policy's `approval.decide` action. With `shared-bearer` every caller is the one principal
  `shared` (the requester of every run), so `role:` approvers need a per-user policy (`jwt` or `custom`).
- **How it runs.** The run pauses before anything is sent and ends its stream with `message.end` status
  `awaiting_approval` and `approval`: `approval_id`, `api`, `method`, `path`, `query`, `body`,
  `operation_id`, `tool`, `reason` (the tool and the text the model wrote with the call), `approvers`,
  `expires_at` (`approvals` lists every one when parallel calls wait). The thread takes no new message
  meanwhile (409 `approval_pending`). An approver lists it (`GET /threads/{id}/approvals`, or
  `GET /approvals?status=pending` across threads) and decides it
  (`POST /threads/{id}/approvals/{approval_id}`); the run resumes, acting as the requester, and streams
  as `/chat` does, to the decider (a `role:` approver sees that tool result and reply). A2A clients see the task move to `input-required` with the approval in a data part and
  answer on the same task with a data part `{"approval_id": "...", "decision": "approve"}` (the same checks,
  `approval.decide` included; a task belongs to its principal, so only the requester decides there). The
  playground (`APP_ENV=dev`) shows Approve and Reject buttons. `graph-agents-cli run` and
  `graph-agents-cli approvals list|approve|reject` do the same from the command line.
- **Bound and single use.** The approval covers the exact request: API, method, URL with the rendered path,
  query, JSON body, operation id and the tool's own headers (a SHA-256 of them). On resume the tool runs
  again and the client sends the request only when it is the same one, then marks the approval used, so it is
  sent once and never replayed. A decision is bound to the call it was taken for (its API, method and
  path), not to the policy of the moment: a rejected or expired call is never sent, even if a new policy no
  longer gates it; an approved one is sent only while the policy still allows it (a later denial or a
  narrower `allowed_methods`/`allowed_operations` refuses it) and still gates it with the same approvers;
  and a call still waiting when another call's decision resumes the run waits on for its own approval.
  Anything else sends nothing and the tool gets an error saying why, which the model relays. A pending
  approval that expires is closed on the next decision or message on the thread; one a failed or
  cancelled run leaves behind is expired when that run ends.
- **Writing a tool that makes a gated call.** Make at most one gated call per tool call: on resume the tool
  runs again from its start, so a second gated call in the same tool call is refused after the first was
  sent, and anything the tool does before the gated call runs again (keep other side effects after it).
  `client.request(..., redact=["card_number"])` masks fields the approver need not read (the request is
  bound as sent). An `auth: forward` call approved by someone other than the requester is not sent: the
  requester's credential is never stored.
- **Records.** Approvals live in the app's database (table `approvals`{% if cookiecutter.runtime == 'langgraph-server' %}; `agent_approvals` in the server's `DATABASE_URI` database{% endif %}): the call,
  the requester and decider hashed, the decision, its comment and time, and when it was used. Once decided
  or expired, the query and body are dropped unless `TRACE_CAPTURE=full`. Deleting a thread deletes its
  approvals. `/metrics` counts `agent_approvals_total{event="requested|approved|rejected|expired"}`.
{%- if cookiecutter.runtime == 'langgraph-server' %}
- **LangGraph Server.** The run pauses and resumes through the server's own interrupt and resume. The
  server's auth handler refuses a run that carries a `command` (a resume) from outside the app, and the
  client sends only an approval the app recorded as approved, so the native API cannot skip the decision.
  A new native run on a thread whose approval is pending gets 409, as `/chat` does.
  Resuming needs the in-process loopback (`LANGGRAPH_SERVER_URL` unset, the default).
{%- endif %}

### Changing the policy

`api-policy.yaml` belongs to this project and evolves with the agent; `scaffold upgrade` and `enhance` never
touch it. There is no default access level: every API lists its methods explicitly.

| Command | Change |
|---|---|
| `graph-agents-cli api add NAME --base-url-env ENV --auth none\|bearer\|forward [--token-env ENV] --access read-only\|read-write\|custom [--methods M,...] [--openapi SPEC] [--max-calls-per-run N] [--rate-per-minute N]` | Declare an API; `--access` is required. read-only = GET, HEAD; read-write = GET, HEAD, POST, PUT, PATCH, DELETE; custom = `--methods` |
| `graph-agents-cli api access NAME read-only\|read-write\|custom [--methods M,...]` | Set the allowed methods |
| `graph-agents-cli api allow NAME OPERATION_ID [--method M --path P]` (or `--method M --path P`) | Add an `allowed_operations` entry pinning every field given (with `openapi`, the id must exist and its method and path are filled in). Creating the list narrows access to the listed operations: the command says so |
| `graph-agents-cli api deny NAME OPERATION_ID [--method M --path P]` (or `--method M --path P`) | Add a `denied_operations` entry (pin the path: it then holds whatever `operation_id` a call gives) |
| `graph-agents-cli api revoke NAME OPERATION_ID [--from allowed\|denied]` | Remove matching entries |
| `graph-agents-cli api limits NAME [--max-calls-per-run N\|none] [--rate-per-minute N\|none]` | Set or clear limits |
| `graph-agents-cli api approval NAME [--methods M,...] [--operations OP,...] [--approvers requester,role:R] [--timeout-s N] [--remove] [--dry-run]` | Set or remove the API's `approval` gate (`--approvers` is required for a new one) |
| `graph-agents-cli api remove NAME` | Remove an API |

Each command validates the result with the rules the agent enforces, prints a diff (comments and key order
are kept), keeps the manifest (`secrets.keys`), `.env.example` and the chart's `values.yaml` in step, and
writes atomically; `--dry-run` shows the diff only. Widening access (more methods or operations, a lifted
denial, a raised limit, a removed or loosened `approval` gate) is a reviewed change: `.github/CODEOWNERS`
covers `api-policy.yaml`. Narrowing is
always safe, and the runtime keeps refusing anything outside the policy even if a tool declares otherwise.

Adding functionality to a working agent, for example letting it update orders:

1. Change the policy, reviewing each printed diff (`--dry-run` first). If `orders` has no
   `allowed_operations` yet, every operation within its methods is allowed: first
   `graph-agents-cli api allow orders <operation> --method M --path P` for each operation the agent already
   calls, because the first `allow` creates the list and every call not on it is refused from then on (the
   command names the declared calls that become refused). Then
   `graph-agents-cli api allow orders updateOrder --method PATCH --path /orders/{order_id}`,
   and `graph-agents-cli api access orders custom --methods <the current methods>,PATCH` if PATCH is not
   allowed yet. With the list in place the new method reaches only the listed operations; `api access`
   without a list would allow every PATCH operation of the API.
2. Write the tool with `{"api": "orders", "method": "PATCH", "operation_id": "updateOrder", "path": ...}` in
   `API_CALLS`, calling `get_client("orders")`.
3. `graph-agents-cli api check` (or `lint`), then add eval cases in `tests/eval/datasets/` and run
   `graph-agents-cli eval run`.
4. Open a pull request: CODEOWNERS approves the policy change.
5. Build and deploy: the policy is baked into the image, so what passed staging is exactly what reaches
   production; only base URLs (the chart's `env`) and tokens (the Secret) differ per environment.

## Authentication

One policy (`AUTH_POLICY`) guards `/chat`, the thread routes and A2A{% if cookiecutter.runtime == 'langgraph-server' %}, and the server's native API{% endif %}.
An unknown policy never starts, and a misconfigured one stops the app outside `APP_ENV=dev` (under dev
requests get 503 and the problem is logged).

- `shared-bearer` (default): `Authorization: Bearer <API_KEY>`, compared in constant time; every caller is the
  same principal, so use it for trusted callers only.
- `jwt`: each user gets their own principal from a verified OIDC/JWT bearer token. Set
  `AUTH_JWT_JWKS_URL` (https outside dev) or `AUTH_JWT_PUBLIC_KEY`, `AUTH_JWT_ISSUER` and
  `AUTH_JWT_AUDIENCE` (both required outside dev); optionally `AUTH_JWT_ALGORITHMS` (default `RS256,ES256`;
  HS* only with `AUTH_JWT_ALLOW_HS=true` and a 32-byte `AUTH_JWT_SECRET` in the Secret),
  `AUTH_JWT_PRINCIPAL_CLAIM` (`sub`), `AUTH_JWT_ROLES_CLAIM` (`roles`; dotted paths such as
  `realm_access.roles`), `AUTH_JWT_LEEWAY_S` (60), `AUTH_JWT_JWKS_CACHE_S` (300). A missing or invalid token
  gets 401 with a `WWW-Authenticate` challenge; unreachable issuer keys (after a 1 hour grace) get 503.
- `custom`: a fail-closed stub in `{{cookiecutter.agent_directory}}/policies/custom.py` for anything else (for
  example an existing application's session cookie). Implement `authenticate` (return a `Principal` with a
  stable `id` and its `roles`; 401 when the credential is missing or invalid, 503 when the issuer is
  unreachable), `authorize`, and optionally `startup_problems()`, then set `auth_policy_implemented: true` in
  the manifest (`deploy --env staging|prod` refuses until then).

Thread and A2A task ownership is enforced per principal. Roles in `AUTH_READ_ACROSS_ROLES` may read, never
continue or delete, other principals' threads (and list their approvals, never decide them); roles in
`AUTH_ADMIN_ROLES` manage assistants, crons and the store under langgraph-server (both empty by default).
Listing and deciding approvals are the actions `approval.read` and `approval.decide`; who may decide a
given call is then its API's `approvers`. Secrets a principal carries live only in
`attributes["credentials"]` and are never persisted, logged or traced.

## Tracing

Off unless `TRACING_ENABLED=true`. With `LANGSMITH_API_KEY` traces go to LangSmith (`LANGSMITH_PROJECT`,
`LANGSMITH_ENDPOINT`); otherwise over OTLP/HTTP to `OTEL_EXPORTER_OTLP_ENDPOINT`. `TRACE_CAPTURE=metadata`
(default) records structure, timing, token counts, tool names, error types and hashed identifiers only;
`TRACE_CAPTURE=full` adds prompts, completions, tool arguments and results, error messages and the client's
`/chat` metadata. Run records (`runs` table under postgres, in-process under memory) follow the same policy;
client metadata is kept in the run record, never in checkpoints.
{%- if cookiecutter.deployment_target == 'kubernetes' %}

## Environments

| Environment | Namespace | Values | Postgres |
|---|---|---|---|
| `dev` | `{{cookiecutter.project_name}}-dev` | `values.yaml` + `values-dev.yaml` | bundled subchart (`postgresql.enabled=true`) |
| `staging` | `{{cookiecutter.project_name}}-staging` | `values.yaml` + `values-staging.yaml` | external, `POSTGRES_DSN`{% if cookiecutter.runtime == 'langgraph-server' %} (`DATABASE_URI` + `REDIS_URI`){% endif %} from the Secret |
| `prod` | `{{cookiecutter.project_name}}-prod` | `values.yaml` + `values-prod.yaml` | external, from the Secret |

The Helm release name is `{{cookiecutter.project_name}}`. Contexts and namespaces are recorded under `environments:`
in `graph-agents-cli-manifest.yaml`. Traffic enters through a Gateway API `HTTPRoute` (set `gateway.parentRef` and
`gateway.hostname` in the values file) or an `Ingress` (`ingress.enabled=true`); only `route.publicPaths`
(`/chat`, `/threads`, `/a2a/{{cookiecutter.agent_directory}}`) are published, so `/health`, `/ready` and
`/metrics` stay inside the cluster. TLS comes from `tls.existingSecret` or cert-manager
(`tls.certManager.enabled=true`). The pod runs as uid 1000 with a read-only root filesystem; readiness uses
`/ready`. `image.tag` is set per deploy (the chart refuses an empty or unquoted numeric tag). Nothing is
installed by the CLI or the chart: `graph-agents-cli infra check --env <env>` reports what the cluster has.

`deploy` and `secrets apply` follow these rules:

- The env file is `--env-file`, else `.env.<env>`; only `dev` falls back to `.env`.
- The kube context is `--context`, else `environments.<env>.context`, else the kubeconfig's current one, which
  outside `dev` needs a confirmation (or `--yes`). Record the staging and prod contexts in the manifest.
- Direct mode checks that the Secret holds every required key before it builds anything (exit 1 otherwise),
  runs `helm upgrade --install --wait --timeout 5m`, and on a failed rollout prints the pods' states, events
  and logs, then rolls back its own revision (`--atomic`, default).
- A workstation build of a tree with uncommitted changes is tagged `<sha>-dirty-<time>`.

## Secrets

The chart never templates the app Secret; it mounts `<release>-app` (`existingSecret`) with `envFrom`, and
outside dev the pods do not start without it. Only the keys listed under `secrets.keys` in
`graph-agents-cli-manifest.yaml` are exported from an env file: the provider key, the database settings, the
policy's own keys and the token of every `auth: bearer` API in `api-policy.yaml` (`api add` and `api remove`
keep that list in step). `graph-agents-cli secrets status --env <env>` shows which of them the Secret holds.
Add other secrets you use (`METRICS_TOKEN`, `PRINCIPAL_HASH_SALT`) to that list.

```bash
graph-agents-cli secrets apply --env staging     # from .env.staging; creates the namespace if needed
graph-agents-cli secrets status --env staging    # which keys are present (no values); exit 1 on a missing required key
graph-agents-cli deploy --env staging --restart  # roll the pods after a rotation
```

Secrets are applied with server-side apply; allow-listed keys the env file leaves out are kept.
{%- if cookiecutter.auth_policy == 'shared-bearer' %} The live
`API_KEY` wins: it changes only when the env file sets another one and `--rotate-api-key` is passed. A missing
`API_KEY` is generated and written to the env file (mode 0600), never printed.
{%- endif %} In `argocd` and `helm-push`
modes CI never holds application secrets: the owner named in the manifest (`secrets.owner`) runs
`secrets apply` from a workstation with cluster access, once per environment.

## Deploying (`cd: {{cookiecutter.cd}}`)
{%- set registry_placeholder = 'CHANGE-ME' in cookiecutter.registry %}
{%- set registry = '<registry>' if registry_placeholder else cookiecutter.registry %}
{%- if registry_placeholder %}

The registry is still the placeholder `{{cookiecutter.registry}}` (`create` had no `--registry` and found no git
`origin` remote). `build` and `deploy` refuse it until you run
`graph-agents-cli scaffold enhance --registry <host>/<org>`, which sets `create_params.registry` in the manifest,
`image.repository` in the chart's `values.yaml` and `IMAGE_REPOSITORY` in `.github/agent.env`.
{%- if cookiecutter.cd != 'argocd' %} A local cluster
gets the image side-loaded, so there any valid name works (for example `--registry localhost/dev`).
{%- endif %}
{%- endif %}
{%- if cookiecutter.cd == 'skip' %}

Direct mode. `graph-agents-cli deploy --env dev` builds the image, loads it into a local cluster (kind, k3d, k3s,
minikube; nothing for Docker Desktop) or pushes it to `{{registry}}` for a remote cluster, applies the
Secret from the allow-listed keys and runs `helm upgrade --install`. `deploy --env staging|prod` works the same way
from a workstation, with the context rules above. Add CD later with
`graph-agents-cli scaffold enhance --cd argocd|helm-push`.
{%- elif cookiecutter.cd == 'argocd' %}

Pull-based. On every push to `main` the `staging` workflow builds and pushes
`{{registry}}/{{cookiecutter.project_name}}:<short sha>` (the tag a workstation `deploy` also uses),
writes the tag into `values-staging.yaml` on a branch built on the latest `main` (closing older staging PRs it
supersedes) and opens a PR with auto-merge; Argo CD reconciles `main` into `{{cookiecutter.project_name}}-staging`.
Production changes only through a PR that touches `values-prod.yaml`: the `promote-to-prod` workflow (GitHub
`production` environment) or a workstation `graph-agents-cli deploy --env prod --image <ref>` opens it, and its
merge (code-owner review, no self-approval, `pr_checks` green) is the single production gate. `deploy` never
runs helm in this mode; `deploy --status` and `--restart` and the `secrets` commands are the only cluster
operations. The production `Application` has no automated sync: sync it in Argo after the merge.
Point `deployment/argocd/application-*.yaml` at this repository (`repoURL`) and apply them to the Argo CD namespace.
{%- elif cookiecutter.cd == 'helm-push' %}

Push-based. On every push to `main` the `staging` workflow builds and pushes
`{{registry}}/{{cookiecutter.project_name}}:<short sha>` (the tag a workstation `deploy` also uses), and a
**self-hosted runner** inside the network runs `graph-agents-cli deploy --env staging --image <ref> --context <ctx> --yes`
with the kubeconfig from the `DEPLOY_KUBECONFIG` secret of the `staging` environment, then verifies the rollout
(`/health`, `/ready`). `promote-to-prod` does the same for production with the `production` environment's secret
and reviewers. From a workstation `deploy` is allowed for `dev` and refused for `staging`/`prod` (even with
`--image`) unless `--force-direct`.
{%- endif %}

## GitHub settings the workflows rely on (not created by the CLI)

These are repository settings a workflow cannot create with the default token; `graph-agents-cli infra check`
reports whether they exist when `gh` is logged in (or `GITHUB_TOKEN` is set).

- Environment `production`: required reviewers (at least one), "prevent self-review" enabled, deployment
  branches restricted to `main`, optional wait timer.
- Environment `staging`: deployment branches restricted to `main`; no reviewers.
- Branch protection on `main` (`argocd` and `helm-push`): pull requests required; required review from code
  owners; "dismiss stale approvals" and "prevent self-approval" enabled; `pr_checks` as a required status
  check; auto-merge allowed for the staging PR.
- `.github/CODEOWNERS` owns the chart and prod values, the workflows, `api-policy.yaml`, `tests/eval/`, the
  extensions and the manifest; replace the `@CHANGE-ME/production-approvers` placeholder.
- `GH_PR_TOKEN` (a fine-grained PAT or GitHub App token with pull-request and contents write access): pull
  requests opened with the workflow token never trigger `pr_checks`.
- Registry credentials: GHCR works with the workflow token; other registries take `REGISTRY_USERNAME` /
  `REGISTRY_PASSWORD` secrets.
- `helm-push`: the `DEPLOY_KUBECONFIG` secret in each of the `staging` and `production` environments (never a
  repository secret) and a self-hosted runner with `kubectl` and `curl`.
- Optional CI model access: the provider key as a repository secret (or the `MODEL_PROVIDER` / `MODEL_NAME`
  repository variables) makes the `pr_checks` eval gate use a real model; tests always run on the `fake` one,
  and on the fake model the gate warns that it is not a quality signal.
{%- endif %}

## Evals

Datasets are JSON files in `tests/eval/datasets/` (`{"cases": [{"id", "messages", "expect", "judge", ...}]}`).
Deterministic `expect` checks and mandatory judge metrics must always pass; only the metrics listed under
`quality_metrics` in `tests/eval/eval_config.yaml` may pass at a rate below 100 percent. `eval run` exits 0 when
the gate is met, 1 on a failure, 2 on an error or missing case, 3 on a configuration error.

- `expect.contains` and `not_contains` ignore case (`case_insensitive: false` for exact case); on a multi-turn case
  the checks read the final turn unless `scope: all_turns`, and the judges see every earlier turn (replies and
  tool results included).
- A gate met on `MODEL_PROVIDER=fake` (or a fake judge) proves the plumbing only, and `eval grade` says so: run it
  on the real provider before trusting it.
- `eval run --url <agent>` sends every case to that agent, whose tools run for real there, writes included: point
  it at an environment whose data you can reset, with a test identity (`GRAPH_AGENTS_CLI_API_KEY`).

## Coding agents

`{{cookiecutter.agent_guidance_filename}}` is the guide a coding agent reads; its `process:` line names the governing
process document ({{ cookiecutter.process if cookiecutter.process else 'none declared' }}).
Install the graph-agents-cli skills with `graph-agents-cli setup`.
