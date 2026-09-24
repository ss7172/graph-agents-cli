# Changelog

All notable changes to graph-agents-cli are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the version is 0.x, a
minor release may contain breaking changes; each one is listed under "Breaking changes and
migration" with the steps to follow.

## [Unreleased]

## [0.2.0] - 2026-09-23

graph-agents-cli is now a generic CLI for building, evaluating and deploying LangGraph agents
on self-hosted Kubernetes, for any project and any domain. Nothing in the CLI, the template,
the skills or a generated project is shaped around one consumer: projects choose an auth
policy and declare the outbound APIs their tools may call. This release also closes most of
the production-readiness findings of an independent assessment of 0.1.0 (runtime guardrails,
per-user authentication, deploy safety, supply chain, release engineering); the remaining
ones are listed under "Known limitations" and "Where it is behind" in the README.

Install from the release tag (the package is not on PyPI yet):

```bash
uv tool install git+https://github.com/ss7172/graph-agents-cli@v0.2.0
```

### Breaking changes and migration

- **`--auth-policy product-session` is now `custom`.** `create --auth-policy product-session`
  is refused with a hint. A manifest or `AUTH_POLICY` that still says `product-session` is
  read as `custom`, with a one-line deprecation warning. In the template,
  `app/policies/product_session.py` (`ProductSessionPolicy`) became `app/policies/custom.py`
  (`CustomPolicy`). Set `AUTH_POLICY=custom` and `auth_policy: custom`.
- **The product API policy is now a multi-API outbound policy.** `product-policy.yaml`
  becomes `api-policy.yaml`, `create --product-policy` becomes `create --api-policy`, the
  manifest block `product_api:` becomes `api_policy: {policy_file: api-policy.yaml}`, the
  cookiecutter variable `has_product_policy` becomes `has_api_policy`, and the tool
  declaration `PRODUCT_CALLS` becomes `API_CALLS` with an `"api"` key per entry. The file
  now declares any number of APIs under `apis: {<name>: ...}`; `allowed_methods` is
  required; `auth: forwarded-session` is now `auth: forward`. `create`, `scaffold enhance`,
  `scaffold upgrade` and `lint` stop on a project that still uses the old format and print
  the migration steps (exit 3); `--product-policy` is refused with a rename hint; a tool
  module that declares `PRODUCT_CALLS` is a lint error.
- **Outbound API calls fail closed.** Without `api-policy.yaml`, or for an API the file does
  not declare, `get_client()` raises `ApiPolicyError` and nothing is sent. 0.1.0 sent every
  call unrestricted (with a warning) when no policy file existed.
- **The default guidance file is `AGENTS.md`** (was `GEMINI.md`). Existing projects keep the
  file name their manifest records; pass `--agent-guidance-filename GEMINI.md` to `create` to
  keep the old default.
- **`CLI_VERSION_PIN` is now `GRAPH_AGENTS_CLI_SPEC`** in `.github/agent.env` (the
  cookiecutter variable `cli_version_pin` is now `cli_install_spec`). The value is a full
  install spec, `git+https://github.com/ss7172/graph-agents-cli@v0.2.0` by default, and the
  workflows run `uvx --from "$GRAPH_AGENTS_CLI_SPEC" graph-agents-cli ...`. The workflows
  refuse an `agent.env` that still sets `CLI_VERSION_PIN`, with a rename hint.
- **Installation moved to a pinned git reference.** The package name `graph-agents-cli` was
  never published on PyPI, so `uv tool install graph-agents-cli` never worked; `setup`,
  `update`, the `scaffold upgrade` baseline and the generated workflows install
  `git+https://github.com/ss7172/graph-agents-cli@v<version>` instead, and the update check
  reads GitHub releases. `GRAPH_AGENTS_CLI_INSTALL_SPEC` overrides the source (a mirror, a
  wheel); write `{version}` where the version goes.
- **`deploy` and `secrets apply` outside `dev` need an explicit env file and kube context.**
  They read `.env.<env>` (or `--env-file`) and never fall back to `.env` (exit 3 without one).
  The kube context must be recorded as `environments.<env>.context` or passed with
  `--context`; the kubeconfig's current context is used only after a confirmation prompt, or
  `--yes` when there is no terminal (exit 1 otherwise). CI jobs that deploy need `--yes` or
  `--context` (the generated workflows pass both).
- **A live `API_KEY` is never replaced implicitly.** Under `shared-bearer`, `secrets apply`
  and `deploy` keep the key in the cluster unless the env file sets a different one **and**
  `--rotate-api-key` is passed. A generated key is written to the env file (mode 0600)
  instead of being printed.
- **`helm-push` reads `DEPLOY_KUBECONFIG` from the `staging` and `production` GitHub
  environments** (was the repository secret `KUBECONFIG`), so the production reviewers gate
  it. Create the environment secrets and delete the repository secret. `deploy --env
  staging|prod` is refused outside CI in `helm-push` mode even with `--image`
  (`--force-direct` overrides).
- **Chart defaults are stricter.** `image.tag` defaults to `""` in every environment and the
  chart refuses to render without a tag (never `latest` by default); the tag must be a
  quoted string. The HTTPRoute and Ingress publish only `route.publicPaths` (`/chat`,
  `/threads`, `/a2a/<agent>`, plus `route.devPaths` under `APP_ENV=dev`) instead of every
  path; `/health`, `/ready` and `/metrics` stay inside the cluster. Outside `dev` the app
  Secret is required (`secretOptional: false`): pods do not start without it.
