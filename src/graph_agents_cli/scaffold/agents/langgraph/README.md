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
graph-agents-cli run "What's the weather in San Francisco?"
graph-agents-cli playground           # http://127.0.0.1:8000/playground (APP_ENV=dev)
```

{%- if cookiecutter.auth_policy == 'shared-bearer' %}
`API_KEY` is the shared bearer key every client sends (`Authorization: Bearer ...`); the local
server answers 503 until it is set. `login --write-env` generates one, or run
`python -c "import secrets; print(secrets.token_hex(32))"`.
{%- else %}
Authentication follows `AUTH_POLICY={{cookiecutter.auth_policy}}` (see Authentication below).
{%- endif %}
Local development needs no database: `.env.example` sets `CHECKPOINTER=memory`.

## Layout

```
{{cookiecutter.agent_directory}}/
├── agent.py                 # exports `graph` (compiled LangGraph agent, no checkpointer bound)
├── fast_api_app.py          # exports `app`: the HTTP API below
├── app_utils/               # auth, api_client, chat, threads, db, limits, metrics, middleware, model, telemetry, a2a
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
{%- if cookiecutter.has_api_policy %}
api-policy.yaml              # the external APIs tools may call, and how (enforced at runtime and by lint)
{%- endif %}
Dockerfile                   # {{cookiecutter.runtime}} image (runs as uid 1000)
.env.example                 # the full environment contract, with defaults
graph-agents-cli-manifest.yaml
```

## Commands

| Command | Purpose |
|---|---|
| `graph-agents-cli playground` | Run the app with reload; `--graph` opens LangGraph Studio (bypasses the auth policy) |
| `graph-agents-cli run "prompt" [--mode a2a] [--url URL] [--thread-id ID]` | One-shot chat; `--url` targets a deployed agent with `--header` / `GRAPH_AGENTS_CLI_API_KEY` |
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
| `POST /chat` | `{"thread_id": "optional", "message": "...", "metadata": {}}` with `Accept: text/event-stream`; streams `message.start`, `message.delta`, `tool.call`, `tool.result`, `message.end` (usage, latency, status) or `error`. Omit `thread_id` to start a thread (the server generates a random id); send it to continue one |
| `GET /threads` | The caller's threads, most recent first (`?limit=1..100&offset=`), each with its `owner` hashed; `?scope=all` lists every principal's, for a role in `AUTH_READ_ACROSS_ROLES` only |
| `GET /threads/{id}/messages` | A thread's messages (owner, or a role in `AUTH_READ_ACROSS_ROLES`) |
| `DELETE /threads/{id}` | Delete a thread, its checkpoints, run records and A2A tasks (owner only; 409 while a run is in progress) |
| `GET /health` | Liveness: `{"status": "ok", "runtime", "checkpointer"}` (no auth) |
| `GET /ready` | Readiness: 200 when the database answers within 2 s, else 503 (no auth) |
| `GET /metrics` | Prometheus text (no auth unless `METRICS_TOKEN` is set; `METRICS_ENABLED=false` turns it off) |
| `/a2a/{{cookiecutter.agent_directory}}` | A2A JSON-RPC; card at `/a2a/{{cookiecutter.agent_directory}}/.well-known/agent-card.json` (description `A2A_DESCRIPTION`, version `AGENT_VERSION`); tasks are private to their principal and kept in memory per replica for `A2A_TASK_TTL_S`; `SendMessage` returns the reply as one text part |
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
- **Guardrails:** a run is cancelled after `RUN_TIMEOUT_S` (300); each model request has
  `MODEL_TIMEOUT_S` (60) and `MODEL_MAX_RETRIES` (2); `RECURSION_LIMIT` (25) caps graph steps.
  A client disconnect cancels the run. Idle streams get a keep-alive comment every
  `SSE_HEARTBEAT_S` (15).
- **Limits:** bodies over `MAX_REQUEST_BYTES` get 413; a message over `MAX_MESSAGE_CHARS`
  (32 000) gets 422 on `/chat` and an invalid-params error over A2A; metadata beyond
  `MAX_METADATA_KEYS` / `MAX_METADATA_VALUE_CHARS` gets 422. A 422 never echoes the submitted
  values.
- **Thread ids** are shared by every caller: an id another principal used first is theirs
  (403). Let the server generate ids, or use unguessable ones (UUID4).
- **Errors:** the `error` event is `{"code", "message", "error_id", "run_id"}`; an unhandled error
  answers 500 with an `error_id`. Details are only in the server log under that id. A failed
  tool call's `tool.result` carries an `error_id` and, outside `APP_ENV=dev`, a generic text
  (the error text is for the model only). Every response carries `X-Request-ID`.
- **Retention:** `RETENTION_DAYS=N` deletes threads idle for more than N days (hourly; 0 keeps
  everything).
- **Logging:** JSON lines outside `APP_ENV=dev` (`LOG_FORMAT`, `LOG_LEVEL`) with request id, run
  id, thread id and a hashed principal (HMAC-keyed with `PRINCIPAL_HASH_SALT` when set). Access
  lines drop query strings; outbound API calls are logged by API, method, operation id and path
  template, never their values; warnings are JSON records too.
- **CORS:** off unless `CORS_ALLOW_ORIGINS` lists origins.
- **Database:** one health-checked pool per process (`DB_POOL_MIN_SIZE`, `DB_POOL_MAX_SIZE`).

No inbound rate limiting is built in: configure it at the gateway or ingress (outbound calls
can be limited per API, see below).

**Tool results are untrusted input.** Text a tool returns (a customer's note, an upstream error)
can carry instructions meant to steer the agent into acting on someone else's records.
`agent.py` fences every tool result the model reads (`UntrustedToolResults`) and its prompt
forbids following instructions found there; write tools should also call
`require_user_mentioned(record_id, runtime)` and, under a per-user auth policy,
`require_owner(owner_id, context=runtime.context)` (from `app_utils.api_client`), and
write-capable APIs should authorize the user themselves (`auth: forward`). This lowers the risk
without removing it; human approval of writes is planned.

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
sent, with a reason the model can read. `approval` is reserved for human approval of calls, which is planned:
the key is refused until then. Unknown and repeated keys are errors, so a typo never widens access. Denials
win and hold on the endpoint: a denial pinning a path refuses every call to it whatever `operation_id` the
call gives, and a call that leaves out what a denial knows the operation by is refused by it, so a denial by
`operationId` alone refuses every call without `operation_id` (name it on the call and in `API_CALLS`), but
only knows that label: pin the denial's `path` too. With `openapi`, `lint` also refuses a declared
`operation_id` the spec does not give that method and path. Paths match after decoding percent-encoded
unreserved characters and ignoring one trailing slash; letter case counts for allows and is ignored for
denials. Pass model input as `path_params`
of a declared template, never as part of a concrete path.
{%- if cookiecutter.has_api_policy %}
This project declares {% for api in cookiecutter.apis %}`{{ api.name }}` (`{{ api.base_url_env }}`{% if api.auth == 'bearer' %}, token in `{{ api.token_env }}`{% endif %}){{ ", " if not loop.last else "" }}{% endfor %}.
{%- if cookiecutter.example_api %}
`{{cookiecutter.agent_directory}}/tools/example_api.py` shows the pattern with one call the policy allows
(`{{ cookiecutter.example_api.method }} {{ cookiecutter.example_api.path }}` on `{{ cookiecutter.example_api.api }}`): replace it with your own.
{%- else %}
No example tool was generated: the first API allows no operation the example could make.
{%- endif %}
{%- else %}
No policy is declared yet: add an API with `graph-agents-cli api add` (below).
{%- endif %}
Every `*.py` under `{{cookiecutter.agent_directory}}/tools/` (subpackages included) declares `API_CALLS` as one
module-level literal list; `graph-agents-cli lint` fails on an undeclared or disallowed call (and prints the
`graph-agents-cli api` command that would allow it), and on `API_CALLS` changed anywhere else (`+=`,
`.append()`, a conditional assignment), because it cannot read those calls.

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
| `graph-agents-cli api remove NAME` | Remove an API |

Each command validates the result with the rules the agent enforces, prints a diff (comments and key order
are kept), keeps the manifest (`secrets.keys`), `.env.example` and the chart's `values.yaml` in step, and
writes atomically; `--dry-run` shows the diff only. Widening access (more methods or operations, a lifted
denial, a raised limit) is a reviewed change: `.github/CODEOWNERS` covers `api-policy.yaml`. Narrowing is
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
continue or delete, other principals' threads; roles in `AUTH_ADMIN_ROLES` manage assistants, crons and the
store under langgraph-server (both empty by default). Secrets a principal carries live only in
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
outside dev the pods do not start without it. Only the keys listed under `secrets.keys` in the manifest are
exported from an env file: `{{ cookiecutter.secret_keys | join('`, `') }}` (the token of every `auth: bearer`
API in `api-policy.yaml` included). Add other secrets you use (`METRICS_TOKEN`, `PRINCIPAL_HASH_SALT`) to that
list.

```bash
graph-agents-cli secrets apply --env staging     # from .env.staging; creates the namespace if needed
graph-agents-cli secrets status --env staging    # which keys are present (no values); exit 1 on a missing required key
graph-agents-cli deploy --env staging --restart  # roll the pods after a rotation
```

Secrets are applied with server-side apply; allow-listed keys the env file leaves out are kept. The live
`API_KEY` wins: it changes only when the env file sets another one and `--rotate-api-key` is passed. A missing
`API_KEY` is generated and written to the env file (mode 0600), never printed. In `argocd` and `helm-push`
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

## Coding agents

`{{cookiecutter.agent_guidance_filename}}` is the guide a coding agent reads; its `process:` line names the governing
process document ({{ cookiecutter.process if cookiecutter.process else 'none declared' }}).
Install the graph-agents-cli skills with `graph-agents-cli setup`.
