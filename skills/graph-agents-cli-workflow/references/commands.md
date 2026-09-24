# Command reference

Every `graph-agents-cli` command with its flags, as `graph-agents-cli <command> --help` prints
them. The help is authoritative and ends with a `Source:` line pointing at the implementing file.
Commands are loaded lazily; nothing imports a model SDK, LangGraph, or a Kubernetes client at
startup.

| Phase | Commands |
|---|---|
| Setup | `setup` · `update` · `login` |
| Scaffold | `create` (alias of `scaffold create`) · `scaffold enhance` · `scaffold upgrade` |
| Develop | `playground` · `run` · `install` · `lint` · `build` |
| Evaluate | `eval run` · `eval generate` · `eval grade` · `eval compare` · `eval analyze` · `eval submit` · `eval metric list` |
| Deploy | `infra check` · `secrets apply` · `secrets status` · `deploy` |
| Extend / inspect | `extension add\|list\|remove\|update` · `info` |

Exit codes, for every command: `0` ok; `1` refused by policy or mode, a declined confirmation,
or a failed gate (a lint violation, an agent that answered with an error, `scaffold enhance`
with required steps left); `2` tool failure (helm/kubectl/docker/git/gh non-zero or missing from
`PATH`, a local server that cannot start, an agent that cannot be reached, `uvx` missing or unable
to fetch the prior release for `scaffold upgrade` or a version-locked `scaffold enhance`, an
unexpected crash); `3` configuration error (not in a project, an invalid manifest, including a
missing or unreleased `cli_version` for `scaffold upgrade`, env file, policy, port or kube
context, an unusable `GRAPH_AGENTS_CLI_INSTALL_SPEC`). A signal ends a command with 128+N (130 for Ctrl-C, 143 for SIGTERM) after the local
server it started is stopped. `secrets status` exits `1` when the Secret or a *required* key is
missing (`--strict`: any allow-listed key). `eval` exit codes are in `/graph-agents-cli-eval`.
`GRAPH_AGENTS_CLI_DEBUG=1` shows the traceback behind a one-line network, file or parse error.

## Setup

```
graph-agents-cli setup [--workspace] [--dry-run] [--dev] [--skills-source TEXT] [--agent TEXT]...
graph-agents-cli update [--workspace] [-i/--interactive] [-y/--yes]
graph-agents-cli login [--profile default|disconnected] [--cluster] [--write-env] [--env-file FILE] [--status] [--json]
```

- `setup` installs the CLI (`uv tool install <install spec>`: the running version's git tag, or
  `GRAPH_AGENTS_CLI_INSTALL_SPEC`) and the six `graph-agents-cli-*` skills into detected coding
  agents through `npx skills add` from this repository at the running release's tag
  (`https://github.com/ss7172/graph-agents-cli#v<version>`; the default branch for a development
  build), falling back to the wheel-bundled copy, then to a direct copy into `~/.agents/skills`
  (`./.agents/skills` with `--workspace`). `--agent` is repeatable (`claude-code`, `cursor`, ...
  or `all`); `--dev` installs the CLI editable from the checkout and the skills from it;
  `--skills-source` picks another source (no fallback). It performs no authentication.
- `update` refreshes the skills (`npx skills update`), then reinstalls the CLI from the latest
  GitHub release (`uv tool install --force <install spec>`; skipped when there is no newer
  release; a failure is a warning, a malformed `GRAPH_AGENTS_CLI_INSTALL_SPEC` is exit 3) and
  installs that release's skills from its tag.
