# graph-agents-cli

A command-line tool and a set of coding-agent skills for building, evaluating and deploying
[LangGraph](https://langchain-ai.github.io/langgraph/) agents on self-hosted Kubernetes.

It is generic: use it for any project, in any domain. It scaffolds a LangGraph project with a
streaming chat API, an A2A endpoint, per-user or shared-key authentication, a policy for the
external APIs the agent's tools may call, an eval harness with an enforceable gate, a
hardened Helm chart and GitHub Actions workflows. It then runs and evaluates the agent
locally and deploys it to any Kubernetes cluster with Helm, either directly or through
Argo CD. Six bundled skills teach a coding agent (Claude Code, Codex, Gemini CLI, Cursor,
Antigravity and others) the same lifecycle.

Nothing in the CLI or the template is specific to one consumer. A project chooses its auth
policy (`shared-bearer`, `jwt` or `custom`), declares its outbound APIs in `api-policy.yaml`,
and configures everything else through environment variables and chart values.

graph-agents-cli is a fork of [google-agents-cli](https://github.com/google/agents-cli) with
the Google Cloud specific parts removed (see [NOTICE](NOTICE)). Version 0.2.0, alpha: the
interfaces below may still change between minor versions; [CHANGELOG.md](CHANGELOG.md)
lists every breaking change with its migration steps.

**Contents:** [Install](#install) · [Quick start](#quick-start) · [Commands](#commands) ·
[The generated service](#the-generated-service) · [Authentication](#authentication) ·
[Outbound API policy](#outbound-api-policy-api-policyyaml) · [Evaluation](#evaluation) ·
[Environments and CD modes](#environments-and-cd-modes) · [Secrets](#secrets) ·
[Exit codes](#exit-codes) · [Security model](#security-model) ·
[Production checklist](#production-checklist) ·
[Compared with google-agents-cli](#compared-with-google-agents-cli) ·
[Known limitations](#known-limitations)

## Install

Prerequisites: Python 3.12 or 3.13 and [uv](https://docs.astral.sh/uv/getting-started/installation/).
Node.js is needed only for the skills installer (`setup` falls back to a plain copy without
it). Deploying needs `helm`, `kubectl`, a Docker-compatible `docker` CLI and `git`, plus `gh`
for Argo CD or GitHub-hosted CD; a tool missing from `PATH` makes `deploy` exit 2.

```bash
uv tool install git+https://github.com/ss7172/graph-agents-cli@v0.2.0
graph-agents-cli setup      # optional: install the skills into your coding agents
```

The CLI is installed from a pinned git tag of this repository. **Publication on PyPI is
pending**: until this repository's release workflow publishes it, a package named
`graph-agents-cli` on an index is not this project, so install from the git tag.

Optional extras: `run --mode a2a` needs `a2a`, `eval submit` needs `langsmith`:

```bash
uv tool install 'graph-agents-cli[a2a,langsmith] @ git+https://github.com/ss7172/graph-agents-cli@v0.2.0'
```

`GRAPH_AGENTS_CLI_INSTALL_SPEC` overrides where `setup`, `update`, the `scaffold upgrade`
baseline and generated projects' CI (`GRAPH_AGENTS_CLI_SPEC` in `.github/agent.env`) install
the CLI from: a private mirror, a wheel, or a package index once one is used. Write
`{version}` where the version goes (`git+https://git.example.com/graph-agents-cli@v{version}`)
so the upgrade baseline can install an older release; an override without it is refused for
that (exit 3). `{version}` is a release number (the `cli_version` a project records), so the
override names releases only; a build between two releases is named with `scaffold upgrade
--baseline-ref` (see [Upgrading a project](#upgrading-a-project)). An override with control
characters or whitespace is refused (exit 3), except the spaces of a PEP 508 `name @ url`
reference.

`graph-agents-cli --version` names the build: `0.2.0` for the release, `0.2.0+g<commit>` for a
build of any other commit (a checkout between releases) and `0.2.0+g<commit>.dirty` with
uncommitted changes; `info` shows the full commit. To install a build from a checkout, see
CONTRIBUTING.md ("Installing a build from a checkout"): uv rebuilds a checkout whenever its
commit or sources change.

`setup` installs the skills with `npx skills add` from this repository at the tag of the
running release (`https://github.com/ss7172/graph-agents-cli#v0.2.0`), so they match the CLI
even after the default branch moves on; a development build (a source checkout) uses the
default branch. When that fails it falls back to the copy bundled in the wheel (the same
version) and then to a plain copy into `~/.agents/skills` (`./.agents/skills` with
`--workspace`), so it also works without git or network. `--skills-source` picks another
source (a path, `owner/repo`, or a URL with `#<ref>`); `update` moves the skills to the tag
of the release it installs. The CLI stores no credentials;
`login` only checks the environment. The CLI checks GitHub for a newer release at most every
12 hours; `GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1` turns that off.

## Quick start

This runs a new agent locally. It works without any model key when you pick the
deterministic test model (`MODEL_PROVIDER=fake`), as the comment shows.

```bash
graph-agents-cli create my-agent        # fastapi runtime, kubernetes target, cd: skip
cd my-agent
cp .env.example .env                    # set OPENAI_API_KEY, or MODEL_PROVIDER=fake to try it keyless
graph-agents-cli login --write-env      # checks the setup; prompts for missing keys, generates API_KEY
graph-agents-cli install                # uv sync from the bundled lock
graph-agents-cli run "What's the weather in San Francisco?"
graph-agents-cli eval run               # generate traces, grade them, enforce the gate
graph-agents-cli playground             # http://127.0.0.1:8000/playground (Ctrl-C to stop)
```

`run` starts a temporary local server (the first free port of 18080-18089), sends the
prompt to `POST /chat` with the `API_KEY` from `.env`, prints the streamed answer and stops
the server. `eval run` does the same for every case in `tests/eval/datasets/` and exits 0
when the gate is met. With a real provider, keep `MODEL_PROVIDER` as generated and put the
key in `.env` (`login --write-env` prompts for it without echoing).

A `jwt` project (`create my-agent --auth-policy jwt`) needs a token on every request. For local
runs, after `install`:

```bash
export GRAPH_AGENTS_CLI_API_KEY="$(graph-agents-cli auth dev-token --sub alice --roles user)"
graph-agents-cli run "What's the weather in San Francisco?"
graph-agents-cli eval run
```

`auth dev-token` keeps a dev key pair in `.graph-agents-cli/dev-jwt/` (git ignored), fills the
blank `AUTH_JWT_PUBLIC_KEY`, `AUTH_JWT_ISSUER` and `AUTH_JWT_AUDIENCE` in `.env`, and prints a
token; it refuses unless `APP_ENV=dev`. `run` and `eval` send `GRAPH_AGENTS_CLI_API_KEY` as the
bearer, which keeps the token out of the process list and your shell history. `login` reports
whether the key and the token are in place.

The example tool (`app/tools/weather.py`) and its eval case are starting points: replace or
delete them. The project's own tests use a test-only tool and read neither `.env` nor your
shell's app settings, so they keep passing as the agent changes.

Useful `create` options: `--runtime fastapi|langgraph-server`,
`--model-provider openai|anthropic|gemini|openai-compatible`, `--model`,
`--checkpointer memory|postgres`, `-d/--deployment-target kubernetes|none`, `--registry`,
`--cd skip|helm-push|argocd`, `--auth-policy shared-bearer|jwt|custom`,
`--api-policy <file>` (seed the outbound API policy; or add APIs later with
`graph-agents-cli api add`), `--process <path>`, `-p/--prototype` (no deployment files). Ask
your coding agent to "use graph-agents-cli to build ..." and the `graph-agents-cli-workflow`
skill walks the same steps.

To deploy to a local cluster (kind, k3d, minikube, Docker Desktop), see
[Environments and CD modes](#environments-and-cd-modes): `graph-agents-cli deploy --env dev`
builds the image, side-loads it and runs `helm upgrade --install` on the kubeconfig's
current context. The image needs a registry name: `create` takes it from `--registry` or
from the git `origin` remote (`ghcr.io/<owner>`). Without either, as in the quick start
above outside a git repository, it records the placeholder `ghcr.io/CHANGE-ME`, which
`build` and `deploy` refuse (exit 3). Set it with
`graph-agents-cli scaffold enhance --registry <host>/<org>`: that updates
`create_params.registry` in the manifest (what `build` and `deploy` read), `image.repository`
in the chart values and `IMAGE_REPOSITORY` in `.github/agent.env`. A side-loaded image never
leaves your machine, so any valid name works for a local cluster (`--registry localhost/dev`).

## Commands

Run `graph-agents-cli <command> --help` for every flag; each help page ends with a `Source:`
line naming the file that implements it.

| Command | What it does |
|---|---|
| `setup [--workspace] [--dry-run] [--dev] [--skills-source TEXT] [--agent TEXT]...` | Install the CLI (`uv tool install` of the pinned spec) and the skills into detected coding agents |
| `update [--workspace] [-i] [-y]` | Reinstall the skills and the CLI from the latest GitHub release |
| `login [--profile default\|disconnected] [--cluster] [--write-env] [--env-file FILE] [--status] [--json]` | Preflight: provider key or `OPENAI_BASE_URL`, `API_KEY` under `shared-bearer`, the verification key and the local token under `jwt`, `LANGSMITH_API_KEY` when tracing is on, a `.env` other users can read, kubeconfig. `--write-env` prompts for missing keys (never echoed), generates `API_KEY`, fills blank `KEY=` lines in place and leaves `.env` at 0600 (even when nothing is missing). Exit 1 on a failed check (0 with `--status`) |
| `auth dev-token --sub USER [--roles R,...] [--ttl 12h]` | A JWT for local runs of a `jwt` project, printed alone for `GRAPH_AGENTS_CLI_API_KEY`: a dev key pair in `.graph-agents-cli/dev-jwt/`, blank `AUTH_JWT_PUBLIC_KEY` / `AUTH_JWT_ISSUER` / `AUTH_JWT_AUDIENCE` filled in `.env`. Refused (exit 3) unless the policy is `jwt` and `APP_ENV` is exactly `dev`, or when `.env` names a JWKS URL or another public key |
| `create [NAME]` / `scaffold create [NAME]` | Create a project: `-a/--agent`, `-o/--output-dir`, `--runtime`, `--model-provider`, `--model`, `--checkpointer`, `-d/--deployment-target`, `--registry`, `--cd`, `--auth-policy`, `--api-policy FILE`, `--process`, `-p/--prototype`, `-dir/--agent-directory`, `--agent-guidance-filename` (default `AGENTS.md`), `-bt/--base-template`, `-i`, `-y`, `-s/--skip-checks`, `--debug` |
| `scaffold enhance [TEMPLATE_PATH]` | Add or change the deployment target, CD mode, runtime or model provider of an existing project (the `create` flags except `--api-policy`, which it refuses (the policy changes through `api`), plus `-n/--name`, `--force`, `--dry-run`, `--prefer-new`). 3-way merge after a backup; exit 1 when steps marked `(required)` are left for you |
| `scaffold upgrade [PROJECT_PATH] [--dry-run] [-y] [-i] [--baseline authentic\|current] [--baseline-ref REF] [--debug]` | Upgrade a project to this CLI build with a 3-way merge against the templates of the exact build that created it (its release, the commit the manifest's `cli_build` records, or `--baseline-ref`); stops unchanged when that baseline cannot be built (exit 2; exit 3 when the manifest, the install-spec override or `--baseline-ref` is the reason). See [Upgrading a project](#upgrading-a-project) |
| `playground [--port INT] [--graph] [--no-open]` | Run the app with reload and the dev chat page (`/playground`, port 8000, refused when the port is taken); `--graph` opens LangGraph Studio (bypasses the auth policy) |
| `run MESSAGE [--mode chat\|a2a] [--url URL] [--thread-id ID] [-H/--header]... [--cookie]... [-f/--file]... [--start-server] [--stop-server] [--port INT] [-v]` | Send one prompt to a local server (started on demand) or a deployed URL. A bearer credential goes in `GRAPH_AGENTS_CLI_API_KEY`, not `--header`; the footer names the thread and how to resume it, after an error too; `-v` adds one line per event. A run paused on a gated call prints the call; on a terminal, when the requester is an approver, it asks `Approve? [y/N]` and continues, otherwise it prints the `approvals` commands and exits 0 (see [Human approval of calls](#human-approval-of-calls-approval)) |
| `approvals list [--thread-id ID] [--all] [--json]` / `approvals approve\|reject APPROVAL_ID [--thread-id ID] [--comment TEXT] [-v]` (each with `[--url URL] [-H]... [--cookie]...`) | List the calls runs are waiting on (a thread's, or every one you may see: your own and the ones a role of yours may decide), or decide one: the call is shown first, the decision carries only `approve`/`reject` and the comment, and the resumed run streams. Same credentials as `run`; exit 1 when the server refuses (not an approver, already decided, expired, not found) |
| `install [--clean] [--locked]` | `uv sync` the project |
| `lint [--fix] [--policy-only]` | `ruff check`, `ruff format --check` and the API-policy check (`api-policy.yaml` against the strict schema, every tool's `API_CALLS` against it; a refused call comes with the `api` command that would allow it) |
| `api add NAME --base-url-env ENV --auth none\|bearer\|forward [--token-env ENV] [--forward-header H] --access read-only\|read-write\|custom [--methods M,...] [--openapi PATH] [--max-calls-per-run N] [--rate-per-minute N] [--connect-timeout-ms N] [--read-timeout-ms N] [--dry-run]` | Declare an outbound API (creates `api-policy.yaml` when absent); `--access` is required, there is no default |
| `api access NAME read-only\|read-write\|custom [--methods M,...] [--dry-run]` | Set the methods an API allows |
| `api allow\|deny NAME (OPERATION_ID [--method M --path P] \| --method M --path P) [--methods M,...] [--dry-run]` | Add an `allowed_operations` / `denied_operations` entry pinning every field given (`--methods` for `allow` only) |
| `api revoke NAME (OPERATION_ID \| --method M --path P) [--from allowed\|denied] [--dry-run]` | Remove the matching entries (with `--method`, only that method of each) |
| `api limits NAME [--max-calls-per-run N\|none] [--rate-per-minute N\|none] [--dry-run]` / `api remove NAME [--dry-run]` | Set or clear call limits / remove an API |
| `api approval NAME [--methods M,...\|none] [--operations OP,...\|none] [--approvers requester,role:NAME,...] [--timeout-s N] [--add-rule \| --rule N] [--remove] [--dry-run]` | Require a human approval before some of an API's calls are sent (each option given replaces that part of the rule); `--add-rule` adds a rule for calls other approvers decide, `--rule N` changes or removes rule N; says when a change loosens the gate, which is a reviewed change |
| `api show [NAME] [--json]` / `api check` | The effective policy, each tool's declared calls and which of them wait for whose approval / the policy check (`lint --policy-only`) |
| `build [--tag TEXT] [--registry TEXT] [--push] [--dry-run]` | `docker build` with the runtime's Dockerfile (default tag `latest`); a placeholder or invalid registry is exit 3 |
| `eval run [--dataset] [--url] [--concurrency] [-H]... [--cookie]... [--app-name] [--timeout] [--config] [-o] [--judge-provider] [--judge-model] [--judge-timeout]` | `eval generate` then `eval grade`; the exit code is the gate |
| `eval generate [--dataset] [-o] [--url] [--concurrency] [-H]... [--cookie]... [--app-name] [--timeout]` | Run the agent over `tests/eval/datasets/*.json` and write `artifacts/traces/` |
| `eval grade [--traces] [--dataset] [--config] [-o] [--judge-provider] [--judge-model] [--judge-timeout]` | Deterministic checks, then judge and custom metrics in the project's environment; write `artifacts/grade_results/` |
| `eval compare BASELINE CANDIDATE [--fail-on-regression] [--json]` | Diff two results files |
| `eval analyze [--results] [--output] [--top-k] [--judge] [--judge-provider] [--judge-model]` | Cluster failures deterministically (judge summaries with `--judge`) |
| `eval submit [--results] [--traces] [--dataset] [--dataset-name] [--experiment] [--endpoint]` | Upload the dataset and results to LangSmith (`langsmith` extra) |
| `eval metric list [--json]` | List deterministic checks, built-in judges and the project's metrics |
| `deploy --env ENV [--image REF] [--env-file FILE] [--context NAME] [-y/--yes] [--status] [--restart] [--force-direct] [--dry-run] [--tag TAG] [--timeout DURATION] [--atomic/--no-atomic] [--rotate-api-key]` | Deploy per the project's CD mode (see below); never runs helm in an Argo CD environment |
| `secrets apply --env ENV [--env-file FILE] [--context NAME] [-y/--yes] [--rotate-api-key] [--dry-run]` | Create or update the `<release>-app` Secret from the allow-listed keys of an env file (server-side apply; values are never printed) |
| `secrets status --env ENV [--context NAME] [--strict] [--dry-run]` | Which allow-listed keys the Secret holds (never values); exit 1 when a required key is missing |
| `infra check [--env ENV] [--profile disconnected] [--json]` | Read-only report of cluster, repository and placeholder prerequisites; creates nothing |
| `extension add REFERENCE [--global] [--ref] [-i] [-y]` / `list` / `remove NAME` / `update [NAME]` | Manage command overrides and additions from extension repositories or local paths (experimental). New third-party code needs a trust prompt or `-y` (never prompted without a terminal); `update` asks only when the code changed |
| `info [--json]` | Project configuration (with the version and build that scaffolded it), paths, extensions, CLI version and build |

CLI environment variables: `GRAPH_AGENTS_CLI_INSTALL_SPEC` (install source),
`GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1` (no GitHub release check), `GRAPH_AGENTS_CLI_API_KEY`
(the bearer credential `run` and `eval` send, locally and with `--url`, when no `Authorization`
header is given: an `API_KEY` or a JWT; it keeps the credential out of argv and shell history),
`GRAPH_AGENTS_CLI_APPROVER_API_KEY` (the bearer credential `eval generate` decides `role:`
gates with; a gate that lists `requester` is decided as the eval identity), `GRAPH_AGENTS_CLI_RUN_PORT` (port of
the local server `run` and `eval` start), `GRAPH_AGENTS_CLI_DEBUG=1` (tracebacks behind
one-line errors), `GRAPH_AGENTS_CLI_DISABLE_OVERRIDES=1` (ignore extension overrides; set in
every generated CI and CD job).

### Upgrading a project

`scaffold upgrade` re-renders the project's old templates with the build that created it and
merges the difference into the project: files you did not edit take the new templates, your
edits are kept, conflicts are listed (or resolved with `-i`). Preview with `--dry-run`, apply
with `-y`; a backup is written first.

The manifest names the old build. `cli_version` is the release; `cli_build` (written by
`create`, `enhance` and `upgrade`) is the build id `--version` prints, its commit and
`template_digest`, a digest of what that build renders for the project's settings. So:

- A project from an older release is rebuilt from the release tag `v<cli_version>`, or from
  the recorded commit when `cli_build` names a build between releases (`X.Y.Z+g<commit>`).
- A project of the running version is "already at version" when `cli_build` names this build,
  or another build that renders the same files (same `template_digest`); otherwise it is
  upgraded from the recorded commit.
- A manifest without `cli_build` (made before builds were recorded, for example by a
  pre-release 0.2.0 build) is compared by version only: `upgrade` says "already at version"
  and shows how to name its build.

`--baseline-ref REF` names the build that created the project and wins over the manifest:
a commit or tag of this repository (`1a2b3c4`, `v0.1.0`), `<clone>@<commit>` for a local
clone (the commit is looked up there first; the way to reach a commit that was never
pushed), a path to a checkout or wheel (rebuilt, never a stale uv cache), or a full install
spec (`git+https://git.example.com/graph-agents-cli@<commit>`, for a mirror). The baseline
must render the manifest's `cli_version` (exit 3 otherwise). Without a known commit, the
newest one before the project was generated is a first candidate; the build may be older (a
checkout behind its branch, or a stale uv build), so check it with `--dry-run`: with the
right build only files you edited are listed under "Will preserve" or as conflicts
(`upgrade` warns when most template files would keep their current content, the sign of a
later build named as the baseline).

```bash
git -C <clone> log -1 --format=%H --before='<generated_at from the manifest>'
graph-agents-cli scaffold upgrade --baseline-ref <clone>@<commit> --dry-run
graph-agents-cli scaffold upgrade --baseline-ref <clone>@<commit> -y
```

Afterwards the manifest records the running build, so the next upgrade needs no flag. A build
with uncommitted changes (`.dirty`) cannot be rebuilt: name the closest commit with
`--baseline-ref`. Release tags on the remote are created by the repository owner; until
`v<version>` exists there, the default baseline of that release cannot be fetched and
`--baseline-ref <clone>@<commit>` is the way to name it.

## The generated service

A project has one agent directory (`app/` by default):

```
app/agent.py              # exports `graph` (compiled LangGraph agent, no checkpointer bound)
app/fast_api_app.py       # exports `app`: the HTTP API below
app/app_utils/            # auth, api_client, chat, threads, db, limits, metrics, middleware, model, telemetry, a2a
app/policies/custom.py    # the custom auth policy (a fail-closed stub until implemented)
app/tools/                # every module declares API_CALLS and TOOLS
tests/{unit,integration,eval,load_test}
deployment/helm/<name>/   # chart and values-{dev,staging,prod}.yaml (kubernetes target)
api-policy.yaml           # outbound API policy (when declared)
.env.example              # the full environment contract, with defaults
AGENTS.md                 # guidance for coding agents
graph-agents-cli-manifest.yaml
```

Two runtimes share the same routes, auth and clients:

- **`fastapi`** (default): uvicorn serves `fast_api_app.py`; the checkpointer is `memory`
  locally and `postgres` in a cluster (`CHECKPOINTER`, `POSTGRES_DSN`).
- **`langgraph-server`**: the LangGraph Server image serves the graph and mounts the same app
  as custom routes (`langgraph.json` `http.app`); the server owns persistence
  (`DATABASE_URI`, `REDIS_URI`) and the same policy is its auth handler. See
  [Known limitations](#known-limitations) for its licence requirement.

### Endpoints

| Route | Auth | Behaviour |
|---|---|---|
| `POST /chat` | `chat.send` | Body `{"thread_id": "optional", "message": "...", "metadata": {}}`, `Accept: text/event-stream`. Events: `message.start`, `message.delta`, `tool.call`, `tool.result`, `message.end` (usage, latency, status) or `error`. Omit `thread_id` to start a thread (the server generates a random id, returned in `message.start`); send it to continue one (owner only). A run paused on a gated call ends with `message.end` status `awaiting_approval` and its `approval`; a new message on that thread gets 409 `{"code": "approval_pending"}` until it is decided |
| `GET /threads/{thread_id}/approvals` | owner, its approvers, read-across roles | The thread's approvals: the call (`api`, `method`, `path`, `query`, `body`, `operation_id`), `reason`, `approvers`, `status` (`pending`, `approved`, `rejected`, `expired`), `expires_at`, decision time and comment |
| `GET /approvals?status=&limit=&offset=` | any authenticated principal (`approval.read`) | Across threads, newest first: the caller's own approvals, the ones naming one of its roles (it may decide them), and every one for a read-across role; each row carries its `thread_id` |
| `POST /threads/{thread_id}/approvals/{approval_id}` | an approver (`approval.decide`) | Body `{"decision": "approve"\|"reject", "comment": "..."}`. Resumes the run and streams the rest as `/chat` does; 403 for anyone who is not an approver, 404 unknown, 409 not pending (decided once), 410 expired |
| `GET /threads` | `thread.list` | The caller's threads, most recent first: `?limit=1..100` (default 20) `&offset=`; `[{thread_id, owner, created_at, updated_at}]`, `owner` being the hashed principal id. `?scope=all` lists every principal's threads, for a role in `AUTH_READ_ACROSS_ROLES` only (403 otherwise); the default `scope=own` lists only the caller's, read-across roles included |
| `GET /threads/{thread_id}/messages` | `thread.read` | The thread's messages; owner, or a role in `AUTH_READ_ACROSS_ROLES` |
| `DELETE /threads/{thread_id}` | `thread.delete` | Deletes the thread, its checkpoints, run records, approvals and A2A tasks (owner only): 204, or 403, 404, 409 (a run in progress), 422. Under `langgraph-server` this is the server's own route, with the same owner rule |
| `GET /health` | none | Liveness, process only: `{"status": "ok", "runtime", "checkpointer"}` |
| `GET /ready` | none | Readiness: 200 `{"status": "ready"}` when the database (and run store) is set up and answers within 2 s, else 503 `{"status": "not_ready"}` |
| `GET /metrics` | none, or `METRICS_TOKEN` | Prometheus text (`METRICS_ENABLED`, default true): `http_requests_total`, `http_request_duration_seconds`, `agent_runs_total` (by status), `agent_active_runs`, `agent_run_duration_seconds`, `agent_tokens_total`, `agent_database_up`. With `METRICS_TOKEN` set, only `Authorization: Bearer <METRICS_TOKEN>` is answered |
| `/a2a/<agent>/.well-known/agent-card.json`, `POST /a2a/<agent>` | `card.read`, `a2a.invoke` | A2A agent card and JSON-RPC (A2A 1.0; 0.3 clients served on the same URL). Tasks are private to the principal that created them. `SendMessage` returns the reply as one text part (streamed replies arrive in chunks, the last marked `lastChunk`); a message with no text, an empty text part or over `MAX_MESSAGE_CHARS` is an invalid-params error (-32602); an unknown task is -32001 under both versions. The card's description is `A2A_DESCRIPTION`, its version `AGENT_VERSION` |
| `GET /playground`, `/docs`, `/openapi.json` | none | Only under `APP_ENV=dev` |

Behaviour:

- **One run per thread.** A second `/chat` on a thread with a run in progress gets 409
  `{"code": "thread_busy"}` (an in-process lock, plus a lease row in Postgres across replicas
  under `postgres`). The holder renews its leases every 5 s; a replica lost without closing
  its connections (a node failure, a partition, an OOM kill) frees its threads 30 s later,
  and a dropped database session (a Postgres restart or failover) keeps the lease. A run
  whose lease cannot be renewed is stopped (status `interrupted`) before it writes, so two
  replicas never run one thread at once.
- **Limits.** Bodies over `MAX_REQUEST_BYTES` (1 MiB) get 413; a message over
  `MAX_MESSAGE_CHARS` (32 000 characters) gets 422 on `/chat` and an invalid-params error
  over A2A (one cap for every surface that reaches the model); `/chat` metadata beyond
  `MAX_METADATA_KEYS` (16) keys or `MAX_METADATA_VALUE_CHARS` (256), with non-scalar values,
  or text that is not valid Unicode (an unpaired surrogate) gets 422. A 422 names the field
  and the rule, never the submitted value. Thread ids are 1-128 characters of
  `[A-Za-z0-9_.:-]` (UUIDs under `langgraph-server`).
- **Thread ids** are one namespace shared by every caller: an id another principal sent first
  is theirs (403 for everyone else), so a predictable id can be claimed ahead of its intended
  user, and a 403 reveals that an id is taken. Let the server generate ids (omit `thread_id`
  on the first turn), or generate unguessable ones (UUID4) in the client.
- **Timeouts.** A run is cancelled after `RUN_TIMEOUT_S` (300; status `timeout`); each model
  request has `MODEL_TIMEOUT_S` (60; 0 = provider default) and `MODEL_MAX_RETRIES` (2). Idle
  SSE streams get a `: keep-alive` comment every `SSE_HEARTBEAT_S` (15). A client disconnect
  cancels the run (status `cancelled`).
- **Step limit.** A run may take `RECURSION_LIMIT` (50) graph steps: two to answer and two
  more per tool call made after the previous one returned, so 24 sequential tool calls.
  A run that reaches it ends with a reply saying so and `message.end` status `step_limit`
  (not an `error`); everything it did stays in the thread, so "continue" picks up with a
  fresh budget. The app warns at startup when an API's `limits.max_calls_per_run` cannot be
  reached within the limit.
- **A valid history after any stop.** Model providers reject a tool call without its result.
  A timeout, a disconnect, a crash, an OOM kill or a database outage can leave one, so every
  run first answers its thread's open tool calls with an error result placed right after
  the call (and moves misplaced results back) before it adds its turn.
- **Tool arguments that are not valid JSON.** A model (OpenAI and OpenAI-compatible ones
  among them) can return a tool call whose arguments do not parse (`{'query': 'SF'}`, a
  trailing comma). No tool runs; the agent answers the call with an error result saying so
  and asks the model again in the same step, at most twice (`AnswerInvalidToolCalls` in
  `agent.py`). The client sees a `tool.call` with `args: {}` and an error `tool.result`.
- **Errors.** The SSE `error` event is `{"code", "message", "error_id", "run_id"}` with `code`
  one of `run_failed`, `timeout`, `recursion_limit` (only when the step-limit reply cannot be
  written), `thread_busy`, `unavailable`, `forbidden`. An unhandled error answers 500
  `{"detail": "Internal server error. Reference: <id>.", "error_id": ...}`; the detail is only
  in the server log under that id (and in the event's `detail` under `APP_ENV=dev`). An
  unreachable database answers 503 with a reference within a few seconds (2 s once the app
  knows it is down) and logs one WARNING line, no traceback. A failed tool call reaches
  clients the same way: outside `APP_ENV=dev` its `tool.result` (and its message in the
  thread history) reads `"The tool call did not succeed. Reference: <error_id>."` with an
  `error_id`; the error text (policy rule, limit, upstream status and reason) goes only to
  the model, which may still paraphrase it in its answer.
- **Run records.** Every run is recorded when it starts (`running`) and updated when it ends:
  `ok`, `step_limit`, `error`, `timeout`, `cancelled` or `interrupted` (stopped because its
  lease was lost, or its process died: records left `running` by a dead process are marked
  `interrupted` within about a minute of its lease expiring, and counted in
  `agent_runs_total{status="interrupted"}`).
- **Retention.** `RETENTION_DAYS=N` (0, the default, keeps everything) deletes threads idle
  for more than N days, with their checkpoints and run records, in an hourly best-effort
  pass on every replica. Idleness is re-checked under the thread's lock.
- **Logging.** JSON lines by default outside `APP_ENV=dev` (`LOG_FORMAT=json|text`,
  `LOG_LEVEL`), each with the request id, run id, thread id and a hashed principal id (HMAC
  with `PRINCIPAL_HASH_SALT` when set). Every response carries `X-Request-ID` (a valid one
  from the caller is echoed). The app does not log credentials, messages or tool arguments:
  access lines keep the path and drop the query string, the HTTP client libraries (`httpx`,
  `httpcore`, and `httpx2`/`httpcore2` of the model SDKs) log at WARNING only because their
  INFO lines carry full outbound URLs, and the API client logs each outbound call by API,
  method, operation id and path template (never the query, the concrete path or the body).
  Python warnings are JSON records too, with the
  values pydantic warnings echo redacted. An unexpected error's exception and traceback are
  logged under its `error_id`. `LOG_LEVEL=DEBUG` also enables third-party debug output, which
  can include message content: keep it for local debugging. Under `langgraph-server` the
  server formats its own lines (its `LOG_JSON` and `LOG_LEVEL`; `LOG_FORMAT` and the
  per-record ids above are fastapi-only) and the app applies the same rules to them: the
  server's access lines (`langgraph_api.server`) lose their `query_string` field, the HTTP
  client libraries log at WARNING only, and warnings are captured with those values redacted.
- **Settings from `.env`** apply to what the app fixes when it is imported (the A2A card and
  its auth scheme, `/docs`, CORS, the startup auth check) as well as to everything else: the
  app reads `.env` first, below the process environment.
- **CORS.** Off unless `CORS_ALLOW_ORIGINS` lists origins (comma-separated).
- **Tracing.** Off unless `TRACING_ENABLED=true`: LangSmith with `LANGSMITH_API_KEY`, else
  OTLP/HTTP to `OTEL_EXPORTER_OTLP_ENDPOINT`. `TRACE_CAPTURE=metadata` (default) exports
  structure, timing, token counts, tool names, error types and hashed ids only; `full` adds
  prompts, completions, tool I/O and the client's `/chat` metadata. Client metadata is kept
  in the run record and never written into checkpoints.
- **Database.** One connection pool per process (`DB_POOL_MIN_SIZE` 1, `DB_POOL_MAX_SIZE` 10),
  health-checked on checkout. Every connection gets `connect_timeout=5` and TCP keepalives
  (client and server side) unless the DSN sets them, so a dead database or node is noticed
  in seconds, not minutes or hours, and the app is ready again seconds after Postgres is.
  A replica that starts while Postgres is unreachable stays up, answers `/ready` 503 (and
  requests 503) and sets up its schema once Postgres answers, instead of crash-looping.
  Schema changes run under an advisory lock, so replicas can start together.

Every setting has a default in code and is documented in the generated `.env.example`. A
guardrail, limit, pool, logging, metrics, `TRACE_CAPTURE` or `A2A_TASK_TTL_S` value that does
not parse stops the app at startup, naming every bad variable; the auth settings follow the
policy's startup rule below. `TRACING_ENABLED` turns tracing on only for `true`, `yes` or `1`
(any case); any other value leaves it off.

## Authentication

One policy, selected by `AUTH_POLICY` (and recorded as `auth_policy` in the manifest),
guards every surface: `/chat` and the thread routes, the A2A card and JSON-RPC, and under
`langgraph-server` the server's native API. `/health`, `/ready`, `/metrics` and the dev-only
pages are outside it. Startup fails closed: an unknown `AUTH_POLICY` never starts, and a
misconfigured policy stops the process outside `APP_ENV=dev` (under dev the problem is logged
and requests get 503). `APP_ENV` counts as dev only when it is exactly `dev` (the app and the
chart compare it as is: `DEV` or ` dev` is a deployed environment).

Common settings: `AUTH_READ_ACROSS_ROLES` (comma list; roles that may read, never continue or
delete, other principals' threads) and `AUTH_ADMIN_ROLES` (comma list, empty = nobody; under
`langgraph-server` only these roles may create, update or delete assistants and crons or
write the store; reads are open to any authenticated principal, and every other native-API
action is denied). A native run cannot resume a paused run (a `command` is refused: decide
through the approval routes), a run without input or from a checkpoint is refused on a
thread that has approvals or waits on a gated call, and a thread that has approvals is not
copied.

### `shared-bearer` (default)

Clients send `Authorization: Bearer <API_KEY>`, compared in constant time. Every caller is
the same principal (`shared`), so thread ownership separates nobody: use it for internal
tools, service-to-service calls and development. An unset `API_KEY` answers 503, never "no
auth". `login --write-env` generates a local key; `secrets apply` / `deploy` generate one
per environment when neither the env file nor the live Secret has one.

### `jwt`

Per-user principals from a verified OIDC/JWT bearer token (`Authorization: Bearer <token>`),
for example from Keycloak, Auth0, Entra ID, Okta or Dex.

| Variable | Default | Meaning |
|---|---|---|
| `AUTH_JWT_JWKS_URL` | | The issuer's JWK set (fetched directly, no redirects; https outside dev unless the host is loopback). Set this or `AUTH_JWT_PUBLIC_KEY`, not both |
| `AUTH_JWT_PUBLIC_KEY` | | One PEM public key or certificate (only its key is used; `\n` escapes accepted) |
| `AUTH_JWT_ISSUER` | | The expected `iss`; required outside `APP_ENV=dev` |
| `AUTH_JWT_AUDIENCE` | | The expected `aud` (comma list accepted); required outside `APP_ENV=dev` |
| `AUTH_JWT_ALGORITHMS` | `RS256,ES256` | Allow-list: RS/PS/ES 256/384/512 and EdDSA; never `none`; the key type must match |
| `AUTH_JWT_ALLOW_HS` | `false` | `true` also allows HS256/384/512 with `AUTH_JWT_SECRET` |
| `AUTH_JWT_SECRET` | | Shared secret for HS*, at least 32 bytes. A secret: it goes into the app Secret (the CLI adds it to the allow-list when the chart values or env file opt into HS*) |
| `AUTH_JWT_PRINCIPAL_CLAIM` | `sub` | Claim holding the principal id (dotted path allowed; at most 256 characters) |
| `AUTH_JWT_ROLES_CLAIM` | `roles` | Claim holding the roles: a list or a space/comma separated string (dotted path allowed, e.g. `realm_access.roles`) |
| `AUTH_JWT_LEEWAY_S` | `60` | Clock skew allowed for `exp`/`nbf`/`iat` (0-600) |
| `AUTH_JWT_JWKS_CACHE_S` | `300` | How long fetched keys are cached (1-86400) |
| `AUTH_JWT_JWKS_ALLOW_HTTP` | `false` | Allow a plain-http JWKS URL outside dev (a trusted in-cluster issuer only) |

A missing token gets 401 `Missing bearer token.`; an invalid one gets 401 `Invalid bearer
token: <reason>.` with an RFC 6750 `WWW-Authenticate: Bearer error="invalid_token"` challenge
(expired, not yet valid, wrong audience or issuer, bad signature, algorithm not allowed,
unknown key, malformed, over 16384 characters, missing principal claim). A misconfigured
policy answers 503 (details in the server log). Keys: one fetch at a time, an unknown key id
triggers at most one refetch per 30 s, an expired cache is refreshed in the background while
the cached keys keep verifying, and the last good keys stay usable for 1 hour when the issuer
is unreachable (then 503). Nothing from the token is logged.

Clients put the token in `GRAPH_AGENTS_CLI_API_KEY` for `run` and `eval` (never `--header`,
which exposes it in argv). Local runs without an identity provider use
`graph-agents-cli auth dev-token` (see [Quick start](#quick-start)); the dev key lives only in
`.env` and `.graph-agents-cli/dev-jwt/` and must never reach a deployed environment.

### `custom`

For anything else, for example the session cookie of an existing application or an API
gateway's identity headers. `create --auth-policy custom` scaffolds
`app/policies/custom.py`, a documented stub that answers 503 to every request, and records
`auth_policy_implemented: false`, so `deploy --env staging|prod` is refused until you
implement it and set the flag to `true`. Implement `CustomPolicy`:

```python
from fastapi import HTTPException, Request
from app.app_utils.auth import ACTIONS, Principal


class CustomPolicy:
    async def authenticate(self, request: Request) -> Principal:
        session = request.cookies.get("session")
        user = await my_session_store.lookup(session)  # your async lookup
        if user is None:
            raise HTTPException(401, "Not signed in.", headers={"WWW-Authenticate": "Cookie"})
        return Principal(
            id=user.id,  # stable, unique: owns threads and A2A tasks
            roles=user.roles,  # matched against AUTH_*_ROLES
            permissions=set(ACTIONS),
            attributes={"tenant": user.tenant},  # secrets only under "credentials"
        )

    async def authorize(self, principal: Principal, action: str, resource: str | None) -> None:
        if action not in principal.permissions:
            raise HTTPException(403, f"{action} is not allowed.")

    def startup_problems(self) -> list[str]:  # optional: stops startup outside dev
        return [] if MY_SETTING else ["MY_SETTING is not set"]
```

Rules: raise 401 (with `WWW-Authenticate`) for a missing or invalid credential and 503 when
the issuer cannot be reached; never put the credential in an error or a log line; do I/O
asynchronously and cache briefly. Thread ownership is enforced outside the policy. A
credential that tools must forward to an `auth: forward` API goes in
`attributes["credentials"][<api name>]`, the only attribute that may hold a secret
(`Principal.public_attributes()` is what gets persisted, logged or traced). Under
`langgraph-server` with `LANGGRAPH_SERVER_URL`, `AUTH_FORWARD_HEADERS` (default
`authorization,cookie`) lists the request headers passed on to the server's auth handler.
Clients send the credential with `run --header 'Name: value'` or `--cookie name=value`.

## Outbound API policy (`api-policy.yaml`)

Tools reach external APIs only through `get_client("<api>")` of `app/app_utils/api_client.py`,
which enforces `api-policy.yaml` at the project root (path from `API_POLICY_PATH`) and refuses,
before sending, anything outside it. It fails closed: without the file, or for an API it does
not declare, every call raises `ApiPolicyError`; the refusal becomes a tool error the model
can read. The client sends every method the policy allows (`request()`, or `get`, `head`,
`post`, `put`, `patch`, `delete`, `options`) with a JSON body, query parameters and headers.

```yaml
apis:
  orders:                                  # ^[a-z][a-z0-9_]{0,31}$
    base_url_env: ORDERS_API_BASE_URL      # required; the URL may carry a path prefix (joined, never replaced)
    auth: bearer                           # required: none | bearer | forward
    token_env: ORDERS_API_TOKEN            # required iff auth: bearer (joins secrets.keys)
    # forward_header: Authorization        # auth: forward only (default Authorization)
    allowed_methods: [GET, HEAD, POST, PUT, PATCH, DELETE]  # required, explicit; ["*"] = every method
    allowed_operations:                    # optional; omitted = every operation within allowed_methods
      - operationId: listOrders            # an entry may pin operationId and/or path (+ methods);
        path: /orders                      # an allow needs every field it pins to match
        methods: [GET]
      - operationId: createOrder
        path: /orders
        methods: [POST]
      - operationId: updateOrder
        path: /orders/{order_id}
        methods: [PATCH]
    denied_operations:                     # same entry shape; denials win and hold on the path:
      - operationId: deleteOrder           # DELETE /orders/{order_id} is refused whatever
        path: /orders/{order_id}           # operation_id a call gives it
        methods: [DELETE]
    openapi: specs/orders.yaml             # optional: lint checks declared calls against it
    timeouts_ms: {connect: 2000, read: 5000}
    pagination: {page_size_param: pageSize, max_page_size: 200}   # enforced at runtime
    limits: {max_calls_per_run: 20, rate_per_minute: 120}         # optional
    approval:                              # optional: calls a human approves before they are sent
      required_for: {methods: [POST, PATCH, DELETE]}
      approvers: [requester]               # (or a list of such rules: other approvers for other calls)
```

There is no default access level. Every API lists its methods; the CLI's two shorthands are
written into the file as the methods themselves, never as a name:

| `--access` | `allowed_methods` written |
|---|---|
| `read-only` | `[GET, HEAD]` |
| `read-write` | `[GET, HEAD, POST, PUT, PATCH, DELETE]` |
| `custom --methods M,...` | exactly those (case-insensitive, stored upper-case; `"*"` alone for every method) |

Auth modes: `none` sends no credential; `bearer` sends `Authorization: Bearer
$<token_env>`; `forward` sends the caller's own `attributes["credentials"][<api name>]` as
`forward_header` (nothing when the caller has none) and is refused under `langgraph-server`,
which would persist it. The policy's credential always overrides a header the tool passes,
and a tool cannot reroute a request or change its method: `Host`, method-override
(`X-HTTP-Method-Override`, `X-HTTP-Method`, `X-Method-Override`), `X-Forwarded-*`, `Forwarded`,
`X-Original-URL`, `X-Rewrite-URL` and hop-by-hop headers are dropped, and a `_method` query
parameter or top-level JSON body key is refused. A non-2xx response raises `ApiCallError` with
`status_code` and `body` (the start of the error body, the credential redacted), and the model
reads the upstream's reason.

For an API the agent can write to, prefer per-user authorization: `auth: forward` with a
per-user auth policy sends each caller's own credential, so the upstream refuses what that
user may not do. With a shared `auth: bearer` token the agent can act on every record, and the
checks move into tool code: `require_user_mentioned(record_id, runtime)` refuses an id the
user's latest message does not name, and `require_owner(owner_id, context=runtime.context)`
refuses a record that is not the caller's (both from `app_utils.api_client`, both tool errors
the model reads). See [Security model](#security-model) for why.

Limits (optional, per API): `max_calls_per_run` caps the calls to that API within one agent
run (the LangGraph run id, else the request's); `rate_per_minute` is a token bucket per
process, so N replicas allow N times the rate. A call over a limit is refused before it is
sent, with a reason the model can read; a run's counters are dropped when a `/chat` or A2A
run ends, and otherwise (LangGraph Server runs included) after an hour without a call, at
most 10 000 runs being tracked. `approval` makes calls wait for a human: see
[Human approval of calls](#human-approval-of-calls-approval).

Fail-closed rules, identical in `create`, `lint`, `api` and the runtime (one block of code is
shared byte-for-byte and a test keeps the copies in sync):

- Unknown keys and repeated keys are errors at every level, so a typo never widens access.
- An allow needs every field it pins to match. A denial names an endpoint and holds whatever
  a call calls it: it refuses every call to a path its `path` covers (with its `methods`),
  whatever `operation_id` the call gives, and every call that names its `operationId`. It
  also refuses a call that leaves out what it knows the operation by: no `path` in
  `API_CALLS` against a denial pinning a path, no `operation_id` against a denial by
  `operationId` alone. A denial by `operationId` alone knows only that label, so a call to
  the same endpoint under another `operation_id` gets past it: pin `path` in denials
  (`api deny NAME OPERATION_ID --method M --path P`, or record the API's `openapi:`, from
  which `api deny` fills them in).
- Paths match after decoding percent-encoded unreserved characters and ignoring one trailing
  slash. Denials and approval gates fail closed: they also ignore letter case, and a literal
  segment they name also covers its dot-suffixed spellings (a denial or gate on
  `/orders/{order_id}/cancel` covers `/orders/7/cancel.json`, `cancel.` and `cancel%2e`,
  which servers that route format suffixes or drop a trailing dot send to the same endpoint);
  an allow never matches that way. A `;` in a concrete path is refused (servers that strip
  path parameters would route around a denial or a gate), and so is a segment with a control
  character, with whitespace at either end or next to a dot, also percent-encoded
  (`cancel%20`, `cancel%20.json`, `cancel%20%2e`, `7%00`: servers that trim segments, trim
  the name before a format suffix, strip trailing dots and spaces, or end a path at a NUL
  route those elsewhere). `lint` refuses the same in declared paths, and also an encoded
  slash, backslash, `;` or dot segment (`cancel%2F`, `cancel%5C`, `cancel%3B`, `%2e%2e`),
  which the client never sends. Pass model input as `path_params` of a declared template
  (each value is encoded as one segment; `.`, `..` and `/` are refused), never as part of a
  concrete path.
- The page-size parameter is capped in every spelling and shape of the query; redirects are
  not followed.

Each tool module declares its calls as one module-level literal list, and `graph-agents-cli
lint` (and `lint --policy-only`, `api check`) checks every `*.py` under `app/tools/`,
subpackages included, against the policy and the OpenAPI spec:

```python
API_CALLS = [
    {"api": "orders", "method": "POST", "operation_id": "createOrder", "path": "/orders"},
]

client = get_client("orders", context=runtime.context)
order = await client.post("/orders", operation_id="createOrder", json_body={"sku": sku})
```

`API_CALLS` changed anywhere else (`+=`, `.append()`, a conditional assignment) is a lint
error because lint cannot read it. With `openapi:` recorded, a declared `operation_id` must
be the one the spec gives that method and path: a typo or a relabelled call is refused, not
trusted. A refused call is printed with the `graph-agents-cli api` command that would allow
it: an entry pinning the call's method, and its path when it names one, so the change allows
exactly that call.

### Human approval of calls (`approval`)

The policy decides which endpoints a tool may call; it cannot tell a call the user wanted from
one an instruction planted in a tool result talked the model into (see
[Security model](#security-model)). An API's `approval` block makes chosen calls wait for a
person who sees the concrete call (which order, which amount, which body) before it is sent.

```yaml
apis:
  orders:
    # ... base_url_env, auth, allowed_methods, allowed_operations, denied_operations ...
    approval:
      required_for:                          # at least one of:
        methods: [POST, PATCH, DELETE]       #   every call with these methods ("*" = every method)
        operations:                          #   calls to these operations (allowed_operations entry shape)
          - operationId: refundOrder
            path: /orders/{order_id}/refund
            methods: [POST]
      approvers: [requester, "role:ops"]     # required: requester and/or role:<name>
      timeout_s: 900                         # optional, 30-86400 (default 900); then it expires (rejected)
```

- **Approval never widens access.** A gated call must still be allowed (`allowed_methods`,
  `allowed_operations`) and denials still win; a gate on a method the API does not allow
  changes nothing (`api approval` and `lint` say so). An `approval` key on an operation entry
  is refused: gate an operation with `approval.required_for.operations`. An operations entry
  holds like a denial, whatever label a call gives it: its `path` gates every call to that
  path, its `operationId` the calls that name it, and a call that leaves out what the entry
  knows the operation by is gated too. Pin the path: `api approval --operations` does it from
  the API's `openapi:` spec.
- **Other approvers for other calls.** `approval` may also be a non-empty list of rules, each
  of the shape above (its own `required_for`, `approvers` and `timeout_s`), for an API whose
  calls need different people: the requester confirms changes to their own orders, and a
  second person approves new ones. A call is gated by the **first** rule in file order whose
  `required_for` covers it, with that rule's approvers and expiry; a later rule that also
  covers it does not apply to it. Rules may overlap: `lint`, `api check` and `api show` name
  the rule (`approval[N]`, numbered from 0) each declared call waits for, count the calls
  several rules cover, and note a rule that never applies because earlier rules cover
  everything it does. A single mapping keeps its meaning (one rule).

  ```yaml
      approval:
        - required_for:                      # the requester confirms changes to their orders
            operations:
              - {operationId: updateOrder, path: "/orders/{order_id}", methods: [PATCH]}
              - {operationId: cancelOrder, path: "/orders/{order_id}/cancel", methods: [POST]}
          approvers: [requester]
        - required_for:                      # a second person approves new orders
            operations:
              - {operationId: createOrder, path: /orders, methods: [POST]}
          approvers: ["role:admin"]
          timeout_s: 3600
  ```

  ```bash
  graph-agents-cli api approval orders --operations updateOrder,cancelOrder --approvers requester
  graph-agents-cli api approval orders --add-rule --operations createOrder \
    --approvers role:admin --timeout-s 3600
  ```

  `--add-rule` appends a rule (it never loosens the gate: the calls earlier rules cover keep
  their approvers); `--rule N` changes or, with `--remove`, removes rule N, and a command on a
  list of several rules without either is refused. Put narrow rules before broad ones (an
  `operations` rule before a `methods` rule that would also cover it): the first match wins.
- **What happens.** Before sending a gated call the agent pauses the run (it stays in the
  checkpointer) and the stream ends with `message.end` status `awaiting_approval` and an
  `approval`: its id, the API, method, full path (ids filled in), query, body (fields the
  optional redact list names masked), operation id, the tool and the reason the model gave,
  the approvers and `expires_at`. An approver decides with
  `POST /threads/{thread_id}/approvals/{approval_id}` (`graph-agents-cli approvals approve` or
  `reject`, or the prompt of `run`); the run resumes and streams the rest.
- **Binding and single-use.** An approval covers exactly the call shown: the agent hashes the
  request it recorded and refuses to send one that differs (another body, another path), and
  it sends the approved call once. A decision is bound to the call it was taken for, not to
  the policy of the moment, so a policy that changed while the call waited (a new image, or a
  typo that un-gates it) cannot turn a decision into a send:
  - A rejected or expired call is never sent, whatever the policy now says about gating it;
    the tool gets a "not approved" error that the model relays.
  - An approved call is sent only if the current policy still allows it (a later denial, or
    narrower `allowed_methods` or `allowed_operations`, refuses it first) and still gates it
    with the same approvers; if the gate was removed, no longer covers the call, or names
    other approvers, nothing is sent and the agent asks again. With a list of rules, the
    approvers are those of the rule that gated the call when it paused (they are recorded
    with the approval and decide it), and on resume those of the rule that gates it now: a
    rule added after it changes nothing, and a reordered or edited list that hands the call
    to other approvers voids the approval.
  - A call whose approval is still pending when another call's decision resumes the run
    waits on for its own approval, even if the policy no longer gates it. If the policy now
    refuses it (a denial, narrower `allowed_methods`), it is refused and its approval
    expires, so the thread takes new messages.
  - The approvals table binds a decision to its tool call as well. A tool call that runs
    again without a decision (under `langgraph-server`, the server's own API can continue a
    paused run without input or replay it from a checkpoint, and a copied thread keeps its
    tool calls) does not send a call an approval was asked for: a rejected, expired or
    pending one is refused, and an approved one is sent only by the run its decision
    resumed, once. The server's auth handler also refuses such runs (403) on a thread that
    has approvals or waits on a gated call, and refuses to copy a thread that has approvals
    (the copy would carry its tool calls without them).

  An approval is decided once (409 after that, 410 once expired), and while one is pending
  the thread takes no new message (409 `approval_pending`).
- **Who decides.** `requester` is the principal who started the run; `role:<name>` is any
  other principal holding that role. A requester decides their own call only when
  `requester` is listed; anyone else gets 403. The decision is recorded with the decider's
  hashed id and the comment. The resumed run acts as the requester, and its stream (the tool
  result and the agent's reply) goes to whoever decided: a `role:` approver sees that much of
  the requester's conversation.
- **Choosing a gate.**
  - *Requester confirmation* (`approvers: [requester]`): the user whose run it is confirms each
    write. An injected instruction can no longer act silently in a privileged user's session:
    the user sees "POST /orders/ORD-1018/cancel" when they asked about ORD-1019.

    ```bash
    graph-agents-cli api approval orders --methods POST,PATCH,DELETE --approvers requester
    ```

  - *Four-eyes* (`role:` approvers without `requester`): a second person holding the role
    must approve, for actions one person should not take alone (refunds, account changes).
    It needs per-user principals with roles (the `jwt` or `custom` auth policy): under
    `shared-bearer` every caller is the one principal `shared`, so only `requester` gates can
    be decided.

    ```bash
    graph-agents-cli api approval payments --operations refundPayment \
      --approvers role:finance-approver --timeout-s 3600
    ```

  - Gate by method to cover every write of an API; gate operations for the risky few on an
    API whose other writes may run unattended. Loosening a gate (fewer gated calls, a new
    approver, a longer timeout, removing it) is a reviewed change like widening access;
    tightening is always safe.
- **Deciding.** `run` shows the call in full (every control character escaped, never cut)
  and, on a terminal, asks `Approve? [y/N]` when the requester is an approver (Enter
  rejects). Without a terminal, or for a call gated for others, it prints the approval id and
  the exact `graph-agents-cli approvals approve` / `reject` commands and exits 0 with an
  "Awaiting approval" line; a one-off local server with the in-memory checkpointer is kept
  running so the paused run survives. `approvals list` shows what waits on a thread, or,
  without `--thread-id`, every approval you may see (`GET /approvals`: your own, and the ones
  a role of yours may decide), so a `role:` approver finds the calls waiting for them.
- **Where approvals live.** In an `approvals` table in the agent's database, beside the
  checkpoints (`POSTGRES_DSN`; `agent_approvals` in `DATABASE_URI` under `langgraph-server`): the call and its
  hash, the thread, run and hashed requester, the status, the hashed decider, the comment and
  the expiry. Pending approvals past their expiry are swept and reported as expired; deleting
  a thread deletes its approvals; `/metrics` counts approvals requested, approved, rejected
  and expired. With `CHECKPOINTER=memory` a paused run lives in one process only: a restart
  loses it (`infra check` warns). The local `langgraph dev` server (what `run`, `playground`
  and `eval` start under `langgraph-server`) keeps its threads in `.langgraph_api/` of the
  project across a restart or a hot reload (a code change), and the approvals with them, in
  `.langgraph_api/agent_approvals.json` (mode 0600): each change is written before it takes
  effect, so a restart or a reload keeps every binding above. A file that cannot be read
  stops the server's startup, and while one cannot be written nothing gated or bound is sent.
  Delete `.langgraph_api/` to reset both; the scaffold's `.gitignore` and `.dockerignore`
  keep it out of git and images. With no local server running, `approvals` starts a
  temporary one there, as for `fastapi` with the postgres checkpointer.
- **A2A.** A gated run moves the task to `input-required` with a data part holding the
  approval; the client resumes it with a message on the same task carrying the data part
  `{"approval_id": "...", "decision": "approve"}` (or `"reject"`), under the same approver
  rules and the auth policy's `approval.decide` action. A task belongs to its principal, so
  only the requester decides over A2A; `role:` approvers use the HTTP routes.
- **Checks.** `lint`, `api check` and `api show` list which declared calls wait for whose
  approval, and by which rule; eval cases say how to decide each gate (see
  [Evaluation](#evaluation)).

### The policy's lifecycle

`api-policy.yaml` belongs to the project and evolves with the agent. `create` only seeds it
(`--api-policy <file>`: validated first, the OpenAPI specs it references copied, and
`app/tools/example_api.py` rendered with the first operation the first API allows, whatever
its method: one concrete declared call rather than a generic "any method, any path" tool,
because `lint` can only check the calls a tool declares) or leaves it out; `scaffold enhance` and `scaffold upgrade` never touch it.
Afterwards it changes through `graph-agents-cli api` (see [Commands](#commands)): each
command validates the current file, applies one change, validates the result, prints a
unified diff of every file it touches (the policy, the manifest's `api_policy` and
`secrets.keys`, `.env.example`, the chart's `values.yaml`), keeps comments and key order, and
writes atomically; `--dry-run` stops after the diff, and an invalid result exits 3 with
nothing written. It also says whether the change widens or narrows access and which of the
tools' declared calls become allowed or refused. `api allow` on an API without
`allowed_operations` creates the list, which narrows access from "every operation within
`allowed_methods`" to the listed ones: the command says so. With `openapi:` recorded, `allow`
and `deny` by operation id check that the id exists and fill in its method and path; without
one, give them yourself (`api allow orders updateOrder --method PATCH --path
/orders/{order_id}`), since an entry by operation id alone pins only the tool's label.

1. `graph-agents-cli create my-agent`, then `graph-agents-cli api add orders --base-url-env
   ORDERS_API_BASE_URL --auth bearer --token-env ORDERS_API_TOKEN --access read-only`
   (every access level is an explicit choice).
2. Write the tool with its calls in `API_CALLS`; `graph-agents-cli lint` (or `api check`).
   Decide which of its writes need a human first (`graph-agents-cli api approval`).
3. Eval cases for the tool's behaviour, refusals included; `graph-agents-cli eval run`.
4. A pull request: `.github/CODEOWNERS` covers `api-policy.yaml`, so widening access (more
   methods or operations, a lifted denial, a raised limit, a loosened approval gate) needs
   the code owners' approval.
   Narrowing is always safe, and the runtime keeps refusing anything outside the policy even
   if a tool declares otherwise.
5. `build`, then `deploy --env dev`, staging, prod. Both Dockerfiles copy the policy into the
   image (readable by the image's uid 1000, whatever the file's mode in your checkout), so each
   image carries exactly one policy: what passed staging is what reaches
   production. Only the base URLs (the chart's `env`, per environment in
   `values-<env>.yaml`) and the tokens (the Secret) differ between environments.

Adding functionality to a working agent, for example letting the agent of step 1 (`orders`
read-only, no `allowed_operations`, a tool calling `listOrders`) update orders:

```bash
graph-agents-cli api show orders                                             # the calls your tools declare, each allowed or not
graph-agents-cli api allow orders listOrders --method GET --path /orders     # first, what the agent already calls (see below)
graph-agents-cli api allow orders updateOrder --method PATCH --path /orders/{order_id}   # --dry-run first to review the diff
graph-agents-cli api access orders custom --methods GET,HEAD,PATCH           # PATCH reaches the listed operations only
# write app/tools/update_order.py with {"api": "orders", "method": "PATCH", "operation_id": "updateOrder", ...}
graph-agents-cli api check
graph-agents-cli eval run                                       # with new cases for the change
git switch -c orders-update && git add -A && git commit -m "Allow updating orders" && git push   # PR: CODEOWNERS review
```

An API without `allowed_operations` allows every operation within its methods. There,
`api access` alone would open the new method to every operation of the API (every PATCH of
`orders`, not only `updateOrder`), and the first `api allow` creates the list, so every call
not on it is refused from then on (the command names the declared calls that become refused).
So list the operations the agent already calls first, as above: `api check` then passes
throughout. When the API already has `allowed_operations`, skip that line.

## Evaluation

`eval run` sends every case in `tests/eval/datasets/*.json` to the agent's `POST /chat` (a
multi-message case sends its user messages in order on one thread), then grades the traces. The
exit code is the gate: every case accounted for, every deterministic `expect` check and every
mandatory judge metric passed, and each metric listed under `quality_metrics` in
`tests/eval/eval_config.yaml` at or above its `min_pass_rate`, a rate over the cases scored on
that metric (a case that does not declare it is not counted as a pass). The
`graph-agents-cli-eval` skill documents the dataset schema, checks, judges and exit codes.

- `expect.contains` and `not_contains` compare case-insensitively (`not_contains: ["deleted"]`
  also fails on "Deleted"); `expect.case_insensitive: false` makes them exact. On a multi-turn
  case the checks read the final turn, or every turn with `expect.scope: all_turns`.
- Judges see every turn: each earlier user message, tool call with its result, and agent reply,
  then the reply being scored. A tool result longer than `judge.max_tool_result_chars` (default
  50000 characters; `null` never cuts) is cut with a marker telling the judge how much it did not
  see, and `eval grade` warns which cases were cut. Custom prompt templates can use `{transcript}`
  for the whole case.
- On the deterministic fake model (`MODEL_PROVIDER=fake` for the local agent, or a fake judge)
  `eval grade` warns that the result is not a quality signal and marks "gate met" as a plumbing
  check only. Run the gate on a real provider before trusting it.
- `eval run --url` (and `eval generate --url`) drive a deployed agent, and **its tools run for
  real there**: a case that creates, updates or cancels data does so in that environment, as the
  identity the requests authenticate as. The command prints a warning naming the target and the
  write methods the project's `api-policy.yaml` allows before the first case. Use an
  environment whose data you can reset and a dedicated test identity, never production data.
  Its credential goes in `GRAPH_AGENTS_CLI_API_KEY`, not on the command line; credentials in
  the URL itself are shown as `***@` and never stored in traces or results. The agent there
  does not report its model, so the traces and results record `model: null`; when the
  project's own settings name the fake model, `eval grade` warns that the target may be
  running them.
- A case that reaches a gated call says how a human would decide it: `"approvals":
  [{"decision": "approve", "match": {"operation_id": "cancelOrder"}}]` (or `match`
  `{"method": "POST", "path": "/orders/{order_id}/cancel"}`, optionally with `api`); the
  first matching instruction decides each gate and the run continues. A gate no instruction
  matches makes the case an error: generate never approves on its own, and it rejects that
  gate (and one whose decision the server refused) so an eval run leaves no approval pending
  to block its thread or be approved later. A gate it may not reject (a `role:` gate without
  `GRAPH_AGENTS_CLI_APPROVER_API_KEY`, or with a credential that may not decide it) goes
  with the case's thread, which the eval identity deletes (deleting a thread deletes its
  approvals). The trace records how (`approvals[].cleanup`: `rejected`, `not_pending`,
  `thread_deleted`, or `left_pending` when the thread could not be deleted either, which the
  case error names: it waits until it expires, unless an approver approves or rejects it
  first). Traces record every
  gate (`approvals`), and `expect.approvals: [{"match": {...}, "status":
  "gated"|"approved"|"rejected"}]` and `expect.no_approvals: true` check them (an injection
  case can assert that the planted write never even reached a gate). A gate that lists
  `requester` is decided as the eval identity (it started the run); any other as
  `GRAPH_AGENTS_CLI_APPROVER_API_KEY` when set (a principal holding the gate's role), so one
  dataset can mix both; with `--url`, an approved call is sent there for real.
- A multi-turn case with `expect.scope: all_turns` whose trace has no per-turn records (an older
  traces file, an `eval generate` override) is graded on its final turn, and `eval grade` says
  which cases.

## Environments and CD modes

Every Kubernetes project has three environments, `dev`, `staging` and `prod`, each with a
values file (`deployment/helm/<name>/values-<env>.yaml`), a namespace (`<name>-<env>`), a
Secret (`<name>-app`) and, under Argo CD, an `Application`. `values-dev.yaml` enables the
bundled Postgres (and Redis for `langgraph-server`) and disables the Gateway; staging and
prod expect an external database through the Secret and a Gateway API `HTTPRoute` (or an
`Ingress`).

The `--cd` choice at create time fixes how changes reach a cluster:

| Mode | What `deploy` does | What CI does |
|---|---|---|
| `skip` (default) | Builds the image, side-loads it into a local cluster or pushes it, applies the Secret, `helm upgrade --install`, for any environment | `pr_checks` only (ruff, tests, eval gate) |
| `helm-push` | Direct deploy to `dev`; `staging`/`prod` are refused outside CI (even with `--image`) unless `--force-direct`. It only checks the live Secret, never applies it, and refuses `--env-file` and `--rotate-api-key`: the Secret owner runs `secrets apply` | `staging` builds and deploys from `main` on a self-hosted runner; `promote-to-prod` deploys behind the GitHub `production` environment; both verify the rollout (`/health`, `/ready`) |
| `argocd` | Never runs helm and contacts no cluster: opens a pull request that changes only `image.tag` in `values-<env>.yaml` (branch `deploy/<env>/<short sha>`, built with git plumbing from `origin/main`, your checkout untouched); `--status` and `--restart` use the cluster | CI builds, pushes and opens the staging PR with auto-merge (newer PRs supersede older ones); production is a PR merged by a human after code-owner review, then an Argo CD sync (manual for prod) |

Rules `deploy` and `secrets apply` follow:

- **Env file.** `--env-file`, else `.env.<env>`. Only `dev` falls back to `.env`; any other
  environment without an env file exits 3, so your development keys never reach staging or
  prod. `secrets apply` always reads one; `deploy` reads it (and applies the Secret) only in
  `skip` mode. In `helm-push` mode `deploy` checks the live Secret without applying it, in
  `argocd` mode it never touches the cluster, and both refuse `--env-file` and
  `--rotate-api-key`: the Secret owner runs `secrets apply`.
- **Kube context.** The context is `--context`, else `environments.<env>.context` in the
  manifest, else the kubeconfig's current context. Outside `dev` the current context needs a
  confirmation: a prompt at a terminal, `--yes` otherwise (exit 1 without). An explicit
  context missing from the kubeconfig is exit 3. The resolved context and API server are
  printed before anything happens; `--dry-run` never prompts.
- **Order.** Everything that needs only the project (chart values, image reference, env file)
  is checked first. Then the context is confirmed, the Secret the deploy would produce (the
  live keys merged with the env file) is checked for every required key (exit 1 when one is
  missing, before anything is built or changed), and the release is checked for a helm
  operation in progress. Only then is the image built and loaded or pushed, the namespace
  created when missing, the Secret applied and helm run. `--dry-run` makes the same checks
  (reading the live Secret, read-only), so it refuses what the real run would.
- **Settings that cannot work.** A `CHANGE-ME` placeholder left in the merged chart `env` (an
  API base URL written by `api add`, the `openai-compatible` `OPENAI_BASE_URL`) is refused
  outside dev in every CD mode (exit 3) and a warning in dev. Under `jwt`, outside dev the
  chart env or the Secret must provide a verification key (`AUTH_JWT_JWKS_URL` or
  `AUTH_JWT_PUBLIC_KEY`), `AUTH_JWT_ISSUER` and `AUTH_JWT_AUDIENCE` (exit 3; the pods would
  refuse to start); in dev a missing key source is a warning (the pods would answer 503). A
  value the chart env lists, even empty, overrides the Secret.
- **Rollout.** `helm upgrade --install --wait --timeout <--timeout, default 5m>`. When the
  rollout fails, deploy prints the pods, container states, this release's warning events since
  the deploy started and the logs, then, with `--atomic` (default), rolls back to the newest
  good revision or uninstalls a first install that never succeeded. It only undoes the
  revision this run created: when another helm operation holds the release (a `pending-*`
  revision), deploy refuses up front (exit 2) and prints the command that clears a lock left by
  an interrupted helm. Two deploys started at the same moment have narrow gaps (see
  [Known limitations](#known-limitations)).
- **The Secret after a failure.** Once the release is back where it was (rolled back, failed
  before a new revision, or a first install uninstalled), the app Secret this deploy applied
  is put back to its previous values, and one it created is deleted, so a bad value never
  waits for the pods' next restart. A Secret someone changed in the meantime is left alone.
  When the release stays on the failed revision (`--no-atomic`, another deploy), the Secret
  keeps the new values and the error names the changed keys.
- **Same image.** Redeploying the image the release already runs is said up front; after the
  upgrade deploy reports when no pod was replaced and, when the Secret changed, that
  `deploy --restart` is what makes the pods read it.
- **Images.** Workstation builds are tagged with the short commit sha, plus
  `-dirty-<timestamp>` when the tree has uncommitted changes (outside `deployment/`,
  `.github/`, `tests/`, `docs/`), with a warning; `--tag` overrides. A placeholder registry
  (`ghcr.io/CHANGE-ME`) or an invalid reference is exit 3 before `docker` runs. `--image`
  takes a tagged reference (digests are refused: the chart renders `repository:tag`).
- **Local clusters.** Side-loading (kind, k3d, minikube; k3s through `k3s ctr images import`,
  which usually needs root; Docker Desktop, Rancher Desktop and OrbStack share the daemon and
  need none) is chosen from the cluster's nodes and confirmed with the tool's own cluster
  listing (`kind get clusters`, `k3d cluster list`, `minikube profile list`). The context
  name decides only when the nodes cannot be read, and a `kind-*` name is still confirmed.
  Otherwise the image is pushed to the registry.
- **Protected environments.** `deploy --env staging|prod` is refused while the manifest says
  `auth_policy_implemented: false` (the `custom` stub).
- **Chart dependencies.** `deploy` runs `helm dependency build` when a subchart is missing
  (also under `--dry-run`), which needs `registry-1.docker.io` unless `charts/` is vendored.

`deploy --status` reports the rollout within `--timeout` (default 60s): replicas, image, helm
revision and each pod's readiness and restarts, with the pods' states, warning events and logs
and exit 1 when it is not complete. `deploy --restart` restarts the Deployment (after a Secret
rotation) and waits for the new pods (default 5m; exit 2 with diagnostics when they do not become
ready, while the old pods keep serving). `--dry-run` prints every command plus the rendered
manifests without changing anything. For a GitHub Enterprise Server remote set `GH_HOST=<host>` (and
`GH_ENTERPRISE_TOKEN` or `GITHUB_TOKEN`) so argocd-mode `deploy` opens the PR there.

### Chart

The chart runs the pod as uid/gid 1000 with a read-only root filesystem (a `/tmp` emptyDir
is the only writable path), seccomp `RuntimeDefault`, every capability dropped, no privilege
escalation and no service-account token. Readiness uses `/ready`, liveness and startup
`/health`. Resource requests default to 100m CPU / 256Mi with a 1Gi memory limit (250m / 512Mi
requests in prod). Values worth knowing:

- `image.tag`: empty by default; the chart refuses to render without one and never defaults
  to `latest`. Quote it (`tag: "0123456"`): an unquoted number is refused.
- `route.publicPaths`: what the HTTPRoute or Ingress publishes, by default `/chat` (Exact),
  `/threads` and `/a2a/<agent>` (PathPrefix); `route.devPaths` (`/playground`, `/docs`,
  `/openapi.json`) are added only under `APP_ENV=dev`. `/health`, `/ready` and `/metrics`
  are never published.
- `secretOptional`: `true` in dev, `false` elsewhere, where pods do not start without the
  app Secret.
- `gateway.*` / `ingress.*` / `tls.*`: set `gateway.parentRef.name` and `gateway.hostname`
  (staging and prod), or enable the Ingress; TLS from `tls.existingSecret` or cert-manager.
- `metrics.scrapeAnnotations`, `metrics.serviceMonitor.enabled`: Prometheus scraping (off).
  With `METRICS_TOKEN` in the app Secret, `metrics.serviceMonitor.bearerToken.enabled` makes
  the ServiceMonitor send it, read from `<release>-metrics`, a Secret holding only that token
  (`secrets apply` and a direct `deploy` write it): Prometheus needs no access to the app
  Secret. `bearerToken.secretName` and `.key` name a Secret of your own. Pod annotations cannot
  carry a token: give that Prometheus's scrape job the token itself.
- `networkPolicy.*`: an optional NetworkPolicy (off: it needs a CNI that enforces it and
  addresses only you know). It admits only the http port, from the `ingressFrom` sources when
  listed; with `restrictEgress` it also limits egress to DNS and `egressTo`.
  `deployment/helm/<name>/examples/networkpolicy.yaml` is a worked staging/prod example (in
  from the Gateway's and Prometheus's namespaces; out to DNS, the database, an on-network model
  server and HTTPS on public addresses only, every private range and the metadata endpoint
  excluded): copy its block into `values-<env>.yaml` and set the addresses marked `CHANGE`.
- `shutdown.preStopSleepSeconds` (5) and `shutdown.drainSeconds` (20): a stopping pod keeps
  serving while the endpoints drain (no refused connections during a rolling restart), then
  gets SIGTERM and finishes in-flight requests and streams within the drain;
  `terminationGracePeriodSeconds` (30) must be longer than both together (the chart refuses
  it otherwise). Raise them together for long runs.
- `hpa.*`, `pdb.*`, `topologySpread.*` (on in prod), `probes.*`, `resources`,
  `extraVolumes`, `extraVolumeMounts`.
- `postgresql.*` / `redis.*`: the Bitnami subcharts for dev, pinned to exact chart versions
  and image digests; the dev database password lives in a chart-managed Secret that survives
  upgrades, and a restart of the database pod shuts Postgres down in fast mode (seconds, no
  crash recovery). Use an external database (or another chart) in staging and prod.

### External database

In staging and prod `POSTGRES_DSN` (or `DATABASE_URI`) in the Secret points at a database you
run. Give the agent a least-privileged role that owns its own database (it creates its tables at
start and needs nothing else, never a superuser), and require TLS with the server's certificate
checked:

```sql
CREATE ROLE agent LOGIN PASSWORD '...' NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE DATABASE agent OWNER agent;
REVOKE ALL ON DATABASE agent FROM PUBLIC;
```

```
POSTGRES_DSN=postgresql://agent:<password>@db.example.com:5432/agent?sslmode=verify-full&sslrootcert=/etc/db-ca/ca.crt
```

The DSN reaches psycopg unchanged, so every libpq parameter works; mount the CA with
`extraVolumes` / `extraVolumeMounts` (a Secret or ConfigMap at `/etc/db-ca`), or use
`sslrootcert=system` for a publicly trusted certificate, or set `PGSSLMODE` / `PGSSLROOTCERT` in
the chart env. `deploy`, `secrets apply` and `infra check` warn outside dev when the DSN does not
require TLS (the value is never printed). On the server, `hostssl` entries in `pg_hba.conf`
refuse clear-text connections.

## Secrets

Secrets never live in values files or the chart. The manifest's `secrets.keys` allow-list is
the only set of variables that can reach the cluster: the provider key, `JUDGE_API_KEY`,
`POSTGRES_DSN` (or `DATABASE_URI` and `REDIS_URI`), `API_KEY`, `LANGSMITH_API_KEY` and the
`token_env` of every `auth: bearer` API. Add any other secret your project uses (for example
`METRICS_TOKEN`, `PRINCIPAL_HASH_SALT`) to that list.

1. Put the values in `.env.<env>` (dev may use `.env`).
2. `graph-agents-cli secrets apply --env <env>` creates the namespace when missing and applies
   the Opaque Secret `<name>-app` with server-side apply (`kubectl create secret generic
   --from-env-file=<0600 temp file> --dry-run=client -o yaml | kubectl apply --server-side
   ...`), so no value appears on a command line or in a `last-applied-configuration`
   annotation (an old one is removed). Allow-listed keys the env file leaves out are kept from
   the live Secret; remove a key by dropping it from `secrets.keys`. Values must be single-line.
3. `API_KEY`: the live key wins. It is replaced only when the env file sets a different one
   and `--rotate-api-key` is passed (clients with the old key then get 401). Under
   `shared-bearer`, the only policy that reads it, when neither the env file nor the Secret has
   one, a key is generated, written to the env file (0600) and never printed; `jwt` and
   `custom` projects never get one generated. `METRICS_TOKEN`, when set, is also written alone
   into `<name>-metrics` for the ServiceMonitor.
4. `graph-agents-cli secrets status --env <env>` lists present, missing required, missing
   optional and unexpected keys, never values. Required keys are the provider key (not for
   `openai-compatible`), `API_KEY` under `shared-bearer`, `AUTH_JWT_SECRET` under `jwt` with
   an HS* algorithm, and the database URIs unless the bundled subchart provides them.
5. Rotate by changing the value in the env file, running `secrets apply` (with
   `--rotate-api-key` for `API_KEY`), then `deploy --env <env> --restart`, which waits for the
   new pods and prints why when they do not become ready.

In `argocd` and `helm-push` modes CI never holds application secrets: an operator (the
manifest's `secrets.owner`) runs `secrets apply` from a workstation with cluster access,
once per environment. `--force-conflicts` in the server-side apply makes the CLI the owner of
the allow-listed keys: do not let another controller (for example External Secrets) manage
the same keys.

### Required GitHub settings (`helm-push`, `argocd`)

- Environments `staging` and `production`; required reviewers (and "prevent self-review")
  on `production`; deployment branches limited to `main`.
- Branch protection on `main`: `pr_checks` required, code-owner review required, no
  self-approval, auto-merge allowed (the staging PR uses it). Replace the
  `@CHANGE-ME/production-approvers` owner in `.github/CODEOWNERS`.
- `GH_PR_TOKEN` (a fine-grained PAT or GitHub App token with pull-request and contents write
  access): pull requests opened with the workflow's `GITHUB_TOKEN` never trigger
  `pr_checks`, so auto-merge on a required check needs it.
- `helm-push`: `DEPLOY_KUBECONFIG` as a secret of the `staging` and `production`
  *environments* (never a repository secret), and a self-hosted runner with network access to
  the cluster and `kubectl` and `curl` installed.
- Registry credentials when the registry is not GHCR (`REGISTRY_USERNAME`,
  `REGISTRY_PASSWORD`).
- `argocd`: Argo CD with a repository credential, and the `deployment/argocd/` Applications
  (with `repoURL` set) applied once by an operator.
- Optional: the provider key secret (`OPENAI_API_KEY`, ...) or the `MODEL_PROVIDER` /
  `MODEL_NAME` repository variables make the `pr_checks` eval gate use a real model; on the
  fake model it warns that the gate is not a quality signal.

`graph-agents-cli infra check --env <env>` reports these settings when `gh` is logged in (or
`GITHUB_TOKEN` is set), the cluster prerequisites (Gateway API when `gateway.enabled`, ingress
class, cert-manager, Argo CD, metrics-server when the HPA is on, namespace, pull secret, the app
Secret and its required keys), the `jwt` verification settings, the ServiceMonitor's token
Secret, whether an external DSN requires TLS, and every unreplaced `CHANGE-ME` placeholder
(the chart `env` one is required outside dev, a warning in dev, as for `deploy`). What the
environment does not use is `skip`, without hints; every printed command runs as is. It creates
nothing.

`.github/agent.env` holds the workflows' project settings (`IMAGE_REPOSITORY`,
`RELEASE_NAME`, `CHART_PATH`, `RUNTIME`, `CD`, `GRAPH_AGENTS_CLI_SPEC`) and is read as
`NAME=VALUE` data, never sourced: any other name, a duplicate or a control character fails
the step before anything is exported.

## Exit codes

Every command follows one scheme:

| Code | Meaning |
|---|---|
| 0 | Success (`eval`: gate met; `secrets status`: every required key present; `run`: also a run left awaiting an approval) |
| 1 | Refused by policy or mode, a declined confirmation, or a failed gate (`eval`: a case failed or a quality metric is under its `min_pass_rate`; `lint`: a violation, or ruff failed; `install`: uv failed; `run`: the agent answered with an error; `run` and `approvals`: the server refused a decision (not an approver, already decided, expired); `scaffold enhance`: required steps left; `deploy --status`: the rollout is not complete within `--timeout`) |
| 2 | Tool failure: helm, kubectl, docker, git or gh failed or is missing (`deploy`, `build`, `secrets`); a local server that cannot start or an agent that cannot be reached (`run`, `eval`); `eval`: a case is `error` or `missing`; `scaffold upgrade` and version-locked `scaffold enhance`: `uvx` is missing or could not fetch and run the prior build; an unexpected crash |
| 3 | Configuration error: not in a project, an invalid manifest (for `scaffold upgrade`, also a missing or unreleased `cli_version`, a recorded build that cannot be rebuilt, or a `--baseline-ref` that names no build or a build of another version), env file, policy (`lint` and `api check` included: an invalid `api-policy.yaml` is not a refused call), port or context, a placeholder registry, a `CHANGE-ME` left in the chart `env` or incomplete `jwt` settings outside dev (`deploy`), an unusable `GRAPH_AGENTS_CLI_INSTALL_SPEC` |

Usage errors from Click (an unknown flag) are also 2. A signal ends a command with 128+N
(130 for Ctrl-C, 143 for SIGTERM) after the local server it started is stopped.

## Security model

- **Authentication on every surface** except the probes, `/metrics` (unless
  `METRICS_TOKEN`) and dev-only pages; unknown or misconfigured policies fail closed at
  startup. Threads and A2A tasks belong to the principal that created them; read-across roles
  may read, never write.
- **Outbound calls are allow-listed** by `api-policy.yaml` and refused before sending; the
  same rules are checked statically by `lint` in CI. There is no default access: each API
  lists its methods, widening access is a reviewed change (CODEOWNERS), and optional
  per-API limits cap the calls per run and per minute.
- **Human approval of writes.** An API's `approval` block makes chosen calls wait until the
  requester, or another principal holding a role, approves exactly that call; it is sent
  once as approved, or never. It is the control for write actions a prompt injection could
  trigger, chosen per API (never on by default): see
  [Human approval of calls](#human-approval-of-calls-approval).
- **Tool results are untrusted input.** The policy decides which endpoints a tool may call,
  not on whose behalf. Text a tool returns (a customer's order note, a ticket comment, an
  upstream error) reaches the model beside the user's request, and planted instructions can
  make a privileged user's agent act on another customer's record or copy one customer's data
  where another can read it (prompt injection, a confused deputy). The template fences every
  tool result the model reads as untrusted data (`UntrustedToolResults` in `agent.py`) and its
  default prompt forbids following instructions found there; write tools must still check who
  asked for what (`require_user_mentioned`, `require_owner`), and write-capable APIs should
  authorize each user themselves (`auth: forward`). These lower the risk; they do not remove
  it. Gate the writes an injected instruction could abuse with an `approval` block, so a
  person sees each concrete call first, and add eval cases with planted instructions
  (`expect.no_approvals` asserts the planted write never reached a gate).
- **Secrets** stay in the allow-listed Kubernetes Secret: never in values files, workflow
  logs, command lines or printed output. Only `Principal.public_attributes()` is persisted,
  logged or traced; principal ids are hashed in logs and traces.
- **Data egress** is explicit: tracing is off by default and `TRACE_CAPTURE=metadata` keeps
  prompts, completions and tool I/O out of traces. A hosted model provider receives the
  prompts, tool results and context the agent assembles: decide what may leave your network
  before connecting one.
- **Deploys** need an explicit context outside dev, never reuse development keys, never
  rotate the live `API_KEY` implicitly, and only roll back their own revision (two narrow
  races between concurrent deploys are listed under
  [Known limitations](#known-limitations)). Production
  desired state changes only through a reviewed pull request (`argocd`) or the `production`
  environment gate (`helm-push`).
- **Supply chain**: the CLI installs from a pinned git tag, and `setup` installs the skills
  from the same tag; generated projects pin the CLI version in `.github/agent.env`, install from committed lock files, and pin base images,
  the uv version, subchart versions and subchart image digests. CI and CD jobs disable
  extension overrides.
- **Pods** run as non-root with a read-only root filesystem, no capabilities and no
  service-account token; probes and metrics stay inside the cluster.

What it does not do for you: inbound rate limiting (do it at the gateway; outbound calls
have per-API `limits`), web application firewall
rules, TLS termination (the Gateway, Ingress or cert-manager), network isolation (the
NetworkPolicy is off by default; `examples/networkpolicy.yaml` in the chart is a worked
staging/prod policy to adapt), and backups of the agent's database.

## Production checklist

- [ ] Pick the auth policy: `jwt` against your identity provider, or a `custom` policy you
      implemented and tested (then set `auth_policy_implemented: true`). `shared-bearer` only
      for trusted callers.
- [ ] Set `AUTH_READ_ACROSS_ROLES` and `AUTH_ADMIN_ROLES` deliberately (both empty by default).
- [ ] Declare every outbound API with the access it needs and no more (`graph-agents-cli api
      add`, then `allow`/`deny` for its operations), with `limits` where a runaway loop would
      hurt; `graph-agents-cli api check` passes and CODEOWNERS covers `api-policy.yaml`.
- [ ] Every write tool calls `require_user_mentioned` on the ids it acts on (and
      `require_owner` under a per-user policy), write-capable APIs use `auth: forward` where
      the upstream can authorize the user, and `agent.py` keeps `UntrustedToolResults` (and
      `AnswerInvalidToolCalls`) and the prompt's tool-results rule.
- [ ] Decide which writes wait for a human (`graph-agents-cli api approval`): `requester`
      confirmation for writes a user makes on their own records, `role:` approvers (four-eyes,
      under `jwt` or `custom`) for actions one person should not take alone; the paused runs
      and the `approvals` table need the postgres checkpointer.
- [ ] `eval run` passes on the real model, with cases for your tools, refusals, failure
      modes and instructions planted in tool data; the `pr_checks` gate runs on the real
      provider (its key secret is set).
- [ ] Record `environments.<env>.context` for staging and prod in the manifest; keep
      `.env.staging` / `.env.prod` out of git.
- [ ] `secrets apply --env <env>`, then `secrets status --env <env>` exits 0.
- [ ] Replace every `CHANGE-ME` (registry, chart image, CODEOWNERS owner, Argo CD `repoURL`);
      `infra check --env prod` reports no required item missing.
- [ ] External Postgres for staging and prod, with backups; a least-privileged role that
      owns its database; `sslmode=verify-full` in the DSN (see [External database](#external-database));
      `max_connections` covers replicas x (`DB_POOL_MAX_SIZE` + 1); no transaction-mode
      PgBouncer in front.
- [ ] A NetworkPolicy adapted from `deployment/helm/<name>/examples/networkpolicy.yaml`, on a
      CNI that enforces it.
- [ ] Gateway or Ingress with TLS; review `route.publicPaths`; rate limiting at the gateway.
- [ ] `APP_URL` (or `appUrl`, or a hostname) so the A2A card advertises the public URL.
- [ ] `METRICS_TOKEN` (in `secrets.keys` and the Secret) or a NetworkPolicy if anything outside
      the cluster can reach the pods; Prometheus scraping configured, with
      `metrics.serviceMonitor.bearerToken.enabled` (or the token in your scrape job) when
      `METRICS_TOKEN` is set; alerts on `agent_runs_total{status!="ok"}` and `/ready`.
- [ ] `PRINCIPAL_HASH_SALT` set (and added to `secrets.keys`) if principal ids are guessable
      (email addresses, for example).
- [ ] Decide `RETENTION_DAYS`, `TRACING_ENABLED` and `TRACE_CAPTURE` with whoever owns the
      data; publish a privacy notice for a hosted model provider.
- [ ] Tune `RUN_TIMEOUT_S`, `RECURSION_LIMIT`, `resources`, `replicaCount` or the HPA, and
      the PodDisruptionBudget for your traffic; run the load test in `tests/load_test/`.
- [ ] GitHub settings above in place (`infra check` reports them); pin the actions in the
      generated workflows to commit SHAs if your organisation requires it.
- [ ] Mirror the base images and vendor the subcharts if the cluster cannot reach Docker Hub.

## Disconnected profile

"Runs locally" means the orchestration runs on your machine; "runs disconnected" means the
whole lifecycle works without internet access. The disconnected profile is:

- `MODEL_PROVIDER=openai-compatible` with `OPENAI_BASE_URL` at an on-network server (vLLM,
  TGI, Ollama) and a tool-capable model; the judge uses the same mechanism via `JUDGE_*`.
- Runtime `fastapi` (the LangGraph Server image needs a licence check, see
  [Known limitations](#known-limitations)).
- Dependencies from a private index (`UV_INDEX_URL`, `install --locked`); base images
  mirrored into your registry; the chart's subcharts vendored under
  `deployment/helm/<name>/charts/`.
- Tracing off, or `TRACING_ENABLED=true` with `OTEL_EXPORTER_OTLP_ENDPOINT` at an in-cluster
  collector; no LangSmith.
- `GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1`; skills installed from the wheel bundle; the CLI
  installed from a mirror (`GRAPH_AGENTS_CLI_INSTALL_SPEC`).
- `cd: skip` with direct-mode `deploy`, unless an on-network GitHub Enterprise Server hosts
  Actions (declared with `GH_HOST`).

`login --profile disconnected` and `infra check --profile disconnected` verify these
conditions and fail on any hosted dependency (a hosted model or judge, `LANGSMITH_API_KEY`,
tracing without an OTLP endpoint, GitHub-hosted runner labels, `langgraph-server`, a
registry that is not on-network).

## Compared with google-agents-cli

graph-agents-cli started from google-agents-cli 1.6.1 and keeps its lifecycle (setup,
scaffold, run, eval, deploy, extensions, skills) and its scaffold engine (template layering,
remote templates, 3-way merge for enhance and upgrade). It targets LangGraph on any
Kubernetes cluster instead of ADK on Google Cloud. As of google-agents-cli 1.7.0
(September 2026):

**Where it goes further**

- An enforceable eval gate: every planned case accounted for, deterministic checks and
  mandatory judges with no threshold, quality metrics with an explicit `min_pass_rate`,
  exit codes CI can use, `eval compare --fail-on-regression`, and a deterministic fake model
  for keyless CI.
- An outbound API policy enforced at runtime and checked by `lint`.
- Kubernetes Secrets management (`secrets apply/status`, allow-listed keys, pre-deploy
  checks) and Argo CD GitOps through pull requests.
- A disconnected profile, verified by `login` and `infra check`.
- Self-hosted auth policies (shared bearer, OIDC/JWT, custom) enforced in the app on every
  surface, instead of a cloud provider's identity layer.

**Where it is behind**

- Evaluation: no prompt optimization, dataset synthesis, user simulation or results fetch
  (`eval optimize`, `eval dataset synthesize`, `eval results` upstream); eval cases are
  written by hand.
- Infrastructure: no provisioning. `infra check` only reports; upstream's `infra cicd`
  creates the CI/CD setup with Terraform.
- Templates and languages: one Python LangGraph template and no sample catalogue, against
  upstream's ADK templates in several languages, samples and a LangChain template.
- Lint: no type checker or spell checker in the generated project's `lint`.
- Maturity: a documentation site, a long release history and PyPI distribution are upstream
  strengths; this project has a README and skills, its first tagged release, and PyPI
  publication pending.
- Upstream fixes after 1.6.1 are ported by hand (see CONTRIBUTING.md); for example, remote
  templates still skip symlinks.

Google Cloud targets (Agent Runtime, Cloud Run, GKE-specific integrations), publishing to
Gemini Enterprise and BigQuery analytics are out of scope, not gaps.

## Known limitations

- **LangGraph Server licence.** The `langgraph-server` runtime's image
  (`langchain/langgraph-api`) checks for a LangGraph licence at startup (a LangSmith API key
  or a licence key; see LangChain's LangGraph Server documentation) and exits without one.
  Add the variable LangChain documents to `secrets.keys`. The image was verified here only up
  to that check; `langgraph dev` (used by `run` and `playground`) needs no licence.
- **A2A task store** is in process memory, per replica: `GetTask` or a resubscribe routed to
  another pod reads as not found, and tasks are dropped `A2A_TASK_TTL_S` after their last
  update. Use one replica, or sticky routing, for long A2A tasks.
- **Prompt injection** through tool results is reduced, not prevented: the fence, the prompt
  rule and the tool checks depend on the model and on your tools, and an approval gate is
  only as good as the human reading the call (a `requester` gate trusts the user to notice a
  record they did not ask about). See [Security model](#security-model).
- **No built-in inbound rate limiting**: configure it at the gateway or ingress (outbound calls
  have per-API `limits` in `api-policy.yaml`).
- **Outbound `limits` are per process.** `rate_per_minute` is a token bucket in each replica
  (N replicas allow N times the rate), and `max_calls_per_run` is counted in the process that
  runs the run; neither is shared across replicas. Rely on the upstream API's own quota for a
  global cap.
- **Run lock across replicas** is a lease with a 30 s expiry: a thread whose run was on a
  replica that died (not one that shut down cleanly) answers 409 `thread_busy` for up to
  30 s. Each replica keeps one extra connection for its leases. The lease is checked in the
  process before each write, not by the database in the same transaction: a write already
  sent when a network partition starts can land after another replica took the thread
  (normal reads still follow the newer run's checkpoints).
- **Rolling upgrades from an older build**: old pods (0.1.0 has no run lock across replicas;
  pre-release 0.2.0 builds used a Postgres session advisory lock) and new pods (lease) do not
  exclude each other while both run, so during the rollout one thread can run on an old and
  a new pod at once. Upgrade with a `Recreate` rollout, or at one replica.
- **A database that stops answering without closing its connections** (a paused host, a
  proxy that holds traffic) is found by `/ready` and by TCP timeouts, not within the few
  seconds a refused connection takes: requests on connections already open can wait up to
  about a minute.
- **Concurrent deploys to one release.** `deploy` refuses while another helm operation holds
  the release, but two narrow races remain. If this run's helm fails before recording a
  revision (a render error) just as another deploy records a revision that has already
  failed, that revision is attributed to this run (and, with `--atomic`, rolled back). A
  deploy that passes the idle check can still apply its Secret before helm refuses it,
  when another deploy starts in between. Serialize deploys to one environment (one CI
  concurrency group, one operator at a time).
- **`jwt`**: one issuer; no tenant or scope claims mapped to permissions (every
  authenticated principal may use every action; ownership is per thread); the JWKS URL must
  answer directly (no redirects); for a PEM certificate only its public key is used.
- **`langgraph-server` specifics**: `/threads` on the public route also exposes the server's
  native thread routes, including native run creation, which skips `/chat`'s guardrails (run
  timeout, one run per thread, run records); the auth handler still limits them to the
  caller's threads. Runs started through the native API carry the caller's raw id in
  checkpoint metadata (the server injects it). The native state routes (`GET
  /threads/{id}/state`, `POST /threads/{id}/history`, `GET /threads/{id}`, search and run
  joins) return the stored state as it is, a failed tool call's error text included; only
  `/chat`, `/threads/{id}/messages` and A2A replace it with an error id. Store reads are
  open to every authenticated principal: namespace per-user data by principal.
- **Human-in-the-loop** is wired for the policy's approval gates only: an `interrupt()` of
  your own in the served graph is not exposed over `/chat` (`message.end` has no status for
  it). `run --mode a2a` prints a gated call and how to resume the task but does not prompt
  (and shows its body's numbers as A2A data carries them: `1` reads `1.0`);
  `eval generate` decides `requester` gates as the eval identity and `role:` gates as the
  one principal of `GRAPH_AGENTS_CLI_APPROVER_API_KEY`.
- `scaffold enhance` (a settings change) and `scaffold upgrade` (to a new version) rewrite the
  manifest without its comments; after
  `enhance --runtime`, run `graph-agents-cli install` to bring `uv.lock` up to date; required
  steps are reported only by the enhance that changes the settings.
- Upgrading a 0.1.0 project needs manual steps (see CHANGELOG.md) and the `v0.1.0` tag for its
  authentic baseline (or `--baseline-ref <clone>@fc3f2f9`). `--baseline current` is no
  substitute there: it cannot tell your edits from 0.2.0's changes, so every scaffolding file
  0.2.0 changed keeps its 0.1.0 content.
- The repository has no release tags yet (the owner creates them): until `v0.1.0` and `v0.2.0`
  are pushed, `uv tool install ...@v0.2.0` and the default upgrade baseline of those releases
  cannot be fetched; install from a checkout and name baselines with `--baseline-ref`.
- A project made by a build between releases is upgraded from its commit on the remote; a
  commit that was never pushed is named with `--baseline-ref <clone>@<commit>`.
- The Bitnami subcharts come from `registry-1.docker.io`, which rate-limits anonymous pulls;
  their images are pinned by digest, and a pin must be refreshed if the digest is withdrawn.
- The generated workflows reference actions by version tag, not commit SHA.

## Documentation

- [CHANGELOG.md](CHANGELOG.md): release notes and migration steps.
- [CONTRIBUTING.md](CONTRIBUTING.md): development setup, tests, templates, locks, releases
  and the upstream-sync process.
- [skills/README.md](skills/README.md): the bundled coding-agent skills and their references.
- A generated project's `README.md` and `AGENTS.md` describe that project.
- [NOTICE](NOTICE): attribution to google-agents-cli and the list of modifications.

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
