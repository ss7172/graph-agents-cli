# graph-agents-cli

CLI and skills for building, evaluating, and deploying [LangGraph](https://langchain-ai.github.io/langgraph/)
agents on self-hosted Kubernetes.

`graph-agents-cli` scaffolds a LangGraph project with a chat API, an A2A endpoint, an eval
harness, a Helm chart, and GitHub Actions workflows; runs and evaluates the agent locally;
and deploys it to any Kubernetes cluster with Helm, either directly or through Argo CD. It
ships six skills that teach a coding agent (Claude Code, Antigravity, Codex, Gemini CLI,
Cursor) the same lifecycle. It is a generic tool: projects choose their auth policy
(`shared-bearer`, `jwt` or `custom`) and declare the external APIs their tools may call in
`api-policy.yaml`; nothing in the CLI or the template is specific to one consumer.

It is a fork of [google-agents-cli](https://github.com/google/agents-cli) with the Google
Cloud specific parts removed; see [NOTICE](NOTICE). Status: first milestone under
construction. See this README for the supported commands and behavior, and
[CONTRIBUTING.md](CONTRIBUTING.md) for development and verification guidance.

## Install

Prerequisites: Python 3.12+, [uv](https://docs.astral.sh/uv/getting-started/installation/),
and Node.js (for the skills installer). Deployment additionally needs `helm`, `kubectl`, a
Docker-compatible `docker` CLI, `git`, and, for Argo CD or GitHub-hosted CD, `gh`. A tool
missing from `PATH` makes `deploy` exit 2. `run --mode a2a` needs the optional `a2a` extra
(`uv tool install 'graph-agents-cli[a2a] @ git+https://github.com/ss7172/graph-agents-cli'`);
`eval submit` needs the `langsmith` extra.

```bash
uv tool install git+https://github.com/ss7172/graph-agents-cli   # the CLI (pin a release: ...@v<version>)
graph-agents-cli setup               # install the CLI and skills into your coding agents
graph-agents-cli login               # preflight: provider key, tracing, kubeconfig
```

The CLI is installed from its GitHub repository (it is not published on a package index).
`GRAPH_AGENTS_CLI_INSTALL_SPEC` overrides where `setup`, `update`, the `scaffold upgrade`
baseline and generated projects' CI (`.github/agent.env` `GRAPH_AGENTS_CLI_SPEC`) install it
from, for example a private mirror or a wheel.

`setup` installs skills with `npx skills add`, falling back to the copy bundled in the
wheel and finally to a plain copy into `~/.agents/skills` (`./.agents/skills` with
`--workspace`), so it also works without git or network. Contributors use
`graph-agents-cli setup --dev` from a checkout. The CLI stores no credentials: `login`
only checks the environment and can append missing keys to `.env` with `--write-env`, which
also generates the `API_KEY` the `shared-bearer` auth policy requires.
`login` resolves the provider as `MODEL_PROVIDER` from the environment or `.env` (the
value the app reads at runtime) > the manifest's `create_params.model_provider` >
`openai`; `MODEL_PROVIDER=fake` is accepted as the test-only provider (warning, no key
check, allowed under the disconnected profile) and `JUDGE_MODEL_PROVIDER=fake` is ok.

## Quick start

```bash
graph-agents-cli create my-agent --model-provider openai      # scaffold (fastapi runtime, cd: skip)
cd my-agent
cp .env.example .env && graph-agents-cli login --write-env    # fill in OPENAI_API_KEY, generate API_KEY
graph-agents-cli install                                      # uv sync from the bundled lock
graph-agents-cli playground                                   # app with reload + /playground chat page
graph-agents-cli eval run                                     # generate traces, grade, enforce the gate
graph-agents-cli deploy --env dev                             # helm upgrade --install on the current context
```

`create` accepts `--runtime fastapi|langgraph-server`, `--model-provider
openai|anthropic|gemini|openai-compatible`, `--model`, `--checkpointer memory|postgres`,
`--deployment-target kubernetes|none`, `--registry`, `--cd argocd|helm-push|skip`,
`--auth-policy shared-bearer|jwt|custom`, `--api-policy <file>`, `--process
<path>`, and `--prototype`; incompatible combinations are rejected by the CLI. Ask your
coding agent to "use graph-agents-cli to build ..." and the `graph-agents-cli-workflow`
skill walks the same steps. `.env.example` selects `MODEL_PROVIDER` from the manifest and
`CHECKPOINTER=memory`; set `MODEL_PROVIDER=fake` in `.env` to exercise the scaffolded
project (tests, `run`, `eval run`) with the deterministic test model and no key.

## Commands

| Command | What it does |
|---------|-------------|
| `setup [--workspace] [--dry-run] [--dev] [--skills-source TEXT] [--agent TEXT]...` | Install the CLI (`uv tool install` from the pinned git spec) and the skills into detected coding agents |
| `update [--workspace] [-i] [-y]` | Force-reinstall the skills and reinstall the CLI from the latest GitHub release (best effort) |
| `login [--profile default\|disconnected] [--cluster] [--write-env] [--env-file FILE] [--status] [--json]` | Preflight: provider key or `OPENAI_BASE_URL`, `API_KEY` under `shared-bearer`, `LANGSMITH_API_KEY` when tracing is on, kubeconfig; writes `.env` on request (generating `API_KEY`); stores nothing; exit 1 on a failed check (0 with `--status`) |
| `create [NAME]` / `scaffold create [NAME]` `[-a/--agent] [-o/--output-dir] [--runtime] [--model-provider] [--model] [--checkpointer] [-d/--deployment-target] [--registry] [--cd] [--auth-policy] [--api-policy FILE] [--process] [-p/--prototype] [-dir/--agent-directory] [--agent-guidance-filename] [-bt/--base-template] [-i] [-y] [-s/--skip-checks] [--debug]` | Create a LangGraph agent project from the template |
| `scaffold enhance [TEMPLATE_PATH]` (the `create` flags plus `[-n/--name]`, `[--force]`, `[--dry-run]`, `[--prefer-new]`; `--api-policy` is refused) | Add or change the deployment target, CD mode, or runtime of an existing project (3-way merge, backup first) |
| `scaffold upgrade [PROJECT_PATH] [--dry-run] [-y] [-i] [--baseline authentic\|current] [--debug]` | Upgrade a project to this CLI version with a 3-way merge; stops without an authentic prior baseline |
| `playground [--port INT] [--graph] [--no-open]` | Run the selected application with reload and the dev chat page (port 8000); `--graph` opens LangGraph Studio via `langgraph dev` |
| `run MESSAGE [--mode chat\|a2a] [--url] [--thread-id] [-H/--header]... [--cookie]... [-f/--file]... [--start-server] [--stop-server] [-v]` | Send one prompt to the local server (started on demand) or a deployed URL; `--mode a2a` needs the `a2a` extra |
| `install [--clean] [--locked]` | Install project dependencies with uv |
| `lint [--fix] [--policy-only]` | Ruff plus the static API-policy check: `api-policy.yaml` against the strict schema, every tool module's `API_CALLS` against it |
| `build [--tag TEXT] [--registry TEXT] [--push] [--dry-run]` | `docker build` the runtime-specific Dockerfile (default tag `latest`) |
| `eval run [--dataset] [--url] [--concurrency] [-H/--header]... [--cookie]... [--app-name] [--timeout] [--config] [-o/--output] [--judge-provider] [--judge-model] [--judge-timeout]` | `eval generate` then `eval grade`; exit code is the eval gate |
| `eval generate [--dataset] [-o/--output] [--url] [--concurrency] [-H/--header]... [--cookie]... [--app-name] [--timeout]` | Run the agent over `tests/eval/datasets/*.json`, write `artifacts/traces/` |
| `eval grade [--traces] [--dataset] [--config] [-o/--output] [--judge-provider] [--judge-model] [--judge-timeout]` | Deterministic checks in-process, then judge metrics through a runner staged into the project; write `artifacts/grade_results/` |
| `eval compare BASELINE CANDIDATE [--fail-on-regression] [--json]` | Diff two result files |
| `eval analyze [--results] [--output] [--top-k] [--judge] [--judge-provider] [--judge-model]` | Deterministic clustering of failures (judge summaries with `--judge`) |
| `eval submit [--results] [--traces] [--dataset] [--dataset-name] [--experiment] [--endpoint]` | Upload the dataset and results to LangSmith (optional `langsmith` extra) |
| `eval metric list [--json]` | List deterministic checks and built-in judges (plus the project's config) |
| `deploy --env <env> [--image] [--env-file] [--status] [--restart] [--force-direct] [--dry-run] [--tag]` | Deploy per the project's CD mode; never runs helm in an Argo CD environment |
| `secrets apply --env <env> [--env-file] [--dry-run]` / `secrets status --env <env> [--dry-run]` | Create or inspect the `<release>-app` Secret from allow-listed keys; values are never printed; `status` exits 1 when a key is missing |
| `infra check [--env] [--profile disconnected] [--json]` | Read-only prerequisite and repository-settings report; creates nothing |
| `extension add REFERENCE [--global] [--ref] [-i] [-y]` / `list` / `remove NAME [-i] [-y]` / `update [NAME] [-i] [-y]` | Manage command overrides and additions from extension repos |
| `info [--json]` | Project configuration, paths, extensions, CLI version |

Run `graph-agents-cli <command> --help` for every flag; the help ends with a `Source:` line
naming the implementing file.

## Environments and CD modes

Every Kubernetes project has three environments, `dev`, `staging`, and `prod`, each with a
values file (`deployment/helm/<name>/values-<env>.yaml`), a namespace (`<name>-<env>`), a
Secret (`<name>-app`), and, under Argo CD, an `Application`. The manifest records the kube
context and namespace per environment; `deploy --env <env>` reads `.env.<env>` when present,
else `.env`. `values-dev.yaml` enables the bundled Postgres subchart and disables the
gateway; staging and prod expect an external database via the Secret and a Gateway API
`HTTPRoute` (or `Ingress`).

The `--cd` choice at create time fixes how changes reach a cluster:

| Mode | What `deploy` does | What CI does |
|------|--------------------|--------------|
| `skip` (default) | `docker build`, load or push the image, `helm upgrade --install` on the current context, for any environment | `pr_checks` only (ruff, tests, eval gate) |
| `helm-push` | Direct deploy to `dev`; refuses `staging`/`prod` from a workstation unless `--force-direct` | `staging` workflow deploys from `main` on a self-hosted runner; `promote-to-prod` deploys behind the GitHub `production` environment |
| `argocd` | Never runs helm; opens a pull request that bumps the image tag in the environment values file (`deploy/<env>/<short sha>` branch, built with git plumbing from `origin/main` so your checkout is never switched); `--status` and `--restart` talk to the cluster | CI builds, pushes, and opens or auto-merges the desired-state PR for staging; production is merged by a human after code-owner review, then Argo CD syncs (manual sync for prod) |

Images are tagged with the short commit SHA (`${GITHUB_SHA::7}` in CI, `git rev-parse --short
HEAD` for a workstation `deploy`, or `--tag`). The chart declares the bitnami `postgresql` and
`redis` subcharts as conditional dependencies; `deploy` runs `helm dependency build` when they
are missing from `charts/` (also under `--dry-run`, since the render needs them), which needs
network to `registry-1.docker.io` unless the charts are vendored. `deploy --dry-run` prints the
helm/kubectl/docker commands and rendered manifests without running them. Exit codes: 0 ok, 1
refused by policy or mode, 2 tool failure (including a tool missing from `PATH`), 3
configuration error. For a GitHub Enterprise Server remote set `GH_HOST=<host>` (and
`GH_ENTERPRISE_TOKEN` or `GITHUB_TOKEN`) so `deploy` opens the PR against it.

Local-load dev clusters are detected from the kube context (`kind-*`, `k3d-*`, `k3s`,
`minikube`, `docker-desktop`, `rancher-desktop`, `orbstack`); the k3s path runs
`k3s ctr images import`, which needs root on most hosts.

## Secrets procedure

Secrets never live in values files or the chart. The manifest's `secrets.keys` allow-list
(provider key, `JUDGE_API_KEY`, `POSTGRES_DSN` or `DATABASE_URI`/`REDIS_URI`, `API_KEY`,
`LANGSMITH_API_KEY`, the `token_env` of every `auth: bearer` API in `api-policy.yaml`) is the
only set of variables that can reach the cluster.

1. Put the values in `.env.<env>` (or `.env`); `login --write-env` prompts for missing
   keys without echoing them and generates a missing `API_KEY`.
2. `graph-agents-cli secrets apply --env <env>` creates or replaces the Opaque Secret
   `<name>-app` in the environment's namespace with one key per allow-listed variable
   that is present (`kubectl create secret generic --from-env-file=<0600 temp file>
   --dry-run=client -o yaml | kubectl apply -f -`, so no value appears on a command
   line). `API_KEY` is generated (32 random bytes, hex) only when absent from the env file
   and from the live Secret, and printed once; an existing key is kept. Values must be
   single-line. `--dry-run` prints the pipeline and a redacted manifest (no key).
3. `graph-agents-cli secrets status --env <env>` lists which keys are present, never the
   values, and exits 1 when the Secret or any allow-listed key is missing. Rotate by
   re-running `secrets apply` with the new value, then `deploy --restart --env <env>`.

In Argo CD environments `secrets apply` is the only command besides `--status` and
`--restart` that touches the cluster from a workstation. The CLI does not refuse
`secrets apply` under CI; keeping application secrets out of CI is the procedure above
(the scaffolded workflows never hold them).

## Required GitHub settings

For `cd: helm-push` or `cd: argocd` the scaffolded workflows assume:

- **Environments** `staging` and `production`, with required reviewers on `production`
  (`promote-to-prod` waits on it).
- **Branch protection on `main`**: `pr_checks` required, at least one review, code-owner
  review required (`.github/CODEOWNERS` is generated), no self-approval, and auto-merge
  permitted (the staging desired-state PR uses `gh pr merge --auto --squash`).
- **Repository secrets**: `KUBECONFIG` for the self-hosted runner in `helm-push` mode;
  registry credentials when the registry is not GHCR (`GITHUB_TOKEN` suffices for GHCR);
  `GH_PR_TOKEN`, a fine-grained PAT or GitHub App token, for the desired-state pull
  requests. A pull request opened with the workflow `GITHUB_TOKEN` does not trigger
  `pr_checks` (GitHub never starts workflows from `GITHUB_TOKEN` events), so auto-merge
  on a required check needs `GH_PR_TOKEN`; the workflows use
  `secrets.GH_PR_TOKEN || secrets.GITHUB_TOKEN`.
- **A self-hosted runner** with network access to the cluster for `helm-push`.
- **Argo CD** with a repository credential for this repo and the `deployment/argocd/`
  `Application` manifests applied once by an operator (`argocd` mode).

`graph-agents-cli infra check --env <env>` reports these settings when `gh` is logged in
(or `GITHUB_TOKEN` is set) and reports the cluster prerequisites (Gateway API CRDs and
classes or ingress class, cert-manager when `tls.certManager.enabled`, Argo CD when
`cd: argocd`, metrics-server when the HPA is enabled, namespace, image pull secret, the
app Secret). It creates nothing.

## Disconnected profile

"Runs locally" means the orchestration runs on your machine; "runs disconnected" means the
whole lifecycle works without internet access. The disconnected profile is:

- `MODEL_PROVIDER=openai-compatible` with `OPENAI_BASE_URL` at an on-network server
  (vLLM, TGI, Ollama) and a tool-capable model; the judge uses the same mechanism via
  `JUDGE_*`.
- Runtime `fastapi`. LangGraph Server is excluded: the licensing requirement of the
  deployed `langchain/langgraph-api:3.12` image is unverified (the
  local `langgraph dev` server was verified to start with no LangSmith key).
- Dependencies from a private index (`UV_INDEX_URL`, `install --locked`); base images
  mirrored into your registry; the chart's `postgresql` and `redis` subcharts vendored
  under `deployment/helm/<name>/charts/` (otherwise `deploy` fetches them from
  `registry-1.docker.io`, exit 2 when unreachable).
- Tracing off, or `TRACING_ENABLED=true` with `OTEL_EXPORTER_OTLP_ENDPOINT` to an
  in-cluster collector; no LangSmith.
- `GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1` so the CLI skips the PyPI and skills checks (they
  also fail silently offline); skills installed from the wheel bundle.
- `cd: skip` with direct-mode `deploy`, unless an on-network GitHub Enterprise Server hosts
  Actions.

CI/CD caveats: CI/CD is outside the disconnected profile unless a
GitHub Enterprise Server is on-network. `infra check --profile disconnected` and
`login --profile disconnected` treat GitHub-hosted runner labels (`ubuntu-*`, `windows-*`,
`macos-*` in `.github/workflows`) or a `GITHUB_ACTIONS` environment as outside the
profile, warn on `cd != skip` without such labels, and expect an on-network GHES to be
declared with `GH_HOST` (or `GITHUB_HOST` / `GITHUB_SERVER_URL`), the same variable
`deploy` uses to open pull requests against it. `login --profile disconnected` also
warns (does not fail) when `OPENAI_BASE_URL` is unreachable and when
`GRAPH_AGENTS_CLI_NO_UPDATE_CHECK` is not `1`.

`graph-agents-cli login --profile disconnected` and `infra check --profile disconnected`
verify these conditions and fail on any hosted dependency (hosted model provider or
judge, `LANGSMITH_API_KEY`, `TRACING_ENABLED` without an OTLP endpoint, GitHub-hosted
runners in the workflows, `langgraph-server`, a registry that is not on-network).

## Egress and privacy

Selecting a hosted provider sends prompts, tool results, and whatever context the agent
assembles to that provider; enabling LangSmith sends traces there. Tracing is off by default
and `TRACE_CAPTURE=metadata` omits tool arguments and results from exported traces. Decide
what may leave your network before connecting a hosted model.

## Documentation

- [skills/README.md](skills/README.md): bundled coding-agent skills and references.
- [CONTRIBUTING.md](CONTRIBUTING.md): development setup, tests, templates and locks.
- [NOTICE](NOTICE): attribution to google-agents-cli and the list of modifications.

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
