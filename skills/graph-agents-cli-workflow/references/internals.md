# Underlying commands reference

`graph-agents-cli` wraps lower-level tools and shells out to them; it never imports a Kubernetes
or Docker SDK. When you need a flag or behaviour the CLI does not expose, or when the 3-strikes
rule says stop retrying, run the underlying command directly from the project root.

Tools are located with `require_tool` (`PATH`, then well-known locations) and every subprocess is
logged; `--dry-run` on `deploy` prints the exact command lines.

## Develop and test

| `graph-agents-cli` command | Runs |
|---|---|
| `playground` (runtime `fastapi`) | `APP_ENV=dev uv run uvicorn <agent_directory>.fast_api_app:app --reload --host 127.0.0.1 --port <port>` (port 8000 by default; opens the browser unless `--no-open`) |
| `playground` (runtime `langgraph-server`) | `APP_ENV=dev uv run langgraph dev --no-browser --port <port>` (the server is the application; `/chat` and `/playground` are custom routes from `langgraph.json` `http.app`) |
| `playground --graph` (either runtime) | `uv run langgraph dev --port <port>` and opens LangGraph Studio; needs the `langgraph-cli[inmem]` dev dependency |
| `run "prompt"` (runtime `fastapi`) | checks the port first (the first free one of 18080-18089, or `--port` / `GRAPH_AGENTS_CLI_RUN_PORT`, exit 3 when taken), starts `uv run uvicorn <agent_directory>.fast_api_app:app --host 127.0.0.1 --port <port>` detached, records `.graph-agents-cli/run_server.json` (`pid`, `port`, `started_at`, `last_activity`, `runtime`, `checkpointer`, `state` `starting`/`ready`, `create_time`), `POST /chat` with `Accept: text/event-stream`, prints the reply, stops a server it started itself (also on SIGTERM/SIGHUP/Ctrl-C, with signals held until the record is removed); a server that exits during startup or misses the 60 s `GET /health` readiness check is terminated and reported (exit 2); a recorded PID now owned by another process is never signalled; local runs send `Authorization: Bearer <API_KEY>` from `.env` unless `--header`/`GRAPH_AGENTS_CLI_API_KEY` is given |
| `run "prompt"` (runtime `langgraph-server`) | same lifecycle with `uv run langgraph dev --no-browser --port <port>` (in-memory server locally) |
| `run --url URL --mode chat` | `POST <URL>/chat` (SSE) with the credential from `--header`, `GRAPH_AGENTS_CLI_API_KEY`, or `--cookie` |
| `run --url URL --mode a2a` | needs the `a2a` extra (one-line hint with the install command otherwise, before any server starts); fetches `<URL>/a2a/<agent_directory>/.well-known/agent-card.json` (then the root card), then JSON-RPC to `<URL>/a2a/<agent_directory>`; locally targets `http://127.0.0.1:<port>/a2a/<agent_directory>` without probing |
| `install` | `uv sync` (`--clean` deletes `.venv` first; `--locked` is `uv sync --locked`) |
| `lint` | `uv run ruff check .` + `uv run ruff format . --check` + the CLI's own `dev/policy_check.py`, which parses every `*.py` under `<agent_directory>/tools/` (subpackages and their `__init__.py` included, the top-level `__init__.py` excluded, symlinked directories followed once) with `ast`, reads the literal `API_CALLS` list (no import, no model SDK), validates `api-policy.yaml` with the runtime's strict schema and checks each entry against the named API's rules and, when set, its OpenAPI spec (resolved relative to the project root); prints a table, the `graph-agents-cli api` command that would allow each refused call, and the violation count; `--policy-only` skips ruff |
| `lint --fix` | `uv run ruff check . --fix && uv run ruff format .` then the policy check |
| `api add\|access\|allow\|deny\|revoke\|limits\|remove` | no subprocess: reads `api-policy.yaml` and the manifest, validates with the shared rules, edits the text at the positions PyYAML reports (comments and key order kept; the result re-parsed and compared with the intended document), prints a unified diff of `api-policy.yaml`, `graph-agents-cli-manifest.yaml`, `.env.example` and `deployment/helm/<name>/values*.yaml`, then writes each through a temporary file renamed over it (`--dry-run` stops before) |
| `api show` / `api check` | the policy check of `lint` (`api check` is exactly `lint --policy-only`) |
| `build` | `docker build -t <registry>/<name>:<tag> -f Dockerfile .` (the Dockerfile is runtime-specific: a multi-stage `python:3.12.14-slim-bookworm` image with a pinned uv in the build stage only, or `FROM langchain/langgraph-api:0.14.4-py3.12` for `langgraph-server`, checked against `uv.lock` at build time; both run as 1000:1000; `langgraph build` is never used); the registry is validated first (exit 3 for `ghcr.io/CHANGE-ME` or an invalid reference) |

