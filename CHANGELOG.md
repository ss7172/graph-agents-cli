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
- **A live `API_KEY` is never replaced implicitly.** `secrets apply` and `deploy` keep the
  key in the cluster unless the env file sets a different one **and** `--rotate-api-key` is
  passed. A generated key is written to the env file (mode 0600) instead of being printed.
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
  `api deny` (by operationId, filled in from the API's OpenAPI spec when it has one, or by
  `--method`/`--path`), `api revoke`, `api limits`, `api remove`, `api show [--json]` and `api
  check` (same as `lint --policy-only`). Every change validates the result, keeps comments
  and key order, prints a unified diff of each file it touches (the policy, the manifest's
  `api_policy` and `secrets.keys`, `.env.example`, the chart's `values.yaml`), writes
  atomically, says whether it widens or narrows access and how the tools' declared calls are
  affected; `--dry-run` prints the diff only; exit 3 on an invalid result or outside a
  project. `lint` prints the `graph-agents-cli api` command that would allow each refused
  call.
- **Outbound call limits**: optional per-API `limits: {max_calls_per_run, rate_per_minute}`.
  `max_calls_per_run` counts the calls to that API within one agent run (the LangGraph run
  id, else the request's); `rate_per_minute` is a token bucket per process (per replica).
  A call over a limit is refused before it is sent, with a reason the model can read;
  a run's counters are dropped when a `/chat` run ends, or after an hour without a call.
- The `approval` key is reserved on an API and on an operation entry for human approval of
  calls (planned) and refused until then ("approval gates are not supported yet (planned);
  remove the approval key"), so a policy never counts on a gate that does not exist.
- **Endpoints**: `GET /ready` (readiness: the database answers within 2 s), `GET /metrics`
  (Prometheus: request count and latency, runs by status, active runs, run duration, tokens;
  optional `METRICS_TOKEN`), `GET /threads` (the caller's threads) and `DELETE
  /threads/{thread_id}`.
- **Runtime guardrails**: one run per thread (409 `{"code": "thread_busy"}`, with a Postgres
  advisory lock across replicas), `RUN_TIMEOUT_S`, `MODEL_TIMEOUT_S`, `MODEL_MAX_RETRIES`,
  `RECURSION_LIMIT`, `MAX_REQUEST_BYTES` (413), `MAX_METADATA_KEYS` and
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
  (from the app Secret by default), so a token-protected `/metrics` can be scraped.
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
  both run as uid/gid 1000 and work with a read-only root filesystem.
- Client `/chat` metadata is kept in the run record only: never written into checkpoints,
  and exported to traces only under `TRACE_CAPTURE=full`.
- Log warnings print as `Warning: ...` instead of `WARNING:root:...`.

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

### Security

- `setup`, `update`, the `scaffold upgrade` baseline (through `uvx`) and the documented
  install no longer use the unpublished PyPI name `graph-agents-cli`: whoever registered it
  would have had their code run on users' machines. Everything installs from a pinned git tag
  of this repository.
- The outbound API policy cannot be widened by accident: without a policy every call is
  refused; unknown and repeated YAML keys are errors; a denial applies to a call that does not
  name the field it pins; `/admin/1/`, `/ADMIN/1` and `/%61dmin/1` no longer get past a denial of
  `/admin/{x}`; the page-size cap holds for every spelling of the parameter; `lint` reads tool
  subpackages and reports `API_CALLS` it cannot read instead of trusting it.
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