- `login` is a preflight check, not an authentication: the provider key for `MODEL_PROVIDER`
  (`OPENAI_BASE_URL` for `openai-compatible`), the judge key, `LANGSMITH_API_KEY` or an OTLP
  endpoint when `TRACING_ENABLED=true`, the kube context (`--cluster` also runs
  `kubectl cluster-info`). Provider precedence: `MODEL_PROVIDER` from the environment or `.env`
  (the value the app reads at runtime) > the manifest's `create_params.model_provider` > `openai`;
  `MODEL_PROVIDER=fake` is accepted as the test-only provider (warning, no key check, allowed
  under the disconnected profile) and `JUDGE_MODEL_PROVIDER=fake` is ok. `--write-env` prompts for
  missing keys without echoing and writes them to `.env` (default `<project>/.env`, or
  `--env-file`): a blank `KEY=` line (also `export KEY=` and `KEY=""`) is filled in place, other
  keys are appended, the file is written atomically and kept at mode 0600. Under the
  `shared-bearer` auth policy it also generates a missing `API_KEY` (an unset one is a warning,
  since the local server answers 503 without it). With stdin closed it writes what it has and
  names the keys left unset. Exit `1` when any check fails; `--status` prints the report and
  exits `0`; `--json` emits the report. `--profile disconnected` fails on any hosted dependency. The CLI stores no
  credentials.

## Scaffold

```
graph-agents-cli create [PROJECT_NAME]
  -a/--agent TEXT           langgraph (default) | local@<path> | <org>/<repo>/<path>@<ref> | https://github.com/org/repo/tree/main/path
  -o/--output-dir PATH      parent directory (default: current directory)
  --runtime fastapi | langgraph-server                              (default: fastapi)
  --model-provider openai | anthropic | gemini | openai-compatible  (default: openai; prompted with -i)
  --model TEXT                                                      (provider default if omitted)
  --checkpointer memory | postgres                                  (default: postgres for kubernetes, memory for none)
  -d/--deployment-target kubernetes | none                          (default: kubernetes)
  --registry TEXT                                                   (default: ghcr.io/<git origin owner>)
  --cd argocd | helm-push | skip                                    (default: skip; requires --deployment-target kubernetes)
  --auth-policy shared-bearer | jwt | custom                        (default: shared-bearer)
  --api-policy FILE                                                 (validated, then seeds api-policy.yaml; optional)
  --process TEXT                                                    (path or string recorded as process: and rendered into the guidance file)
  -p/--prototype                                                    (target defaults to none unless given; CD forced to skip)
  -dir/--agent-directory TEXT   --agent-guidance-filename TEXT (default AGENTS.md)   -bt/--base-template TEXT (remote templates only)
  -i/--interactive   -y/--auto-approve/--yes   -s/--skip-checks (skips only the uv-on-PATH preflight)   --debug
graph-agents-cli scaffold create [PROJECT_NAME] ...                 (same command)
graph-agents-cli scaffold enhance [TEMPLATE_PATH]
  -n/--name TEXT plus the create flags above except --api-policy (--runtime, --model-provider,
  --model, --checkpointer, -d/--deployment-target, --registry, --cd, --auth-policy, --process, -p,
  -dir, --agent-guidance-filename, -bt, -i, -y, -s, --debug) and
  --force   --dry-run/--dryrun   --prefer-new
  (api-policy.yaml belongs to the project: enhance never touches it; use graph-agents-cli api)
graph-agents-cli scaffold upgrade [PROJECT_PATH] [--dry-run/--dryrun] [-y/--auto-approve/--yes] [-i/--interactive]
  [--baseline authentic|current] [--debug]
```

`enhance` always enhances the current directory; `TEMPLATE_PATH` names the template to apply and
is ignored when the manifest records one. A runtime or model-provider change is applied to every
file it shapes (chart values and `.github/agent.env` merged key by key around your edits) and ends
with a "Left for you" list; steps marked `(required)` make `enhance` exit 1. Backups go to
`~/.graph-agents-cli/backups/<dir>_<project id>_<timestamp>` (private; the newest 5 per project are
kept). Full flag tables and the valid combinations: the `flags.md` reference of
`/graph-agents-cli-scaffold`.

## Develop