## Evaluate

| Command | Runs |
|---|---|
| `eval generate` | starts the local server as `run` does (or uses `--url`), sends each case's messages to `POST /chat` on a fresh `thread_id`, derives `response`, `tool_calls`, `usage`, `latency_ms`, `status` from the SSE events, writes `artifacts/traces/traces_<ts>.json` |
| `eval grade` | deterministic `expect` checks in the CLI process first; then stages `.graph-agents-cli/judge_runner.py` into the project and runs `uv run python .graph-agents-cli/judge_runner.py in.json out.json` from the project root, which calls `app.app_utils.model.get_judge_model()` (JUDGE_* env; `--judge-provider/--judge-model` are passed as `JUDGE_MODEL_PROVIDER`/`JUDGE_MODEL_NAME`) for judge and custom metrics; writes `artifacts/grade_results/results_<ts>.json` (suffix `_2`, `_3` on a name clash); no eval service and no LangChain in the CLI |
| `eval run` | `generate` then `grade`, worst exit code wins; each stage honours an installed extension override |
| `eval analyze` | in-process deterministic clustering of non-passed cases into `artifacts/analysis_<ts>.json`; `--judge` summarises clusters through the same judge runner |
| `eval submit` | LangSmith SDK (`langsmith` extra): creates or updates the dataset and examples, then an experiment with runs and feedback from the results file |

## Deploy