- **Exit codes are consistent** (0 ok, 1 refused or failed gate, 2 tool failure, 3
  configuration error): an unexpected crash exits 2 (was 1), running outside a project
  exits 3 (was 1), `run` exits 2 when the agent cannot be reached or goes silent (was 1),
  `secrets status` exits 1 only when a *required* key is missing (`--strict` for every
  allow-listed key), and a local server that cannot start exits 2 from `run` and `eval`.
  `scaffold upgrade` exits 3 for a manifest without a released `cli_version` or an
  install-spec override without `{version}`, and 2 when `uvx` is missing or cannot fetch and
  run the prior release (all were 1); a version-locked `scaffold enhance` without `uvx`
  exits 2 (was 1).
- **Chat API changes.** The SSE `error` event is `{code, message, error_id, run_id}` with
  `code` one of `run_failed`, `timeout`, `recursion_limit`, `thread_busy`, `unavailable`,
  `forbidden` (was the exception class name); details go to the server log (and `detail` only
  under `APP_ENV=dev`). `/chat` metadata outside the caps is refused with 422 (was silently
  dropped). Under `langgraph-server` thread ids must be UUIDs.
- **`.github/agent.env` is data, not shell.** The workflows accept only `IMAGE_REPOSITORY`,
  `RELEASE_NAME`, `CHART_PATH`, `RUNTIME`, `CD` and `GRAPH_AGENTS_CLI_SPEC`, and refuse
  anything else.
- **A run that reaches the step limit ends with a reply, status `step_limit`.**
  `RECURSION_LIMIT` defaults to 50 (was 25): two steps to answer plus two per sequential tool
  call, so 24 calls. A run that reaches it streams a final reply saying so and ends with
  `message.end` `"status": "step_limit"` (was an `error` event `recursion_limit`, now sent only
  when the reply cannot be written); its work stays in the thread. Clients that treat any
  status other than `ok` as a failure should accept `step_limit`; the eval client counts it as
  an error turn ("message.end status step_limit").
- **Run records and statuses.** A run is recorded when it starts (`running`) and ends `ok`,
  `step_limit`, `error`, `timeout`, `cancelled` or `interrupted` (its lease was lost, or its
  process died: reconciled about a minute after the lease expires, `error_type`
  `ProcessLost`). Dashboards keyed on the old statuses need the new ones.