```
graph-agents-cli playground [--port INTEGER] [--graph] [--no-open]
graph-agents-cli run MESSAGE [--mode chat|a2a] [--url TEXT] [--thread-id TEXT]
  [-H/--header 'Key: Value']... [--cookie name=value]... [-f/--file FILE]...
  [--start-server] [--stop-server] [--port INTEGER] [-v/--verbose]
graph-agents-cli install [--clean] [--locked]
graph-agents-cli lint [--fix] [--policy-only]
graph-agents-cli build [--tag TEXT] [--registry TEXT] [--push] [--dry-run]
```

- `playground`: the selected application with reload and the `/playground` page (`APP_ENV=dev`),
  default port 8000 (a port in use is refused with exit 3 and a free one suggested), browser
  opened unless `--no-open`. Ctrl-C, SIGTERM or SIGHUP stops the whole server tree. `--graph` runs `langgraph dev` under
  either runtime for LangGraph Studio; it bypasses the auth policy and the chat API.
- `run`: default `--mode chat` against the local server it starts (tracked in
  `.graph-agents-cli/run_server.json`) on the first free port of 18080-18089, or on `--port` /
  `GRAPH_AGENTS_CLI_RUN_PORT` (exit 3 when that port is taken), or against `--url`. Credentials per auth policy:
  `--header` or `GRAPH_AGENTS_CLI_API_KEY` for `shared-bearer` (a local run falls back to the
  `API_KEY` in `.env`); `--header 'Authorization: Bearer <token>'` for `jwt`; `--header` or
  `--cookie` for `custom`. `--file` attaches UTF-8 text files as extra context. `--start-server` keeps
  the local server for later runs (idle timeout 30 minutes); `--stop-server` stops it. `-v` prints
  every SSE event as JSON. The footer's "Resume with" line prints credential flags redacted
  (`--header 'Authorization: <redacted>'`, `--cookie name=<redacted>`);
  re-supply them. A turn silent for 600 s is reported as "no event from the agent" and leaves the
  server running (a one-off server is still stopped). `--mode a2a` needs the optional `a2a` extra
  (the hint prints the `uv tool install` command) and fails with a one-line hint before any server
  starts when it is absent. Exit codes: `0` answered, `1` the agent refused or reported an error,
  `2` the agent could not be reached or went silent (or the local server could not start), `3`
  configuration error. A signal during `run` stops the server it started before exiting.
- `install`: `uv sync` (`--clean` recreates `.venv`; `--locked` asserts `uv.lock` matches
  `pyproject.toml`) plus re-materialising vendored extensions.
- `lint`: `ruff check` and `ruff format --check` (`--fix` applies both) plus the static
  API-policy check: `api-policy.yaml` passes the strict schema, and every `*.py` under
  `app/tools/` (subpackages included, the top-level `__init__.py` excluded) declares one literal
  `API_CALLS`, read with `ast` by the CLI and checked against the named API's rules and, when set,
  its OpenAPI spec; `API_CALLS` changed anywhere else (`+=`, `.append()`, a conditional) is a
  violation. A leftover `PRODUCT_CALLS` is an error; a
  project still on `product-policy.yaml` stops with migration steps (exit 3).
  `--policy-only` skips ruff. Each refused call is followed by the `graph-agents-cli api`
  command that would allow it (a reviewed change; propose it, do not run it unasked).
- `build`: `docker build -t <registry>/<name>:<tag> -f Dockerfile .` (default tag `latest`;
  `--registry` overrides the manifest; `--push` pushes; `--dry-run` prints the commands). Exit `2`
  on a docker failure, `3` without a Dockerfile or with a placeholder (`ghcr.io/CHANGE-ME`) or
  invalid image reference (checked before docker runs, `--dry-run` included).


## Outbound API policy

