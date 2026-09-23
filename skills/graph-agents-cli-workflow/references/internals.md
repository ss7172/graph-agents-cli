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
| `run "prompt"` (runtime `fastapi`) | starts `uv run uvicorn <agent_directory>.fast_api_app:app --host 127.0.0.1 --port <18080+>` detached, records `.graph-agents-cli/run_server.json` (`pid`, `port`, `started_at`, `last_activity`, `runtime`, `checkpointer`), `POST /chat` with `Accept: text/event-stream`, prints the reply, stops a server it started itself; a server that misses the 60 s `GET /health` readiness check is terminated before the error is reported; local runs send `Authorization: Bearer <API_KEY>` from `.env` unless `--header`/`GRAPH_AGENTS_CLI_API_KEY` is given |
| `run "prompt"` (runtime `langgraph-server`) | same lifecycle with `uv run langgraph dev --no-browser --port <port>` (in-memory server locally) |
| `run --url URL --mode chat` | `POST <URL>/chat` (SSE) with the credential from `--header`, `GRAPH_AGENTS_CLI_API_KEY`, `--cookie`, or `--session-token` |
| `run --url URL --mode a2a` | needs the `a2a` extra (`uv tool install 'graph-agents-cli[a2a]'`; one-line hint otherwise, before any server starts); fetches `<URL>/a2a/<agent_directory>/.well-known/agent-card.json` (then the root card), then JSON-RPC to `<URL>/a2a/<agent_directory>`; locally targets `http://127.0.0.1:<port>/a2a/<agent_directory>` without probing |
| `install` | `uv sync` (`--clean` deletes `.venv` first; `--locked` is `uv sync --locked`) |
| `lint` | `uv run ruff check .` + `uv run ruff format . --check` + the CLI's own `dev/policy_check.py`, which parses every `<agent_directory>/tools/*.py` with `ast`, reads the literal `PRODUCT_CALLS` list (no import, no model SDK) and checks each entry against `product-policy.yaml` and, when set, the OpenAPI spec (resolved relative to the project root); prints a table and the violation count; `--policy-only` skips ruff |
| `lint --fix` | `uv run ruff check . --fix && uv run ruff format .` then the policy check |
| `build` | `docker build -t <registry>/<name>:<tag> -f Dockerfile .` (the Dockerfile is runtime-specific: a `python` base with uvicorn, or `FROM langchain/langgraph-api:<pinned>` for `langgraph-server`; `langgraph build` is never used) |

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
| `infra check` | tool presence (helm, kubectl, docker, gh, argocd when `cd: argocd`); `kubectl version` (cluster reachable, Kubernetes >= 1.28); `kubectl get crd httproutes.gateway.networking.k8s.io` then `kubectl get gatewayclass` (when `gateway.enabled`); `kubectl get ingressclass` (when `ingress.enabled`); `kubectl get crd certificates.cert-manager.io` (when `tls.certManager.enabled`); `kubectl get apiservice v1beta1.metrics.k8s.io` (when `hpa.enabled`); `kubectl get namespace argocd` + `kubectl get crd applications.argoproj.io` (when `cd: argocd`); `kubectl get namespace <ns>`, `kubectl -n <ns> get secret <pull-secret>` (from `imagePullSecrets`) and `kubectl -n <ns> get secret <name>-app`; `gh api repos/<owner>/<repo>/environments/production`, `.../environments/staging` and `.../branches/main/protection` when `gh` is logged in and `cd != skip`; `--profile disconnected` adds the D25 checks (provider, `OPENAI_BASE_URL`, runtime, registry, tracing, CI runner labels, `GRAPH_AGENTS_CLI_NO_UPDATE_CHECK`). Read-only |
| `secrets apply --env <env>` | `kubectl -n <namespace> create secret generic <release>-app --from-env-file=<0600 temporary file holding only the allow-listed keys> --dry-run=client -o yaml \| kubectl apply -f -`; the temporary file is deleted afterwards; `API_KEY` is generated and printed once only when absent from the file and from the live Secret (`kubectl get secret -o json`, re-included otherwise); multi-line values are exit 3; not refused under CI |
| `secrets status --env <env>` | `kubectl -n <namespace> get secret <release>-app -o json` and lists present/missing key names; exit 1 when the Secret or a key is missing |
| `deploy --env dev` (direct, local-load) | `docker build -t <registry>/<name>:<short sha>` (`git rev-parse --short HEAD`, else a UTC timestamp, or `--tag`); then `kind load docker-image` / `k3d image import` / `docker save` + `k3s ctr images import` (no sudo; usually needs root) / `minikube image load` (none for docker-desktop, rancher-desktop, orbstack), detected from the kube context name; the Secret pipeline of `secrets apply` (skipped with a warning when no env file exists); `helm dependency build deployment/helm/<name>` when a subchart is missing from `charts/`; `helm upgrade --install <release> deployment/helm/<name> -n <namespace> --create-namespace -f values.yaml -f values-<env>.yaml --set image.repository=...,image.tag=<short sha>,existingSecret=<release>-app --wait --kube-context <context>` |
| `deploy --env <env>` (direct, registry) | `docker build`, `docker push <registry>/<name>:<short sha>`, the Secret pipeline, `helm dependency build` when needed, `helm upgrade --install ...` |
| `deploy --image <ref> --env <env>` (helm-push, from the CI runner) | `helm dependency build` when needed, then `helm upgrade --install ...` only; refused from a workstation for staging/prod unless `--force-direct` |
| `deploy --env <env> --image <ref>` (argocd) | rewrites `image.tag` in `origin/main`'s copy of `deployment/helm/<name>/values-<env>.yaml` (`git cat-file blob`; the working tree is untouched), then builds branch `deploy/<env>/<short sha>` with git plumbing (`git fetch origin main`, `hash-object --stdin`, `read-tree`, `update-index` with a repo-relative path, `write-tree`, `commit-tree`, `update-ref`; the checkout is never switched), `git push --force-with-lease -u origin deploy/<env>/<short sha>`, `gh pr list`/`gh pr create --repo <host>/<owner>/<repo>` (or GitHub REST with `GITHUB_TOKEN`/`GH_TOKEN`/`GH_ENTERPRISE_TOKEN`; `GH_HOST` for GitHub Enterprise Server); without `--image` the tag is `--tag` or the short git sha with a warning; never helm, never merges |
| `deploy --status --env <env>` | `kubectl -n <namespace> rollout status deploy/<release>` or, in argocd mode, `argocd app get <name>-<env>` |
| `deploy --restart --env <env>` | `kubectl -n <namespace> rollout restart deploy/<release>` (warns that Argo self-heal may revert it in argocd environments) |
| `deploy --dry-run` | prints every command above with a `[dry-run]` prefix and `helm template` output without executing, except `helm dependency build`, which is executed when subcharts are missing because the render needs them |