- **`GET /threads` lists the caller's own threads by default, read-across roles included**
  (they got every principal's before); `?scope=all` lists every thread for a role in
  `AUTH_READ_ACROSS_ROLES` (403 otherwise; any other scope is 422). Rows gain `owner`, the
  hashed principal id.
- **One message cap for every surface.** `MAX_MESSAGE_CHARS` (default 32000): a longer
  message is 422 on `/chat` and JSON-RPC -32602 over A2A 1.0 and 0.3 (A2A accepted up to
  `MAX_REQUEST_BYTES` before). A `/chat` 422 no longer echoes the submitted value (`input`,
  `url`); a too-long message is `value_error` (was `string_too_long`); text with an unpaired
  surrogate is 422 (was 500).
- **Failed tool calls reach clients as an error id.** Outside `APP_ENV=dev` a failed call's
  `tool.result` `result` and its message in `GET /threads/{id}/messages` read "The tool call
  did not succeed. Reference: <error_id>." with a new `error_id` field; the error text
  (policy rule, limit, upstream status and reason) goes to the model only. API-policy
  refusals read "... refused by the API policy: <reason>." (no "(api-policy.yaml)").
- **Outbound calls: stricter headers and no method override.** Tool-supplied `Host`,
  method-override (`X-HTTP-Method-Override` and its underscore spelling), `X-Forwarded-*`,
  `Forwarded`, `X-Original-URL`, `X-Rewrite-URL` and hop-by-hop headers are dropped with a
  warning; a `_method` query parameter or top-level JSON body key raises `ApiPolicyError`.
- **Eval gates can change result.** `expect.contains` and `not_contains` ignore case (add
  `expect.case_insensitive: false` for exact matching; a `not_contains` word now also fails
  in another case). A quality metric's pass rate is passed / scored over the cases that ran
  it (was over every planned case), so a metric declared on only some cases can now miss its
  gate. `eval_config.yaml` `judge:` accepts only `provider`, `model` and
  `max_tool_result_chars`, and an unknown `prompt_template` placeholder is exit 3 at load.
- **New projects list `API_KEY` in `secrets.keys` only under `shared-bearer`** (the one policy
  that reads it); `secrets apply`, `deploy` and `login --write-env` generate it only there.
  Existing manifests keep what they list.
- **Chart: bounded shutdown and a separate metrics Secret.** The chart refuses to render when
  `terminationGracePeriodSeconds` (30) is not above `shutdown.preStopSleepSeconds` (5) +
  `shutdown.drainSeconds` (20); raise it with them. The ServiceMonitor's bearer token now
  comes from the Secret `<release>-metrics` (was the app Secret).
- **`deploy` refuses more outside `dev`**: a `CHANGE-ME` value in the chart `env` (exit 3; a
  warning in `dev`), and `jwt` without a JWKS URL or public key, `AUTH_JWT_ISSUER` and
  `AUTH_JWT_AUDIENCE` (exit 3). `deploy --status` and `--restart` wait at most `--timeout`
  and exit 1 / 2 when the pods are not ready (they returned at once before).
- **`extension add` / `update` without a terminal never prompts**: untrusted code needs `-y`
  (exit 1 otherwise; `update` keeps the installed copy).

#### Upgrading a running deployment

1. **Roll out with `Recreate`, or at one replica.** Runs take a Postgres lease per thread
   (`thread_locks`, `agent_thread_locks` under `langgraph-server`), which older builds do not
   honour: 0.1.0 has no run lock across replicas, and pre-release 0.2.0 builds used a session
   advisory lock. While old and new pods run side by side, one thread can run on both.
2. The new tables, their token sequence and a partial index on the runs table (built
   `CONCURRENTLY`) are created at startup; nothing to run by hand.
3. With `metrics.serviceMonitor.bearerToken.enabled` and the default Secret, run
   `graph-agents-cli secrets apply --env <env>` once after upgrading the chart, so
   `<release>-metrics` exists (`infra check` shows the row).
4. If you lowered `terminationGracePeriodSeconds`, keep it above
   `shutdown.preStopSleepSeconds + shutdown.drainSeconds` (or lower those).
5. Clients: accept `message.end` status `step_limit`; list other principals' threads with
   `GET /threads?scope=all`; let the server generate thread ids (omit `thread_id`) or use
   UUID4s: a thread id another principal used first is theirs.
6. Eval datasets: review `not_contains` checks (now case-insensitive) and metrics declared
   on only some cases (see above).
7. Template files you have not edited take the new versions with `graph-agents-cli scaffold
   upgrade`; in `app/agent.py` keep `middleware()` (`SurfaceApiErrors` and
   `UntrustedToolResults`) if you rewrote it, and in tools use `ToolRuntime[Any]`.


#### Upgrading a project created with 0.1.0

1. If the project uses `product-policy.yaml`, migrate it first: every project command prints
   the steps (rename the file to `api-policy.yaml`, move the fields under
   `apis: {<name>: ...}` with `allowed_methods`, change `product_api:` in the manifest to
   `api_policy: {policy_file: api-policy.yaml}`, rename `PRODUCT_CALLS` to `API_CALLS` with an
   `"api"` key).
2. Run `graph-agents-cli scaffold upgrade` (preview with `--dry-run`, apply with `-y`). Its
   authentic baseline re-renders the project with graph-agents-cli 0.1.0 from the `v0.1.0`
   tag of this repository (commit `fc3f2f9`), so it updates every scaffolding file you did
   not edit (about 30: `app/app_utils/*.py`, `app/fast_api_app.py`, the Dockerfile, the chart
   templates and `values.yaml`, `pr_checks.yaml`, `.github/agent.env`, ...), adds the new
   ones (`app/policies/custom.py` among them), removes `app/app_utils/product_client.py` and
   reports your own edits as conflicts. It needs `uvx` and access to the repository.

   If the tag cannot be fetched (an offline mirror, a fork without tags), build the same
   baseline from any clone that holds commit `fc3f2f9`:

   ```bash
   git clone https://github.com/ss7172/graph-agents-cli /tmp/gac   # or your mirror
   git -C /tmp/gac tag v0.1.0 fc3f2f9
   GRAPH_AGENTS_CLI_INSTALL_SPEC='git+file:///tmp/gac@v{version}' graph-agents-cli scaffold upgrade -y
   ```

   The override also becomes the new `.github/agent.env`'s `GRAPH_AGENTS_CLI_SPEC`: set that
   line back to `git+https://github.com/ss7172/graph-agents-cli@v0.2.0` (or your mirror).

   **Do not use `--baseline current` for a 0.1.0 project.** It compares against the 0.2.0
   templates, so it cannot tell your edits from 0.2.0's changes: every scaffolding file 0.2.0
   changed is listed under "Will preserve" and keeps its 0.1.0 content, the new dependencies
   (`pyjwt`, `prometheus-client`) are not merged, and only new files are added (not
   `app/policies/custom.py`). After step 3 the project's tests fail to import and `/chat`
   answers 500. If you ran it, restore the project from the backup it printed
   (`~/.graph-agents-cli/backups/...`) or from git, and upgrade with the authentic baseline.
3. `scaffold upgrade` never rewrites agent code or config, so port these by hand (compare
   with a fresh `graph-agents-cli create` of the same settings):
   - `app/policies/__init__.py`: replace it with the 0.2.0 registry. The old file imports
     `PRODUCT_SESSION`, which no longer exists, so the app does not start until it is
     replaced. If you implemented `ProductSessionPolicy`, move it into
     `app/policies/custom.py` as `CustomPolicy` and delete `policies/product_session.py`.
   - `app/agent.py`: import `ApiCallError`, `ApiPolicyError` from `app_utils.api_client`
     (the old file imports the removed `app_utils.product_client`) and bind
     `recursion_limit()`; an unmodified file can be replaced with the new one.
   - `app/tools/`: delete `product_lookup.py` (or port it to `get_client()`), rename
     `PRODUCT_CALLS` to `API_CALLS` in every module (`weather.py` included), and take the new
     `tools/__init__.py`.
   - `deployment/helm/<name>/values-dev.yaml`: add `secretOptional: true` (without it the dev
     pods wait for the app Secret); set `image.tag: ""` in each `values-<env>.yaml` (or a
     quoted tag) instead of `latest`; take the prod `resources` and `topologySpread` if wanted.
   - `.env.example`: compare with a fresh render for the new variables.
4. `graph-agents-cli install`, `uv run pytest tests/unit tests/integration` with
   `MODEL_PROVIDER=fake`, and `graph-agents-cli lint`.

### Added

- **`jwt` auth policy**: per-user principals from a verified OIDC/JWT bearer token. JWKS URL
  (`AUTH_JWT_JWKS_URL`, cached for `AUTH_JWT_JWKS_CACHE_S`, one rate-limited refetch on an
  unknown key id, stale-while-revalidate, a bounded grace when the issuer is down) or one PEM
  key (`AUTH_JWT_PUBLIC_KEY`); issuer and audience (required outside `APP_ENV=dev`),
  `exp`/`nbf`/`iat` with `AUTH_JWT_LEEWAY_S`; an algorithm allow-list (`AUTH_JWT_ALGORITHMS`,
  default `RS256,ES256`; never `none`; HS256/384/512 only with `AUTH_JWT_ALLOW_HS=true` and a
  32-byte `AUTH_JWT_SECRET`); `AUTH_JWT_PRINCIPAL_CLAIM` and `AUTH_JWT_ROLES_CLAIM` (dotted
  paths); https JWKS outside dev unless `AUTH_JWT_JWKS_ALLOW_HTTP=true`. RFC 6750 challenges;
  nothing from the token is logged.
- **`custom` auth policy** as a documented, fail-closed interface (`authenticate`,
  `authorize`, optional `startup_problems()`), and `Principal.public_attributes()`: secrets
  live only under `attributes["credentials"]` and are never persisted, logged or traced.
- Startup fails closed: an unknown `AUTH_POLICY` never starts; a misconfigured policy stops
  the process outside `APP_ENV=dev`.
- `AUTH_ADMIN_ROLES`: under `langgraph-server`, only these roles may create, update or delete
  assistants and crons or write the store (default: nobody); reads are open to authenticated
  principals; every other native-API action is denied by default.
- **`api-policy.yaml`** (see Breaking changes): strict schema shared byte-for-byte by
  `create`, `lint` and the runtime client (`app_utils/api_client.py`), `auth: none | bearer |
  forward`, `allowed_operations` / `denied_operations` (denials win and fail closed),
  OpenAPI validation in `lint`, timeouts, an enforced pagination cap, path-prefix-safe URL
  joins, no redirects. There is no default access level: every API lists its methods
  explicitly. `create --api-policy` validates the file first, copies the OpenAPI specs it
  references and renders an example tool making the first operation the policy's first API
  allows, whatever its method (a `body` argument for POST, PUT and PATCH); bearer tokens join
  `secrets.keys`; `auth: forward` is refused under `langgraph-server`. The runtime client
  sends every allowed method (`request()`, `get`, `head`, `post`, `put`, `patch`, `delete`,
  `options`) with JSON bodies, query parameters and headers.
- **`graph-agents-cli api`**: the policy belongs to the project and evolves with the agent.
  `api add NAME --base-url-env ENV --auth none|bearer|forward --access
  read-only|read-write|custom` (`--access` is required: read-only = GET, HEAD; read-write =
  GET, HEAD, POST, PUT, PATCH, DELETE; custom = `--methods`), `api access`, `api allow` /
  `api deny` (by operationId, filled in from the API's OpenAPI spec when it has one; by
  `--method`/`--path`; or by both, pinning all three), `api revoke`, `api limits`, `api
  remove`, `api show [--json]` and `api check` (same as `lint --policy-only`). Every change
  validates the result, keeps comments and key order, prints a unified diff of each file it
  touches (the policy, the manifest's `api_policy` and `secrets.keys`, `.env.example`, the
  chart's `values.yaml`), writes atomically, says whether it widens or narrows access and how
  the tools' declared calls are affected; `--dry-run` prints the diff only; exit 3 on an
  invalid result, outside a project, or when the edit would also change another API that
  repeats the edited one through a YAML alias. `lint` prints every `graph-agents-cli api`
  command a refused call needs (the method, a denial, an allow-list entry pinning the call's
  method and path), and `lint` and `api check` exit 3 on an invalid `api-policy.yaml` (a
  configuration error, not a refused call).
- **Outbound call limits**: optional per-API `limits: {max_calls_per_run, rate_per_minute}`.
  `max_calls_per_run` counts the calls to that API within one agent run (the LangGraph run
  id, else the request's); `rate_per_minute` is a token bucket per process (per replica).
  A call over a limit is refused before it is sent, with a reason the model can read;
  a run's counters are dropped when a `/chat` or A2A run ends, and otherwise (LangGraph
  Server runs included) after an hour without a call.
- The `approval` key is reserved on an API and on an operation entry for human approval of
  calls (planned) and refused until then ("approval gates are not supported yet (planned);
  remove the approval key"), so a policy never counts on a gate that does not exist.
- **Endpoints**: `GET /ready` (readiness: the database is set up and answers within 2 s),
  `GET /metrics` (Prometheus: request count and latency, runs by status, active runs, run
  duration, tokens, database up; optional `METRICS_TOKEN`), `GET /threads` (the caller's
  threads; `?scope=all` for read-across roles) and `DELETE /threads/{thread_id}`.
- **Runtime guardrails**: one run per thread (409 `{"code": "thread_busy"}`, with a Postgres
  lease across replicas), `RUN_TIMEOUT_S`, `MODEL_TIMEOUT_S`, `MODEL_MAX_RETRIES`,
  `RECURSION_LIMIT` (50), `MAX_REQUEST_BYTES` (413), `MAX_MESSAGE_CHARS`, `MAX_METADATA_KEYS` and
  `MAX_METADATA_VALUE_CHARS` (422), `SSE_HEARTBEAT_S`, a client disconnect cancels the run,
  and a stopped run answers its open tool calls so the thread stays usable.
- `RETENTION_DAYS`: an hourly best-effort purge of threads (checkpoints and run records) idle
  longer than N days.
- Structured JSON logging (`LOG_FORMAT`, `LOG_LEVEL`) with request id (`X-Request-ID` on
  every response), run id, thread id and a hashed principal; optional `PRINCIPAL_HASH_SALT`
  keys the hash (HMAC-SHA256). Client-facing errors carry an `error_id`; details stay in the
  log.
- `CORS_ALLOW_ORIGINS` (empty: no CORS), `DB_POOL_MIN_SIZE` / `DB_POOL_MAX_SIZE`,
  `AUTH_FORWARD_HEADERS`, `A2A_TASK_TTL_S`, `API_POLICY_PATH`.
- `deploy`: `--context`, `--yes`, `--timeout` (default 5m), `--atomic/--no-atomic` (default
  atomic), `--rotate-api-key`; the resolved kube context and API server are printed before
  anything happens; a pre-deploy check that the Secret holds every required key; pod
  diagnostics (states, warning events, logs) when a rollout fails, then a rollback of this
  run's revision only; a refusal while another helm operation holds the release; workstation
  images from a dirty tree are tagged `<sha>-dirty-<timestamp>`.
- `secrets apply`: `--context`, `--yes`, `--rotate-api-key`; creates the namespace when it is
  missing. `secrets status`: `--context`, `--strict`, exit codes usable as a gate.
- `infra check`: rows for unreplaced `CHANGE-ME` placeholders (registry, chart image, chart
  env, CODEOWNERS, Argo CD `repoURL`), missing required Secret keys, and, for `helm-push`,
  whether `DEPLOY_KUBECONFIG` exists as an environment secret and no repository-level
  kubeconfig secret exists.
- Chart: `metrics.serviceMonitor.bearerToken` makes the ServiceMonitor send `METRICS_TOKEN`
  (from the Secret `<release>-metrics` by default, which `secrets apply` and `deploy` write
  with that key alone), so a token-protected `/metrics` can be scraped.
- `run --port` and `GRAPH_AGENTS_CLI_RUN_PORT`; a port preflight for `run` and `playground`
  (exit 3 when the port is taken); `GRAPH_AGENTS_CLI_DEBUG=1` shows the traceback behind a
  one-line error.
- `scaffold enhance --runtime/--model-provider` reconciles everything the change affects
  (model default, `secrets.keys`, `.env.example`, chart values merged key by key around your
  edits, `.github/agent.env`) and ends with a "Left for you" list; steps marked `(required)`
  make it exit 1, and an edited Dockerfile gets the new version beside it as
  `Dockerfile.new`.
- Chart: readiness on `/ready`, liveness and startup on `/health`; default requests and
  limits (100m / 256Mi, 1Gi limit; 250m / 512Mi requests in prod); read-only root filesystem with a `/tmp`
  emptyDir, uid/gid 1000, seccomp `RuntimeDefault`, all capabilities dropped, no service
  account token; optional NetworkPolicy, ServiceMonitor and scrape annotations (off by
  default); soft topology spread in prod; a chart-managed dev Postgres password that
  survives upgrades; subcharts pinned to exact versions and their images by digest.
- Generated workflows: `pr_checks` runs the tests on the fake model and the eval gate on the
  real provider when its key secret exists (with a warning when the gate runs on the fake
  model); the helm-push jobs resolve one kube context, pass `--context` and `--yes`, and
  verify the rollout (`/health`, `/ready`); `staging` can be dispatched by hand from `main`
  and does not re-trigger itself; argocd staging PRs supersede older ones;
  `GRAPH_AGENTS_CLI_DISABLE_OVERRIDES=1` in every CI and CD job; one `.github/CODEOWNERS` for
  every project covering `deployment/`, `.github/`, `api-policy.yaml`, `tests/eval/`,
  extensions and the manifest.
- Release engineering for the CLI itself: this changelog, `.github/workflows/ci.yml` (ruff
  and the fast suite on every pull request and push to `main`; the end-to-end suite nightly
  and on demand) and `.github/workflows/release.yml` (a tag `vX.Y.Z` builds the sdist and
  wheel and creates a GitHub Release; PyPI trusted publishing is opt-in).
- **`graph-agents-cli auth dev-token --sub USER [--roles R,...] [--ttl 12h]`**: a JWT for
  local runs of a `jwt` project (`APP_ENV=dev` only), printed alone on stdout for
  `export GRAPH_AGENTS_CLI_API_KEY="$(graph-agents-cli auth dev-token --sub alice)"`. The
  first call creates a dev RSA key in `.graph-agents-cli/dev-jwt/` (0600, git ignored; safe
  when several runs start at once) and fills blank `AUTH_JWT_PUBLIC_KEY`, `AUTH_JWT_ISSUER`
  and `AUTH_JWT_AUDIENCE` in `.env`. Refused (exit 3) for other policies, outside dev, or
  with a JWKS URL or another key configured. `login` gains the checks `jwt_key`,
  `jwt_token`, `jwt_claims` and `env_file`.
- **History repair**: a thread left mid tool call (a timeout, a disconnect, a crash, an OOM
  kill, a database outage) is repaired at the start of the next run on both runtimes: each
  open call gets an error result right after it, and a result written after a later message
  is moved back. Calls and results are paired turn by turn, so tool-call ids that repeat
  across turns (`call_0` in every message) never make a healthy thread look damaged, and a
  healthy thread is never rewritten.
- **Run leases**: one run per thread across replicas is a Postgres lease with a 30 s expiry,
  renewed every 5 s, with a fencing token checked before every checkpoint write. A frozen or
  partitioned replica frees its threads after 30 s (a session advisory lock held them for
  about 2 hours); a database restart or a killed session no longer lets a second replica run
  the thread. A run that cannot renew its lease stops (`interrupted`) before it writes.
- **Database outages**: connections default to `connect_timeout=5` and TCP keepalives (the
  DSN wins); requests answer 503 "Database unavailable. Reference: <id>" within 5 s (2 s once
  the app knows) with one WARNING line and no traceback, on every route; the app starts and
  stays alive while Postgres is unreachable (`/health` 200, `/ready` 503) and becomes ready
  once it answers. New metric `agent_database_up`. The pool checkout timeout is 5 s (was 10).
- A startup WARNING when an API's `limits.max_calls_per_run` cannot be reached within
  `RECURSION_LIMIT` (it names the value needed).
- **Untrusted tool output**: `app_utils.content.UntrustedToolResults`, wired into the
  generated agent next to `SurfaceApiErrors` (`agent.middleware()`), fences every tool
  result the model reads in `<tool_output ... trust="untrusted">` tags on both runtimes,
  whatever the result's text looks like (tags inside it are renamed, so it cannot close the
  fence or forge one); the default `SYSTEM_PROMPT` says tool output is data, never
  instructions. Helpers for tools in `app_utils.api_client`: `require_user_mentioned`,
  `require_owner`, `current_caller`, `latest_user_message`.
- **A2A**: `A2A_DESCRIPTION` sets the card's description (and its chat skill's) and is in the
  chart values; `AGENT_VERSION` sets the card version. `SendMessage` returns the reply as one
  text part; streamed replies mark the last chunk `lastChunk`, and the stored task holds one
  part. A message with no text, an empty text part, a non-user role or over
  `MAX_MESSAGE_CHARS` is -32602, and an A2A 0.3 request that fails the SDK's validation
  (a JSON-escaped method name, an unpaired surrogate, a missing `messageId`) is answered
  -32602 or -32600 naming the fields, never the values, with no traceback.
- `/chat` without a `thread_id` starts a thread with a server-generated UUID4; `DELETE
  /threads/{id}` and the retention purge (both runtimes) drop the thread's A2A tasks.
- **eval**: judges of a multi-turn case see every earlier turn (user message, each tool call
  with its result, the agent's reply) and a new `{transcript}` placeholder; a tool result is
  cut for the judge at `judge.max_tool_result_chars` (default 50000, `null` never; was a
  silent 2000) with a `[TRUNCATED ...]` marker, a warning and `judge_notes`;
  `expect.case_insensitive`; `expect.scope: final_turn | all_turns`; results add
  `quality.<m>.scored`, `passed` and `status`, `fake_model` and `warnings`; `eval grade`
  warns when the agent or the judge ran on the fake model ("gate met ... (fake model:
  plumbing check only, not a quality signal)"), when a `--url` run's project settings name
  the fake model, and when `all_turns` checks had to read the final turn only; `eval
  generate --url` / `eval run --url` warn before the first case that tools run for real
  there, naming the write methods `api-policy.yaml` allows; trace files record `target` and
  `model_provider`; `eval metric list` shows the check modifiers.
- **deploy**: a failed rollout that is rolled back (or a first install that is uninstalled)
  puts the app Secret and `<release>-metrics` back to their values from before the run, keys
  the run removed included, or deletes a Secret the run created; a Secret changed by someone
  else meanwhile is left alone (and the metrics Secret with it, so both keep the same
  `METRICS_TOKEN`), and the error says what happened. `deploy --status` (default `--timeout`
  60s) prints replicas, image, helm revision and each pod's state; failed-deploy diagnostics
  show only this release's warning events since the run started; a redeploy of the running
  image says what will happen and advises `--restart` when only the Secret changed;
  `--dry-run` reads the live Secret and refuses a missing required key like the real run.
  An external DSN without `sslmode=require|verify-ca|verify-full` is a warning outside dev.
- Chart: a `preStop` pause (`shutdown.preStopSleepSeconds`, 5) and a bounded drain
  (`shutdown.drainSeconds`, 20, as `UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN` or
  `BG_JOB_SHUTDOWN_GRACE_PERIOD_SECS`), so in-flight requests end before the kill (fastapi:
  runs end `cancelled` and release their leases); the dev Postgres stops in fast mode; a
  NetworkPolicy example (`examples/networkpolicy.yaml`, not packaged) that values-staging and
  values-prod point to.
- `infra check` rows: `auth: jwt settings`, `metrics token secret <name>-metrics`, `database
  tls (<KEY>)`; a disabled gateway is one skip row.
- `run`: after an `error` event the footer prints the run, the thread and the resume command;
  a dropped stream says the run started and was interrupted (and whether the thread
  survived: a stopped local server with an in-memory checkpointer loses it); `run -v`
  prints one compact line per event.
- README: an "External database" section (a least-privileged role, `sslmode=verify-full`,
  the CA mount).

### Changed

- `setup` installs the skills from this repository at the tag of the running release
  (`https://github.com/ss7172/graph-agents-cli#v<version>`; the default branch only for a
  development build), so they cannot drift from the CLI when the default branch moves on;
  `update` moves them to the tag of the release it installs.
- `scaffold enhance` no longer lists `--api-policy` in its help (it refuses the flag) and
  points to `graph-agents-cli api` for changing the policy.
- The printed "Get Started" after `create` includes `cp .env.example .env` and
  `graph-agents-cli login --write-env`, so a first `eval run` does not fail with 503.
- With no `--registry` and no git `origin` remote, `create` still records the placeholder
  `ghcr.io/CHANGE-ME`; its hints, the generated README, `deploy`, `build` and `infra check`
  now name `graph-agents-cli scaffold enhance --registry <host>/<org>`, which sets the
  registry everywhere it is read (the manifest, the chart values, `.github/agent.env`).
- A `TRACE_CAPTURE` other than `metadata` or `full` stops the app at startup, like the other
  settings that do not parse (0.1.0 read it as `metadata`); so does an `A2A_TASK_TTL_S` that
  is not a whole number >= 0.
- `scaffold upgrade --baseline current` labels its "Will preserve" list as files that differ
  from the current template, and says that files you did not edit keep their old content and
  that dependency changes are not merged; when the authentic baseline fails, the hint that
  suggests `--baseline current` warns about this too.
- `login --write-env` fills a blank `KEY=` line in place (no duplicate lines), keeps `.env`
  at mode 0600 and writes it atomically.
- `extension add` accepts a local path without the `local@` prefix; `extension update`
  reports "Already up to date".
- `lint` checks every `*.py` under `<agent>/tools/`, subpackages included, and reports any
  `API_CALLS` it cannot read as one literal (`+=`, `.append()`, a conditional assignment).
- Backups under `~/.graph-agents-cli/backups` are private (0700, `.env*` files 0600) and only
  the newest 5 per project are kept.
- Images: the fastapi image is multi-stage on `python:3.12.14-slim-bookworm` with a pinned
  uv and no uv in the final image; the server image is `langchain/langgraph-api:0.14.4-py3.12`
  (checked against `uv.lock` at build time) with its unauthenticated meta routes disabled;
  both run as uid/gid 1000 and work with a read-only root filesystem. Both make
  `api-policy.yaml` readable by that user whatever its mode in the working tree.
- Client `/chat` metadata is kept in the run record only: never written into checkpoints,
  and exported to traces only under `TRACE_CAPTURE=full`.
- Log warnings print as `Warning: ...` instead of `WARNING:root:...`.
- **Logs** (both runtimes): access lines keep the path and drop the query string (under
  `langgraph-server` the server's access lines lose their `query_string` field); `httpx`,
  `httpcore`, `httpx2` and `httpcore2` log at WARNING only (their INFO lines carry full
  outbound URLs), and `api_client` logs each outbound call as `api call done: <api> <METHOD>
  <operation|template> -> <status> (<ms> ms)`; Python warnings are records (JSON under
  fastapi) with pydantic's `input_value` redacted; a failed tool call logs one WARNING with
  its error id, tool name and error type.
- `.env` is applied when the app module is imported (below the process environment), so the
  A2A card's auth scheme, `A2A_NAME`, the dev-only `/docs` and `CORS_ALLOW_ORIGINS` follow it
  under `uvicorn` too; `PYTHON_DOTENV_DISABLED` switches it off.
- The fake model calls whichever bound tool the request mentions (not only `get_weather`)
  and echoes a tool's own text (without the untrusted-data fence). Generated projects' tests
  depend on none of the project's tools, its `.env` or the developer's shell: a
  `tests/conftest.py` strips app settings, server tests serve a graph without the project's
  tools (`@pytest.mark.project_graph` opts out) and use test-only tools through
  `use_test_tools`.
- `run` and `eval` help lead with `GRAPH_AGENTS_CLI_API_KEY` for bearer credentials (argv is
  visible to other local users); 401 hints depend on the project's policy; `eval generate`
  prints the same hint.
- `api show`, `api check` and `lint` tables grow to 250 columns in piped output instead of
  cutting cells; `api add --openapi` prints the policy diff first and summarises the copied
  spec; `api allow` of an entry that allows nothing yet says so.
- Deploy mode labels say "local cluster" (was "dev cluster"); generated projects git-ignore
  `deployment/helm/*/Chart.lock`.
- `login --write-env` and `auth dev-token` never assign a key `.env` already sets, and
  concurrent writers take turns (a lock in `.graph-agents-cli/`); `login --write-env` leaves
  `.env` at 0600.

### Deprecated

- The hidden `run` / `eval` option `--session-token`: pass `--header 'X-Session-Token: ...'`.
  It prints a warning and will be removed.

### Removed

- `app/app_utils/product_client.py` and `tools/product_lookup.py` from the template (replaced
  by `api_client.py` and `tools/example_api.py`).

### Fixed

- The `pr_checks` workflow wrote comment lines of `agent.env` into `GITHUB_ENV` and failed.
- `deploy` created no namespace before applying the Secret on a first deploy.
- Server runtime: "thread not found" is 404, not 503; run records are durable in Postgres.
- The schema setup races between replicas (now under an advisory lock) and the pool did not
  recover after a Postgres restart (connections are health-checked).
- Thread ownership is claimed atomically; thread ids are validated.
- The argocd staging workflow could re-trigger itself on its own values commit.
- Local-load (kind, k3d, minikube, k3s) was chosen from the kube context's name alone; it is
  now decided from the cluster's nodes, confirmed with the kind, k3d or minikube listing.
- `run`, `eval generate` and `playground` left their local server running after SIGTERM or
  SIGHUP.
- `scaffold enhance --runtime/--model-provider` left chart values, `.env.example` and
  `secrets.keys` on the old settings.
- A thread whose tool call was cut short failed every later turn with a provider 400; the
  langgraph-server repair now sends a remove-all update the real server accepts.
- A database outage made requests hang for about a minute with tracebacks, and a replica
  started during an outage exited (crash loop); `/ready` took over a minute to recover.
- Under `langgraph-server` a run that reached the step limit ended with an error instead of
  its reply (the server marks the run done after sending the error; the reply now waits
  for it briefly).
- A2A replies were split into one part per streamed token.
- `run`'s drop and timeout messages offered to resume threads a stopped in-memory server had
  lost, and "the answer above is incomplete" when nothing had been shown.
- `sslmode = verify-full` (spaces around `=`, which libpq accepts) was reported as no TLS.

### Security

- `setup`, `update`, the `scaffold upgrade` baseline (through `uvx`) and the documented
  install no longer use the unpublished PyPI name `graph-agents-cli`: whoever registered it
  would have had their code run on users' machines. Everything installs from a pinned git tag
  of this repository.
- The outbound API policy cannot be widened by accident: without a policy every call is
  refused; unknown and repeated YAML keys are errors; a denial pinning a path refuses every
  call to it whatever `operation_id` the call gives (a relabelled or misspelt call no longer
  reaches a denied endpoint), and a call that leaves out what a denial knows the operation by
  is refused by it; with an OpenAPI spec, `lint` refuses a declared `operation_id` the spec
  does not give that method and path; `/admin/1/`, `/ADMIN/1` and `/%61dmin/1` no longer get
  past a denial of `/admin/{x}`; the page-size cap holds for every spelling of the
  parameter; `lint` reads tool subpackages and reports `API_CALLS` it cannot read instead of
  trusting it.
- A2A tasks are private to the principal that created them (another principal's task reads
  as not found); the in-memory task store evicts tasks after `A2A_TASK_TTL_S`.
- LangGraph Server native API: assistants, crons and store writes need `AUTH_ADMIN_ROLES`;
  a default-deny handler covers every other resource; thread owners cannot transfer a thread
  or set its tenant; read-across roles cannot copy another principal's thread; a 401 carries
  the policy's challenge and a policy 503 stays a 503.
- LangGraph Server: the run context carries only `public_attributes()` (credentials were
  persisted before), and `/chat` run metadata (so traces and checkpoint metadata) carries the
  hashed principal id instead of the raw one.
- The server image disables LangGraph Server's unauthenticated `/docs`, `/openapi.json`,
  `/info` and `/metrics`; `/metrics` can require `METRICS_TOKEN`.
- Secrets are applied with server-side apply (no `last-applied-configuration` annotation
  holding the values; an old one is removed), and a generated `API_KEY` is never printed.
- `.github/agent.env` can no longer inject environment variables (`BASH_ENV`, `PS4`,
  `SHELLOPTS`, ...) into workflow steps; `GRAPH_AGENTS_CLI_INSTALL_SPEC` with control
  characters or stray whitespace is refused.
- `helm-push` kubeconfig moved to environment secrets behind the production gate and is
  removed from the runner after the job.
- Workstation `helm-push` deploys to staging and prod cannot bypass CI with `--image`.
- A delete racing a chat turn can no longer leave ownerless checkpoints another principal
  could claim; the retention purge re-checks idleness under the thread lock.
- `NaN`/`Infinity` in a request body gives 422 instead of a 500 with a traceback.
- The chart runs pods as non-root with a read-only root filesystem, dropped capabilities
  and no service-account token, and keeps probes and metrics off the public route.
- `APP_ENV` counts as dev (dev-only pages, the error `detail`, an optional jwt issuer and
  audience, the chart's dev paths) only when it is exactly `dev`; 0.1.0 also accepted any
  case and surrounding whitespace.
- **Prompt injection through tool results** is reduced: results are fenced as untrusted data
  (see Added) and write tools have `require_user_mentioned` / `require_owner`. It is not
  prevented; see "Known limitations" and the langgraph-code skill (section 2a).
- Query strings (a token a client put in the URL) and outbound URLs with their values stay
  out of the logs on both runtimes; `repr()` of `Principal` and of the run context no longer
  shows forwarded credentials; a database URL that does not parse is reported without its
  text (it can hold the password).
- `eval` never prints or stores the credentials of a `--url` (`***@` in errors, traces and
  results).
- Thread ids are one namespace: the server generates random ids when a client names none,
  and the docs say to use unguessable ones (an id another principal used first is theirs).

## [0.1.0] - 2026-09-23

First public import of graph-agents-cli, a fork of google-agents-cli 1.6.1 with the Google
Cloud specific parts removed (see NOTICE). It is commit `fc3f2f9` on `main`, with no GitHub
Release; the release process tags that commit `v0.1.0` so `scaffold upgrade` can rebuild a
0.1.0 project's baseline.

### Added

- Commands: `setup`, `update`, `login`, `create` / `scaffold create|enhance|upgrade`,
  `playground`, `run`, `install`, `lint`, `build`, `eval run|generate|grade|compare|analyze|
  submit|metric list`, `deploy`, `secrets apply|status`, `infra check`,
  `extension add|list|remove|update`, `info`.
- One LangGraph template with two runtimes (`fastapi`, `langgraph-server`), four model
  providers (OpenAI, Anthropic, Gemini, OpenAI-compatible) and a deterministic `fake` model
  for tests; `POST /chat` (SSE), A2A, `/playground`; Postgres or in-memory checkpointer.
- A Helm chart with `dev`, `staging` and `prod` values; CD modes `skip`, `helm-push` and
  `argocd`; allow-listed Kubernetes Secrets.
- A local eval harness with deterministic checks, an LLM judge and an enforceable gate;
  optional LangSmith upload.
- Six coding-agent skills, bundled in the wheel.

[Unreleased]: https://github.com/ss7172/graph-agents-cli/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/ss7172/graph-agents-cli/releases/tag/v0.2.0
[0.1.0]: https://github.com/ss7172/graph-agents-cli/commit/fc3f2f9
