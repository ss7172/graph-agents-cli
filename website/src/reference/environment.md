---
description: Every environment variable graph-agents-cli reads and every setting of the generated service, with its default.
---

# Environment variables

<p class="gac-lede">The variables the CLI reads, and every setting of the service it
generates, with defaults and a link to the guide that explains each group.</p>

Two sets of variables are on this page. The **CLI's own** change how `graph-agents-cli`
behaves on your machine or in CI. The **service's** configure the agent a project runs:
locally from `.env`, in a cluster from the chart values and the app Secret.

## The CLI

`GRAPH_AGENTS_CLI_API_KEY`
:   The bearer credential `run`, `approvals` and `eval` send, locally and with `--url`, when
    no `Authorization` header is given: an `API_KEY` or a JWT. Read from the process
    environment only, never from `.env`; it keeps the credential out of the process list and
    your shell history. Locally, a `shared-bearer` project's `API_KEY` from `.env` is used
    when it is unset.

    **Default:** unset.

`GRAPH_AGENTS_CLI_APPROVER_API_KEY`
:   The bearer credential `eval generate` decides `role:` approval gates with. A gate that
    lists `requester` is decided as the eval identity instead. See
    [Evaluation](../guides/evaluation.md).

    **Default:** unset.

`GRAPH_AGENTS_CLI_RUN_PORT`
:   Port of the local server `run`, `approvals` and `eval` start on demand; used exactly
    (exit 3 when it is taken or not a port). `run --port` wins over it.

    **Default:** first free of `18080`-`18089`.