## Rollback

- Direct and helm-push modes: `helm rollback <release> <revision> -n <namespace>` or
  `helm history <release>`.
- argocd mode: revert the values change on `main` with a PR (the same gate applies), then let Argo
  reconcile. `argocd app history` and `argocd app rollback` are Argo-side tools for emergencies.

## Scaffold

| Command | Runs |
|---|---|
| `create` | cookiecutter layering (`base_templates/_shared` -> `base_templates/python` -> `deployment_targets/<target>` -> `agents/langgraph`), conditional-file selection by runtime and CD mode, copies `uv-<runtime>.lock` to `uv.lock` (substituting the project name), reconciles the manifest against the contract, then `uv sync`; remote `--agent` specs are shallow-cloned with `git`; the registry default comes from the git `origin` owner |
| `scaffold enhance` | renders old and new baselines and runs the 3-way merge; a backup goes to `~/.graph-agents-cli/backups/<project>_<timestamp>/` first; when the merge changes the manifest it is rewritten through the YAML dumper (block style, comments dropped) and `.github/agent.env` is updated |
| `scaffold upgrade` | `uvx graph-agents-cli@<old-version> scaffold create ...` to regenerate the authentic old baseline, then the 3-way merge; stops without changes if that version cannot be fetched unless `--baseline current` |