```
graph-agents-cli api add NAME --base-url-env ENV --auth none|bearer|forward [--token-env ENV]
  [--forward-header H] --access read-only|read-write|custom [--methods M,...] [--openapi PATH]
  [--max-calls-per-run N] [--rate-per-minute N] [--connect-timeout-ms N] [--read-timeout-ms N] [--dry-run]
graph-agents-cli api access NAME read-only|read-write|custom [--methods M,...] [--dry-run]
graph-agents-cli api allow NAME (OPERATION_ID | --method M --path P) [--methods M,...] [--dry-run]
graph-agents-cli api deny NAME (OPERATION_ID | --method M --path P) [--dry-run]
graph-agents-cli api revoke NAME (OPERATION_ID | --method M --path P) [--from allowed|denied] [--dry-run]
graph-agents-cli api limits NAME [--max-calls-per-run N|none] [--rate-per-minute N|none] [--dry-run]
graph-agents-cli api remove NAME [--dry-run]
graph-agents-cli api show [NAME] [--json]
graph-agents-cli api check
```

- `api-policy.yaml` belongs to the project and evolves with the agent; `create --api-policy`
  only seeds it. There is no default access: `--access` is required on `add`; `read-only` writes
  `[GET, HEAD]`, `read-write` writes `[GET, HEAD, POST, PUT, PATCH, DELETE]`, `custom` writes
  `--methods` (case-insensitive, stored upper-case; `"*"` alone for every method). The file never
  stores a preset name.