`GRAPH_AGENTS_CLI_INSTALL_SPEC`
:   Where `setup`, `update`, the `scaffold upgrade` baseline and generated projects' CI
    install the CLI from: a private mirror, a wheel, or a package index. See [Install source
    override](#install-source-override).

    **Default:** the release tag of this repository.

`GRAPH_AGENTS_CLI_NO_UPDATE_CHECK`
:   `1` turns off the check for a newer release on GitHub (at most every 12 hours) and the
    skills version check. Set it for disconnected installs.

    **Default:** unset.

`GRAPH_AGENTS_CLI_DEBUG`
:   `1` shows the traceback behind a one-line network, file or parse error. See [Exit
    codes](exit-codes.md).

    **Default:** unset.

`GRAPH_AGENTS_CLI_DISABLE_OVERRIDES`
:   `1` ignores extension overrides and additions; every generated CI and CD job sets it.
    See [Extensions](../guides/extensions.md).

    **Default:** unset.

`GRAPH_AGENTS_CLI_SKIP_VERSION_LOCK`
:   `1` lets `scaffold enhance` run with the running build instead of the version the
    project records.

    **Default:** unset.

### Install source override

`GRAPH_AGENTS_CLI_INSTALL_SPEC` takes any spec `uv tool install` accepts. Write `{version}`
where the release number goes, so the upgrade baseline can install an older release:

```bash
export GRAPH_AGENTS_CLI_INSTALL_SPEC='git+https://git.example.com/graph-agents-cli@v{version}'
```

- `{version}` is a release number (the `cli_version` a project records), so the override
  names releases only. A build between two releases is named with
  `scaffold upgrade --baseline-ref` (see [Upgrading projects](../guides/upgrading.md)).
- An override without `{version}` installs one fixed build: the upgrade baseline refuses it
  (exit 3), since it cannot install the release a project was made with.
- An override with a control character or whitespace is refused (exit 3), except the two
  spaces of a PEP 508 `graph-agents-cli @ <url>` reference.

The value ends up in each generated project as `GRAPH_AGENTS_CLI_SPEC` in
`.github/agent.env` (see [Project manifest](manifest.md#the-workflows-settings)).

### Read from the project

Some commands also read the project's `.env`, below the process environment (a variable set
in your shell wins):

| Command | What it reads |
|---|---|
| `login` | The provider key or `OPENAI_BASE_URL`, `API_KEY`, the `AUTH_JWT_*` key settings, `LANGSMITH_API_KEY` when `TRACING_ENABLED` is on, `MODEL_PROVIDER` and `AUTH_POLICY` |
| `run`, `approvals`, `eval` | The whole file, passed to the local server they start; `AUTH_POLICY`, `CHECKPOINTER` and `API_KEY` to decide how to call it |
| `eval grade`, `eval analyze --judge` | `JUDGE_MODEL_PROVIDER` and `JUDGE_MODEL_NAME` (then the agent's `MODEL_PROVIDER` and `MODEL_NAME`), unless `eval_config.yaml` names the judge |
| `eval submit` | `LANGSMITH_API_KEY`, `LANGSMITH_ENDPOINT` |
| `auth dev-token` | `AUTH_POLICY`, `APP_ENV`, the `AUTH_JWT_*` key settings (and fills the blank ones) |

### Read by the tools the CLI runs

The CLI passes its environment to `helm`, `kubectl`, `docker`, `git`, `gh` and `uv`, so their
own variables apply (`KUBECONFIG`, `DOCKER_HOST`, `UV_INDEX_URL`, ...). A few are also read
by the CLI itself:

| Variable | Used for |
|---|---|
| `GH_HOST`, `GITHUB_HOST`, `GITHUB_SERVER_URL` | Declare a GitHub Enterprise Server host, so `argocd`-mode `deploy` opens its pull request there. See [CI/CD](../guides/cicd.md). |
| `GITHUB_TOKEN`, `GH_TOKEN`, `GH_ENTERPRISE_TOKEN` | The token for that pull request (the first one set). `infra check` reports the repository's GitHub settings only when `gh` is logged in or `GITHUB_TOKEN` is set. |
| `GITHUB_ACTIONS` | `true` marks a GitHub Actions job: a `helm-push` project deploys to staging and prod directly only there (or with `--force-direct`). |
| `CI`, `BUILD_ID`, `GITHUB_ACTIONS`, `GITLAB_CI` | Any of them skips the skills version check. |
| `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY` | The CLI's own requests to another machine (a `--url` agent, the `login` model probe, the GitHub API) go through them, SOCKS (`socks5://`, `socks5h://`) included. Requests to this machine (the local server of `run`, `approvals` and `eval`, the playground) never use a proxy. A proxy the CLI cannot use (another scheme) is a one-line error naming the variable, exit 3. |

## The generated service

Every setting has a default in code and is documented in the project's `.env.example`, the
full contract. Locally the app reads `.env` when it starts; in a cluster the chart sets the
non-secret values from `values-<env>.yaml` and the Secret carries the allow-listed secret
ones (see [Secrets](../guides/secrets.md)).

!!! info "How settings are parsed"

    - **`.env` sits below the process environment.** The app reads `.env` first, as it is
      imported, so settings fixed at import (the A2A card and its auth scheme, `/docs`, CORS,
      the auth policy's startup check) follow it too. `PYTHON_DOTENV_DISABLED=1` skips the
      file; the project's own tests set it.
    - **A value that does not parse stops the app at startup**, naming every bad variable
      at once: the guardrails and limits, the pool sizes, `LOG_LEVEL`, `LOG_FORMAT`,
      `METRICS_ENABLED`, `MODEL_TIMEOUT_S`, `MODEL_MAX_RETRIES`, `TRACE_CAPTURE`,
      `A2A_TASK_TTL_S` and `MAX_MESSAGE_CHARS`. Nothing silently falls back to a default.
    - **`APP_ENV` counts as dev only when it is exactly `dev`.** `DEV`, ` dev`,
      `development` or unset are a deployed environment.
    - **`TRACING_ENABLED` is on only for `true`, `yes` or `1`** (any case); any other value
      leaves it off.
    - **The auth settings follow the policy's startup rule**: an unknown `AUTH_POLICY` never
      starts, and a misconfigured policy stops the process outside `APP_ENV=dev` (under dev
      requests get 503). See [Authentication](../guides/authentication.md).

### Application and runtime

| Variable | Default | Meaning |
|---|---|---|
| `APP_ENV` | unset (`dev` in `.env.example`) | Exactly `dev` enables `/playground`, `/docs`, `/openapi.json` and the dev relaxations. |
| `HOST` | `127.0.0.1` (`0.0.0.0` in the image) | Address the server binds. |
| `PORT` | `8000` | Port the server listens on. |
| `RUNTIME` | detected | `fastapi` or `langgraph-server`; overrides the detection (the server image sets `LANGGRAPH_SERVER=1`). |
| `AGENT_VERSION` | `0.1.0` | The version the A2A card, the OpenAPI document and traces report. |

### Model

| Variable | Default | Meaning |
|---|---|---|
| `MODEL_PROVIDER` | `openai` (the `create` choice in `.env.example`) | `openai`, `anthropic`, `gemini` or `openai-compatible`; `fake` is the deterministic test model. |
| `MODEL_NAME` | the `create` choice | The model. Required: a run fails with a message naming it when it is unset. |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `MODEL_API_KEY` | | The provider key (a secret): one per provider, `MODEL_API_KEY` for `openai-compatible`. |
| `OPENAI_BASE_URL` | | The endpoint of an `openai-compatible` server (vLLM, TGI, Ollama). |
| `MODEL_TIMEOUT_S` | `60` | Timeout of one model request, in seconds; `0` keeps the provider SDK's default. |
| `MODEL_MAX_RETRIES` | `2` | Retries of a failed model request. |
| `JUDGE_MODEL_PROVIDER`, `JUDGE_MODEL_NAME`, `JUDGE_BASE_URL`, `JUDGE_API_KEY` | the agent's provider, model and key | The eval judge. See [Evaluation](../guides/evaluation.md). |

### Persistence

| Variable | Default | Meaning |
|---|---|---|
| `CHECKPOINTER` | `memory` (the chart sets `postgres`) | `fastapi` runtime: `memory` keeps state in the process (lost on restart), `postgres` in `POSTGRES_DSN`. |
| `POSTGRES_DSN` | | The database (a secret). Passed to psycopg unchanged, so every libpq parameter works. |
| `DB_POOL_MIN_SIZE`, `DB_POOL_MAX_SIZE` | `1`, `10` | The connection pool of each process, shared by the checkpointer and the app's tables. |
| `PGCONNECT_TIMEOUT` | | When set, replaces the app's own `connect_timeout=5` default. |
| `DATABASE_URI`, `REDIS_URI` | | `langgraph-server` runtime: the server's persistence (secrets). |
| `RETENTION_DAYS` | `0` | Delete threads idle for more than N days, hourly, on every replica; `0` keeps everything. |

The database behaviour (keepalives, `/ready`, schema setup) is in
[Deploy to Kubernetes](../guides/deploy.md).

### Authentication

| Variable | Default | Meaning |
|---|---|---|
| `AUTH_POLICY` | `shared-bearer` (the `create` choice) | `shared-bearer`, `jwt` or `custom`. |
| `API_KEY` | | `shared-bearer`: the key clients send as `Authorization: Bearer <API_KEY>` (a secret). Unset answers 503, never "no auth". |
| `AUTH_READ_ACROSS_ROLES` | empty | Comma list of roles that may read, never continue or delete, other principals' threads (and list their approvals). |
| `AUTH_ADMIN_ROLES` | empty (nobody) | `langgraph-server`: roles that may manage assistants, crons and the store. |
| `AUTH_FORWARD_HEADERS` | `authorization,cookie` | `langgraph-server` with `LANGGRAPH_SERVER_URL`: the request headers passed on to the server's auth handler; empty forwards nothing. |
| `PRINCIPAL_HASH_SALT` | | When set (a secret), principal ids in logs, traces and run records are HMAC-SHA256 with it instead of a plain hash. Keep it stable. |
| `AUTH_JWT_*` | | The `jwt` policy: `AUTH_JWT_JWKS_URL`, `AUTH_JWT_PUBLIC_KEY`, `AUTH_JWT_ISSUER`, `AUTH_JWT_AUDIENCE`, `AUTH_JWT_ALGORITHMS`, `AUTH_JWT_ALLOW_HS`, `AUTH_JWT_SECRET`, `AUTH_JWT_PRINCIPAL_CLAIM`, `AUTH_JWT_ROLES_CLAIM`, `AUTH_JWT_LEEWAY_S`, `AUTH_JWT_JWKS_CACHE_S`, `AUTH_JWT_JWKS_ALLOW_HTTP`. Defaults and rules: [Authentication](../guides/authentication.md). |

### Outbound APIs

| Variable | Default | Meaning |
|---|---|---|
| `API_POLICY_PATH` | `api-policy.yaml` | Path of the outbound API policy. See [api-policy.yaml](api-policy-schema.md). |
| each API's `base_url_env` | | The API's base URL, per environment in `values-<env>.yaml`. The URL may carry a path prefix. |
| each `auth: bearer` API's `token_env` | | The API's token (a secret; `api add` adds it to `secrets.keys`). |

### Guardrails and limits

| Variable | Default | Meaning |
|---|---|---|
| `RUN_TIMEOUT_S` | `300` | Wall-clock limit of one run; the run is cancelled with status `timeout`. |
| `RECURSION_LIMIT` | `50` | Graph steps per run: two to answer and two per sequential tool call (24 calls). The run then ends with status `step_limit`. |
| `MAX_REQUEST_BYTES` | `1048576` | Request body cap (413 above it). |
| `MAX_MESSAGE_CHARS` | `32000` | Longest user message on `/chat` (422) and A2A (invalid params): one cap for every surface. |
| `MAX_METADATA_KEYS` | `16` | Keys in a `/chat` `metadata` object (422 above it). |
| `MAX_METADATA_VALUE_CHARS` | `256` | Characters per metadata key and string value (422 above it). |
| `SSE_HEARTBEAT_S` | `15` | Idle seconds before the stream sends a `: keep-alive` comment. |

What each limit does to a request is in the [HTTP API](http-api.md#guardrails).

### Logging, metrics and CORS

| Variable | Default | Meaning |
|---|---|---|
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. `DEBUG` also turns on third-party debug output, which can hold message content. |
| `LOG_FORMAT` | `text` under `APP_ENV=dev`, else `json` | `fastapi` runtime: `json` or `text`. The LangGraph Server formats its own lines (`LOG_JSON`). |
| `METRICS_ENABLED` | `true` | Serve Prometheus text at `GET /metrics` (`true` or `false`). |
| `METRICS_TOKEN` | | When set (a secret), `/metrics` answers only `Authorization: Bearer <METRICS_TOKEN>`. |
| `CORS_ALLOW_ORIGINS` | empty (no CORS) | Comma list of browser origins allowed to call the API. |

### Tracing

| Variable | Default | Meaning |
|---|---|---|
| `TRACING_ENABLED` | `false` | On only for `true`, `yes` or `1`. |
| `TRACE_CAPTURE` | `metadata` | `metadata`: structure, timing, token counts, tool names, error types and hashed ids. `full`: also prompts, completions, tool I/O and the client's `/chat` metadata. |
| `LANGSMITH_API_KEY` | | With tracing on, traces go to LangSmith (a secret). |
| `LANGSMITH_PROJECT` | the project name | The LangSmith project. |
| `LANGSMITH_ENDPOINT` | LangSmith's default | A self-hosted LangSmith. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | | Without a LangSmith key, spans go over OTLP/HTTP here. |
| `OTEL_SERVICE_NAME` | the project name | The OTLP service name. |

See [Observability](../guides/observability.md) for what each mode sends where.

### A2A

| Variable | Default | Meaning |
|---|---|---|
| `APP_URL` | `http://HOST:PORT` | The public base URL the agent card advertises; the chart sets it from `appUrl` or the route hostname. |
| `A2A_NAME` | the agent directory (`app`) | Mount name: the card at `/a2a/<name>/.well-known/agent-card.json`, JSON-RPC at `/a2a/<name>`. |
| `A2A_DESCRIPTION` | a generic description | What the card (and its chat skill) says the agent does. |
| `A2A_TASK_TTL_S` | `3600` | Seconds a task is kept after its last update; `0` keeps tasks until restart. |

See [HTTP API](http-api.md#a2a).

### `langgraph-server` runtime

| Variable | Default | Meaning |
|---|---|---|
| `LANGGRAPH_SERVER_URL` | in-process loopback | Reach the LangGraph Server over HTTP instead (its auth handler then sees `AUTH_FORWARD_HEADERS`). |
| `LANGGRAPH_SERVER` | `1` in the server image | Marks the server runtime for detection. |

The server's own variables (licence, `LOG_JSON`, ...) are LangChain's; see
[Develop your agent](../guides/develop.md) for the runtime.

## Offline

For a disconnected install, set `GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1` and point
`GRAPH_AGENTS_CLI_INSTALL_SPEC` at a mirror; the service needs `MODEL_PROVIDER=openai-compatible`
with `OPENAI_BASE_URL` on your network and tracing off or at an in-cluster OTLP collector.
The whole profile is in [Offline profile](../guides/offline.md).

<div class="grid cards gac-cols-3" markdown>

-   :material-shield-key-outline:{ .lg } **[Authentication](../guides/authentication.md)**

    The three policies and every `AUTH_JWT_*` setting.

-   :material-api:{ .lg } **[HTTP API](http-api.md)**

    What the limits and timeouts do to requests and runs.

-   :material-chart-line:{ .lg } **[Observability](../guides/observability.md)**

    Logging, metrics and tracing in practice.

</div>