| Command | Runs |
|---|---|
| `infra check` | tool presence (helm, kubectl, docker, gh, argocd when `cd: argocd`); `kubectl version` (cluster reachable, Kubernetes >= 1.28); `kubectl get crd httproutes.gateway.networking.k8s.io` then `kubectl get gatewayclass` (when `gateway.enabled`); `kubectl get ingressclass` (when `ingress.enabled`); `kubectl get crd certificates.cert-manager.io` (when `tls.certManager.enabled`); `kubectl get apiservice v1beta1.metrics.k8s.io` (when `hpa.enabled`); `kubectl get namespace argocd` + `kubectl get crd applications.argoproj.io` (when `cd: argocd`); `kubectl get namespace <ns>`, `kubectl -n <ns> get secret <pull-secret>` (from `imagePullSecrets`) and `kubectl -n <ns> get secret <name>-app` (its required keys); the `CHANGE-ME` placeholders in the manifest registry, the chart's `image.repository` and `env`, `.github/CODEOWNERS` and the Argo CD `repoURL`; `gh api repos/<owner>/<repo>/environments/production`, `.../environments/staging` and `.../branches/main/protection` when `gh` is logged in and `cd != skip`, plus (helm-push) `.../environments/<env>/secrets/DEPLOY_KUBECONFIG` and the repository/organization kubeconfig secrets; `--profile disconnected` adds the disconnected-profile checks (provider, `OPENAI_BASE_URL`, runtime, registry, tracing, CI runner labels, `GRAPH_AGENTS_CLI_NO_UPDATE_CHECK`). Read-only |
| `secrets apply --env <env>` | resolves the env file (`.env.<env>`; `.env` for dev only) and the kube context (confirmed outside dev); `kubectl create namespace <namespace>` when absent; `kubectl get secret <release>-app -o json` (live keys, merged under the file's); `kubectl -n <namespace> create secret generic <release>-app --from-env-file=<0600 temporary file holding only the allow-listed keys> --dry-run=client -o yaml \| kubectl apply --server-side --field-manager=graph-agents-cli --force-conflicts -f -`; the temporary file is deleted afterwards; a leftover `last-applied-configuration` annotation is removed; `API_KEY` is generated only when absent from the file and from the live Secret and then written to the env file (0600), never printed; multi-line values are exit 3; not refused under CI |
| `secrets status --env <env>` | `kubectl -n <namespace> get secret <release>-app -o json` and lists present, missing required, missing optional and unexpected key names; exit 1 when the Secret or a required key is missing (`--strict`: any allow-listed key), 2 when kubectl fails |
| `deploy --env dev` (direct, local-load) | a read-only check of the Secret it would produce (required keys; exit 1 before building) and of the release (`helm history`; a `pending-*` revision is exit 2); `docker build -t <registry>/<name>:<tag>` (`git rev-parse --short HEAD`, plus `-dirty-<time>` for uncommitted changes, else a UTC timestamp, or `--tag`); then `kind load docker-image` / `k3d image import` / `docker save` + `k3s ctr images import` (no sudo; usually needs root) / `minikube image load` (none for docker-desktop, rancher-desktop, orbstack), chosen from the cluster's nodes and confirmed with `kind get clusters` / `k3d cluster list` / `minikube profile list`; the Secret pipeline of `secrets apply` (skipped when dev has no env file); `helm dependency build deployment/helm/<name>` when a subchart is missing from `charts/`; `helm upgrade --install <release> deployment/helm/<name> -n <namespace> --create-namespace -f values.yaml -f values-<env>.yaml --set image.repository=...,image.tag=<tag>,existingSecret=<release>-app --wait --timeout <5m> --kube-context <context>`; on failure `kubectl get pods`, container states, warning events and logs, then `helm rollback <release> <last good>` (or `helm uninstall` of a failed first install) with `--atomic` |
| `deploy --env <env>` (direct, registry) | the same checks, `docker build`, `docker push <registry>/<name>:<tag>`, the Secret pipeline, `helm dependency build` when needed, `helm upgrade --install ... --wait --timeout` with the same failure handling |
| `deploy --image <ref> --env <env>` (helm-push, from the CI runner) | the live Secret's required keys and the release state, `helm dependency build` when needed, then `helm upgrade --install ...` only; refused outside CI (`GITHUB_ACTIONS=true`) for staging/prod even with `--image`, unless `--force-direct` |
| `deploy --env <env> --image <ref>` (argocd) | rewrites `image.tag` in `origin/main`'s copy of `deployment/helm/<name>/values-<env>.yaml` (`git cat-file blob`; the working tree is untouched), then builds branch `deploy/<env>/<short sha>` with git plumbing (`git fetch origin main`, `hash-object --stdin`, `read-tree`, `update-index` with a repo-relative path, `write-tree`, `commit-tree`, `update-ref`; the checkout is never switched), `git push --force-with-lease=refs/heads/deploy/<env>/<short sha>:<sha on origin>` (nothing when origin already holds the change), `gh pr list`/`gh pr create --repo <host>/<owner>/<repo>` (or GitHub REST with `GITHUB_TOKEN`/`GH_TOKEN`/`GH_ENTERPRISE_TOKEN`; `GH_HOST` for GitHub Enterprise Server); without `--image` the tag is `--tag` or the short git sha with a warning; never helm, never merges |
| `deploy --status --env <env>` | `kubectl -n <namespace> rollout status deploy/<release>` or, in argocd mode, `argocd app get <name>-<env>` |
| `deploy --restart --env <env>` | `kubectl -n <namespace> rollout restart deploy/<release>` (warns that Argo self-heal may revert it in argocd environments) |
| `deploy --dry-run` | prints every command above with a `[dry-run]` prefix and `helm template` output without executing, except `helm dependency build`, which is executed when subcharts are missing because the render needs them |

## Rollback

- Direct and helm-push modes: a failed rollout is rolled back by `deploy` itself (`--atomic`,
  the default), acting only on the revision that run created. For a release that deployed fine,
  `helm history <release> -n <namespace>` then `helm rollback <release> <revision> -n <namespace>`.
- argocd mode: revert the values change on `main` with a PR (the same gate applies), then let Argo
  reconcile. `argocd app history` and `argocd app rollback` are Argo-side tools for emergencies.

## Scaffold

| Command | Runs |
|---|---|
| `create` | cookiecutter layering (`base_templates/_shared` -> `base_templates/python` -> `deployment_targets/<target>` -> `agents/langgraph`), conditional-file selection by runtime and CD mode, copies `uv-<runtime>.lock` to `uv.lock` (substituting the project name), reconciles the manifest against the contract, then `uv sync`; remote `--agent` specs are shallow-cloned with `git`; the registry default comes from the git `origin` owner |
| `scaffold enhance` | renders old and new baselines and runs the 3-way merge; a backup goes to `~/.graph-agents-cli/backups/<dir>_<project id>_<timestamp>/` first (0700, the newest 5 per project kept); a runtime or provider change also merges the chart values and `.github/agent.env` key by key around your edits; when the merge changes the manifest it is rewritten through the YAML dumper (block style, comments dropped) and `.github/agent.env` is updated |
| `scaffold upgrade` | `uvx --from <install spec of the old version> graph-agents-cli scaffold create ...` to regenerate the authentic old baseline, then the 3-way merge; stops without changes (exit 2) if that version cannot be fetched unless `--baseline current`, which cannot tell edits from template changes since the old version |