- Every mutating command loads and validates the current file, applies one change, validates the
  result, prints a unified diff of each file it touches (the policy, the manifest's `api_policy`
  and `secrets.keys`, `.env.example`, the chart's `values.yaml` `env`), keeps comments and key
  order, and writes atomically; `--dry-run` prints the diff only. It says whether access widens
  (a reviewed change: CODEOWNERS covers `api-policy.yaml`) or narrows, and which declared calls
  become allowed or refused. What it cannot edit safely is listed under "Left for you".
- `add` creates the file when absent, copies an `--openapi` spec outside the project to
  `openapi/<name>/`, adds a bearer `token_env` to `secrets.keys`, and documents the variables in
  `.env.example` and the chart values (placeholder `http://CHANGE-ME`). `forward` is refused under
  `langgraph-server`.
- `allow` on an API without `allowed_operations` creates the list, which narrows access from
  every operation within `allowed_methods` to the listed ones: the command says so. With
  `openapi:` recorded, `allow` and `deny` by operation id check that the id exists and fill in its
  method and path.
- `revoke` removes the entries naming the operation (only one method of an entry pinning several
  when `--method` is given); `--from` picks the list when both match; removing the last
  `allowed_operations` entry is refused (it would allow every operation).
- `remove` drops the API (and its token from `secrets.keys` when no other API uses it); the last
  one removes `api-policy.yaml` and the manifest's `api_policy`, so every call is refused.
- `show` prints the effective policy per API (auth, methods and preset, allowed and denied
  operations, limits, openapi, timeouts) and every tool's declared calls with their status and
  hint; `check` is `lint --policy-only` (same exit codes).
- Exit codes: `0` changed (or nothing to change), `1` `check` found a refused call, `2` usage
  error, `3` an invalid result (nothing written), an invalid current file, or not in a project.
## Evaluate

```
graph-agents-cli eval run      [--dataset TEXT] [--url TEXT] [--concurrency N] [-H/--header]... [--cookie]...
                               [--app-name TEXT] [--timeout SECONDS] [--config PATH] [-o/--output TEXT]
                               [--judge-provider TEXT] [--judge-model TEXT] [--judge-timeout SECONDS]
graph-agents-cli eval generate [--dataset TEXT] [-o/--output TEXT] [--url TEXT] [--concurrency N] [-H/--header]... [--cookie]...
                               [--app-name TEXT] [--timeout SECONDS]
graph-agents-cli eval grade    [--traces PATH] [--dataset TEXT] [--config PATH] [-o/--output TEXT]
                               [--judge-provider TEXT] [--judge-model TEXT] [--judge-timeout SECONDS]
graph-agents-cli eval compare  BASELINE CANDIDATE [--fail-on-regression] [--json]
graph-agents-cli eval analyze  [--results TEXT] [--output TEXT] [--top-k N] [--judge] [--judge-provider TEXT] [--judge-model TEXT]
graph-agents-cli eval submit   [--results TEXT] [--traces TEXT] [--dataset TEXT] [--dataset-name TEXT] [--experiment TEXT] [--endpoint TEXT]
graph-agents-cli eval metric list [--json]
```

Datasets `tests/eval/datasets/*.json` (`--dataset` defaults to `basic-dataset.json`, else every
file); config `tests/eval/eval_config.yaml`; traces `artifacts/traces/traces_<ts>.json`; results
`artifacts/grade_results/results_<ts>.json`; analyses `artifacts/analysis_<ts>.json` (a `_2`,
`_3`, ... suffix is added when two runs land in the same second). `eval run` chains generate and
grade and returns the worse exit code; it honours extension overrides of both `eval.generate` and
`eval.grade`. `eval grade` defaults to the newest traces file. `eval submit` uploads the dataset
and a results file to LangSmith as an experiment (needs `LANGSMITH_API_KEY` and the `langsmith`
extra; optional, never required). Details: `/graph-agents-cli-eval`.

## Deploy

```
graph-agents-cli infra check [--env TEXT] [--profile disconnected] [--json]
graph-agents-cli secrets apply  --env TEXT [--env-file TEXT] [--context TEXT] [-y/--yes] [--rotate-api-key] [--dry-run]
graph-agents-cli secrets status --env TEXT [--context TEXT] [--strict] [--dry-run]
graph-agents-cli deploy --env TEXT [--image TEXT] [--env-file TEXT] [--context TEXT] [-y/--yes] [--status] [--restart]
  [--force-direct] [--dry-run] [--tag TEXT] [--timeout DURATION] [--atomic/--no-atomic] [--rotate-api-key]
```

- `infra check` is read-only: reports the required tools and the kube context, Gateway API CRDs
  and `GatewayClass`es, `IngressClass`es, cert-manager (only when `tls.certManager.enabled`),
  Argo CD (only when `cd: argocd`), metrics-server (only when `hpa.enabled`), the namespace, the
  image pull secret, the app Secret and its required keys, every `CHANGE-ME` placeholder
  (registry, chart image and env, CODEOWNERS, Argo CD `repoURL`), and, when `gh` is logged in,
  the GitHub `production`/`staging` environments, `main` branch protection and (helm-push) the
  `DEPLOY_KUBECONFIG` environment secrets. `--profile disconnected` adds the
  disconnected-profile checks (no hosted dependency). Never creates anything.
- Env file and context rules (`deploy` and `secrets apply`): `--env-file`, else `.env.<env>`;
  only `dev` falls back to `.env` (exit 3 otherwise). The kube context is `--context`, else
  `environments.<env>.context`, else the kubeconfig's current one, which outside `dev` needs a
  confirmation prompt or `--yes` (exit 1 without); an explicit context missing from the kubeconfig
  is exit 3. The context and API server are always printed first.
- `secrets apply` creates the namespace when missing and applies the Opaque Secret
  `<release>-app` from the allow-listed keys (`secrets.keys` in the manifest) of the env file with
  server-side apply (a 0600 temporary `--from-env-file` piped into `kubectl apply --server-side`);
  allow-listed keys the file leaves out are kept from the live Secret. The live `API_KEY` wins
  unless the file sets another and `--rotate-api-key` is passed; a missing key is generated and
  written to the env file (0600), never printed. It is not refused under CI; keeping application
  secrets out of CI is the documented procedure. `--dry-run` prints the pipeline and a redacted
  manifest. `secrets status` lists present, missing required, missing optional and unexpected
  keys without values: exit `0` all required keys present, `1` the Secret or a required key missing
  (`--strict`: any), `2` kubectl failed, `3` configuration error.
- `deploy` behaviour depends on `create_params.cd` and the kube context (see the mode table in
  `/graph-agents-cli-deploy`). `--tag` sets the tag of a local build (default: the short git sha,
  plus `-dirty-<time>` for uncommitted changes, else a UTC timestamp); in argocd mode without
  `--image` it is the tag written into the values file. Before building anything, direct mode
  checks that the Secret it would produce holds every required key (exit 1) and that no other helm
  operation holds the release (exit 2). helm runs with `--wait --timeout <--timeout, default 5m>`;
  a failed rollout prints pod diagnostics and, with `--atomic` (default), rolls back this run's
  revision (or uninstalls a first install that never succeeded). `--status` wraps `kubectl rollout
  status` or `argocd app get`; `--restart` runs `kubectl rollout restart` (after secret rotation);
  `--force-direct` allows a workstation deploy to staging/prod in `helm-push` mode, which is
  otherwise refused outside CI even with `--image`; `--dry-run` prints the docker, helm, kubectl
  and gh commands and the rendered manifests without running them and never prompts (except
  `helm dependency build`, which is executed when subcharts are missing because the render needs
  them). `deploy --env staging|prod` refuses while the manifest has
  `auth_policy_implemented: false`.

## Extensions and info

```
graph-agents-cli extension add REFERENCE [--global] [--ref TEXT] [-i/--interactive] [-y/--yes]
graph-agents-cli extension list
graph-agents-cli extension update [NAME] [-i/--interactive] [-y/--yes]
graph-agents-cli extension remove NAME [-i/--interactive] [-y/--yes]
graph-agents-cli info [--json]
```

`extension add` takes a git reference (`org/repo`, a URL, `--ref`) or a local path (`/abs`,
`./rel`, `../rel`, `~/dir`, or `local@<path>`); a local source is recorded relative to the project
root (absolute with `--global`). A bad local path is exit 3, a git or network failure exit 2.
`extension update` reports "Already up to date" when nothing changed.

`info` prints the CLI version and install path plus, inside a project: name, base template,
agent directory, runtime, model provider and model, checkpointer, deployment target, registry,
CD mode, auth policy, the API policy file (or none), `process`, the environments with their
namespaces, and active extensions with their sources and conflicts.

## Environment variables (CLI side)

| Variable | Effect |
|---|---|
| `GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1` | disables the GitHub release check and the skills-version check (disconnected profile) |
| `GRAPH_AGENTS_CLI_INSTALL_SPEC` | where `setup`, `update`, the `scaffold upgrade` baseline and generated projects' CI install the CLI from (a mirror, a wheel); `{version}` is replaced by the version needed; control characters and whitespace are refused (exit 3), except the spaces of `name @ url` |
| `GRAPH_AGENTS_CLI_RUN_PORT` | port of the local server `run` and `eval generate` start |
| `GRAPH_AGENTS_CLI_DEBUG=1` | print the traceback behind a one-line network, file or parse error |
| `GRAPH_AGENTS_CLI_API_KEY` | bearer key that `run --url` and `eval generate --url` send when `--header` is absent |
| `GRAPH_AGENTS_CLI_E2E=1` | opts the CLI repository's slow end-to-end test suite in (contributors only) |
| `GRAPH_AGENTS_CLI_DISABLE_OVERRIDES=1` | bypass extension overrides (set automatically inside an override) |
| `GRAPH_AGENTS_CLI_EXTENSION_DIR` | set for an override's process: the extension's directory |
| `GRAPH_AGENTS_CLI_EXPERIMENTS` | JSON map of experiment toggles (empty mechanism today) |
| `GRAPH_AGENTS_CLI_SKIP_VERSION_LOCK` | skip the CLI-version mismatch guard on a project |
| `GH_HOST` (or `GITHUB_HOST`, `GITHUB_SERVER_URL`) | GitHub Enterprise Server host for argocd-mode pull requests and the disconnected-profile CI check |
| `GITHUB_TOKEN`, `GH_TOKEN`, `GH_ENTERPRISE_TOKEN` | token for the REST fallback when `gh` is not installed (argocd mode) |
