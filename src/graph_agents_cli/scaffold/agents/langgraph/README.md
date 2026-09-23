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
cp .env.example .env            # set {{cookiecutter.provider_key_var}}{% if cookiecutter.auth_policy == 'shared-bearer' %} and API_KEY{% endif %}
uv sync                          # from the committed uv.lock
graph-agents-cli playground      # http://127.0.0.1:8000/playground (APP_ENV=dev)
graph-agents-cli run "What's the weather in San Francisco?"
```

{%- if cookiecutter.auth_policy == 'shared-bearer' %}
`API_KEY` is the shared bearer key every client sends (`Authorization: Bearer ...`);
generate one with `python -c "import secrets; print(secrets.token_hex(32))"`.
{%- else %}
Authentication follows `AUTH_POLICY={{cookiecutter.auth_policy}}` (see Authentication below).
{%- endif %}
Local development needs no database: `.env.example` sets `CHECKPOINTER=memory`.

## Layout

```
{{cookiecutter.agent_directory}}/
├── agent.py                 # exports `graph` (compiled LangGraph agent, no checkpointer bound)
├── fast_api_app.py          # exports `app`: POST /chat (SSE), GET /health, /threads/{id}/messages, /playground, A2A
├── app_utils/               # model, checkpointer, db (run records), threads, auth, api_client, telemetry, a2a, chat
├── policies/                # AuthPolicy implementations (custom.py is a fail-closed stub)
└── tools/                   # every module declares API_CALLS and TOOLS
tests/{unit,integration,eval,load_test}
{%- if cookiecutter.deployment_target == 'kubernetes' %}
deployment/helm/{{cookiecutter.project_name}}/   # chart, values.yaml, values-{dev,staging,prod}.yaml
{%- if cookiecutter.cd == 'argocd' %}
deployment/argocd/           # application-{dev,staging,prod}.yaml
{%- endif %}
{%- endif %}
langgraph.json               # graph, custom app and auth handler (LangGraph Studio / Server)
{%- if cookiecutter.has_api_policy %}
api-policy.yaml              # the external APIs tools may call, and how (enforced at runtime and by lint)
{%- endif %}
Dockerfile                   # {{cookiecutter.runtime}} image
.env.example                 # the full environment contract
graph-agents-cli-manifest.yaml
```

## Commands

| Command | Purpose |
|---|---|
| `graph-agents-cli playground` | Run the app with reload; `--graph` opens LangGraph Studio (bypasses the auth policy) |
| `graph-agents-cli run "prompt" [--mode a2a] [--url URL] [--thread-id ID]` | One-shot chat; `--url` targets a deployed agent with `--header` / `GRAPH_AGENTS_CLI_API_KEY` |
| `uv run pytest tests/unit tests/integration` | Tests with the deterministic `fake` model and the in-memory checkpointer |
| `graph-agents-cli eval run` | Generate traces (`artifacts/traces/`) and grade them against the gate in `tests/eval/eval_config.yaml` |
| `graph-agents-cli lint` | ruff plus the API-policy check of every tool's `API_CALLS` |
| `graph-agents-cli build` | `docker build` with the runtime's Dockerfile |
{%- if cookiecutter.deployment_target == 'kubernetes' %}
| `graph-agents-cli infra check --env <env>` | Read-only report of cluster and GitHub prerequisites |
| `graph-agents-cli secrets apply --env <env> [--env-file FILE]` | Create or update the environment's app Secret from the allow-listed keys |
| `graph-agents-cli deploy --env <env> [--image REF] [--status] [--restart] [--dry-run]` | Deploy per the CD mode (see below) |
{%- endif %}

## The API

`POST /chat` with `Accept: text/event-stream` and `{"thread_id": "optional", "message": "...", "metadata": {}}`
streams `message.start`, `message.delta`, `tool.call`, `tool.result`, `message.end` (usage, latency, status)
or `error`. Send the same `thread_id` to continue a conversation. `GET /threads/{id}/messages` returns
the thread (ownership enforced), `GET /health` reports runtime and checkpointer, and the A2A agent card is at
`/a2a/{{cookiecutter.agent_directory}}/.well-known/agent-card.json` with JSON-RPC at `/a2a/{{cookiecutter.agent_directory}}`.
{%- if cookiecutter.runtime == 'langgraph-server' %}
Under LangGraph Server these routes are mounted beside the native Assistants/Threads/Runs API
(`langgraph.json` `http.app`) and the same policy is the server's auth handler (`langgraph.json` `auth`).
{%- endif %}

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
refused. Each API declares `base_url_env` (the URL may carry a path prefix), `auth` (`none`, `bearer` with
`token_env`, or `forward`, which sends the caller's own `attributes["credentials"][<api>]`; not available
under langgraph-server, which would persist it), the required `allowed_methods`, and optional
`allowed_operations` / `denied_operations` (an entry pinning both `operationId` and `path` needs both to
match), `openapi`, `timeouts_ms` and `pagination` (`max_page_size` is enforced for every spelling of the
parameter). Unknown and repeated keys are errors, so a typo never widens access. Denials win and fail closed: a
call that does not name a field a denial pins is refused by it, so a denial by `operationId` alone refuses every
call without `operation_id` (name it on the call and in `API_CALLS`, or pin the denial's `path`). Paths match
after decoding percent-encoded unreserved characters and ignoring one trailing slash; letter case counts for
allows and is ignored for denials. Pass model input as `path_params` of a declared template, never as part of a
concrete path.
{%- if cookiecutter.has_api_policy %}
This project declares {% for api in cookiecutter.apis %}`{{ api.name }}` (`{{ api.base_url_env }}`{% if api.auth == 'bearer' %}, token in `{{ api.token_env }}`{% endif %}){{ ", " if not loop.last else "" }}{% endfor %}.
{%- else %}
No policy is declared yet: seed one with `graph-agents-cli create --api-policy <file>`, or write
`api-policy.yaml` by hand and add `api_policy: {policy_file: api-policy.yaml}` to the manifest.
{%- endif %}
Every tool module declares `API_CALLS`; `graph-agents-cli lint` fails on an undeclared or disallowed call.

## Authentication

`AUTH_POLICY=shared-bearer` (default) checks `Authorization: Bearer <API_KEY>` with a constant-time compare;
every caller is the same principal. `AUTH_POLICY=jwt` gives each user their own principal from a verified
OIDC/JWT bearer token (`AUTH_JWT_JWKS_URL` or `AUTH_JWT_PUBLIC_KEY`, `AUTH_JWT_ISSUER`, `AUTH_JWT_AUDIENCE`;
see `.env.example`). `AUTH_POLICY=custom` ships as a fail-closed stub in
`{{cookiecutter.agent_directory}}/policies/custom.py` for anything else (for example an existing application's
session): implement it, then set `auth_policy_implemented: true` in the manifest. Thread ownership is enforced
per principal; roles listed in `AUTH_READ_ACROSS_ROLES` may read other principals' threads.

## Tracing

Off unless `TRACING_ENABLED=true`. With `LANGSMITH_API_KEY` traces go to LangSmith (`LANGSMITH_PROJECT`,
`LANGSMITH_ENDPOINT`); otherwise over OTLP/HTTP to `OTEL_EXPORTER_OTLP_ENDPOINT`. `TRACE_CAPTURE=metadata`
(default) records structure, timing, token counts, tool names, error types and hashed identifiers only;
`TRACE_CAPTURE=full` adds prompts, completions, tool arguments and results, and error messages. Run records
(`runs` table under postgres, in-process under memory) follow the same policy.
{%- if cookiecutter.deployment_target == 'kubernetes' %}

## Environments

| Environment | Namespace | Values | Postgres |
|---|---|---|---|
| `dev` | `{{cookiecutter.project_name}}-dev` | `values.yaml` + `values-dev.yaml` | bundled subchart (`postgresql.enabled=true`) |
| `staging` | `{{cookiecutter.project_name}}-staging` | `values.yaml` + `values-staging.yaml` | external, `POSTGRES_DSN`{% if cookiecutter.runtime == 'langgraph-server' %} (`DATABASE_URI` + `REDIS_URI`){% endif %} from the Secret |
| `prod` | `{{cookiecutter.project_name}}-prod` | `values.yaml` + `values-prod.yaml` | external, from the Secret |

The Helm release name is `{{cookiecutter.project_name}}`. Contexts and namespaces are recorded under `environments:`
in `graph-agents-cli-manifest.yaml`; `deploy --env <env>` and `secrets apply --env <env>` read `.env.<env>`
when present, else `.env`. Traffic enters through a Gateway API `HTTPRoute` (set `gateway.parentRef` and
`gateway.hostname` in the values file) or an `Ingress` (`ingress.enabled=true`); TLS comes from
`tls.existingSecret` or cert-manager (`tls.certManager.enabled=true`). Nothing is installed by the CLI or the
chart: `graph-agents-cli infra check --env <env>` reports what the cluster has.

Before the first deploy, fetch the chart dependencies once: `helm dependency build deployment/helm/{{cookiecutter.project_name}}`.

## Secrets

The chart never templates the app Secret; it mounts `<release>-app` (`existingSecret`) with `envFrom`.
Only the keys listed under `secrets.keys` in the manifest are exported from an env file:
`{{ cookiecutter.secret_keys | join('`, `') }}` (the token of every `auth: bearer` API in `api-policy.yaml` included).

```bash
graph-agents-cli secrets apply --env staging --env-file .env.staging   # create or update
graph-agents-cli secrets status --env staging                          # which keys are present (no values)
graph-agents-cli deploy --env staging --restart                        # roll the pods after a rotation
```

`secrets apply` generates `API_KEY` (32 random bytes, hex) when the env file has none and prints it once.
In `argocd` and `helm-push` modes CI never holds application secrets: the owner named in the manifest
(`secrets.owner`) runs `secrets apply` from a workstation with cluster access, once per environment.

## Deploying (`cd: {{cookiecutter.cd}}`)
{%- if cookiecutter.cd == 'skip' %}

Direct mode. `graph-agents-cli deploy --env dev` builds the image, loads it into a kind/k3s/minikube/Docker
Desktop node (or pushes it to `{{cookiecutter.registry}}` for a remote cluster), applies the Secret from the
allow-listed keys and runs `helm upgrade --install`. `deploy --env staging|prod` works the same way from a
workstation. Add CD later with `graph-agents-cli scaffold enhance --cd argocd|helm-push`.
{%- elif cookiecutter.cd == 'argocd' %}

Pull-based. On every push to `main` the `staging` workflow builds and pushes
`{{cookiecutter.registry}}/{{cookiecutter.project_name}}:<short sha>` (`${GITHUB_SHA::7}`, the tag a workstation `deploy` also uses), writes the tag into `values-staging.yaml`
and opens a PR with auto-merge; Argo CD reconciles `main` into `{{cookiecutter.project_name}}-staging`.
Production changes only through a PR that touches `values-prod.yaml`: the `promote-to-prod` workflow (GitHub
`production` environment) or a workstation `graph-agents-cli deploy --env prod --image <ref>` opens it, and its
merge (code-owner review, no self-approval, `pr_checks` green) is the single production gate. `deploy` never
runs helm in this mode; `deploy --status` and `--restart` and the `secrets` commands are the only cluster
operations. The production `Application` has no automated sync: sync it in Argo after the merge.
Point `deployment/argocd/application-*.yaml` at this repository (`repoURL`) and apply them to the Argo CD namespace.
{%- elif cookiecutter.cd == 'helm-push' %}

Push-based. On every push to `main` the `staging` workflow builds and pushes
`{{cookiecutter.registry}}/{{cookiecutter.project_name}}:<short sha>` (`${GITHUB_SHA::7}`, the tag a workstation `deploy` also uses) and a **self-hosted runner** inside the network
runs `graph-agents-cli deploy --env staging --image <ref>` with the kubeconfig from the `KUBECONFIG` repository
secret. `promote-to-prod` (GitHub `production` environment) does the same for production. From a workstation
`deploy` is allowed for `dev` and refused for `staging`/`prod` unless `--force-direct`.
{%- endif %}

## GitHub settings the workflows rely on (not created by the CLI)

These are repository settings a workflow cannot create with the default token; `graph-agents-cli infra check`
reports whether they exist when a `GITHUB_TOKEN` is available.

- Environment `production`: required reviewers (at least one), "prevent self-review" enabled, deployment
  branches restricted to `main`, optional wait timer.
- Environment `staging`: deployment branches restricted to `main`; no reviewers.
- The production job declares `environment: production` and runs only from `main`.
- Branch protection on `main` (`argocd` and `helm-push`): pull requests required; required review from code
  owners; "dismiss stale approvals" and "prevent self-approval" enabled; `pr_checks` as a required status
  check; no bypass for the Actions token except allowing auto-merge for the staging PR.
- `.github/CODEOWNERS` maps `values-prod.yaml` and `application-prod.yaml` to the production approvers;
  replace the `@CHANGE-ME/production-approvers` placeholder.
- Registry credentials: GHCR works with the workflow token; other registries take `REGISTRY_USERNAME` /
  `REGISTRY_PASSWORD` secrets. `helm-push` additionally needs the `KUBECONFIG` secret and a self-hosted runner.
- Optional CI model access: repository variables `MODEL_PROVIDER` / `MODEL_NAME` and the matching key secret
  make `pr_checks` run the tests and the eval gate against a real model instead of the `fake` one.
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
