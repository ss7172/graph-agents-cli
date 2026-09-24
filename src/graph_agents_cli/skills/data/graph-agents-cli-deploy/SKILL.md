---
name: graph-agents-cli-deploy
description: >
  This skill should be used when the user wants to "deploy an agent",
  "deploy to Kubernetes", "deploy to our cluster", "set up CI/CD", "set up
  Argo CD", "configure secrets", "rotate a key", "promote to production",
  "check cluster prerequisites", "deploy to kind/k3s/minikube", "deploy
  air-gapped", or "troubleshoot a deployment". Covers the deployment modes
  (direct local-load, direct registry, helm-push, argocd), the dev/staging/
  prod environments, the secrets procedure and rotation, the Argo CD PR
  flow and the single production merge gate, the required GitHub
  environment and branch-protection settings, `infra check`, local-load dev
  clusters, and the disconnected profile. Part of the graph-agents-cli
  skills suite. Do NOT use for agent code (graph-agents-cli-langgraph-code),
  evaluation (graph-agents-cli-eval), scaffolding
  (graph-agents-cli-scaffold), or tracing (graph-agents-cli-observability).
metadata:
  author: graph-agents-cli contributors
  license: Apache-2.0
  version: "0.2.0"
  requires:
    bins:
      - graph-agents-cli
    install: "uv tool install git+https://github.com/ss7172/graph-agents-cli@v0.2.0"
---

# Deployment guide

> **Requires:** `graph-agents-cli`, plus `docker`, `helm`, `kubectl`, `git`, a kubeconfig, `gh` for
> GitHub-hosted CD, and `argocd` when `cd: argocd`. The CLI shells out to these; it never installs cluster components. If the
> project has no deployment target, `/graph-agents-cli-scaffold` adds one with `scaffold enhance`.

> **Never deploy without explicit human approval.** In `argocd` mode you open a PR; a code owner
> merges it. You never merge it.

## Reference files

| File | Contents |
|---|---|
| `references/kubernetes.md` | Helm chart values and render-time refusals, pod security, probes, published routes (`route.publicPaths`), traffic entry (Gateway API / Ingress / TLS), metrics and NetworkPolicy, Postgres and Redis toggles, HPA/PDB, local-load dev clusters, rollback, disconnected profile |
| `references/gitops.md` | The argocd mode: `Application` manifests, the `deploy` PR flow, staging auto-merge, production promotion, rollback by revert, `--restart` under self-heal |
| `references/secrets.md` | The Secret contract, allow-listed and required keys, env-file and context rules, `secrets apply/status` (server-side apply, key merge, `API_KEY` handling), ownership in CD modes, rotation |
| `references/github-settings.md` | Required GitHub environment and branch-protection settings, `CODEOWNERS`, secrets and variables (`DEPLOY_KUBECONFIG`, `GH_PR_TOKEN`), the self-hosted runner, what `infra check` reports |

---

## Deployment modes (determined by the manifest's `cd` and the environment)

| Mode | Who applies the release | What `deploy` does |
|---|---|---|
| *direct, local-load* (`cd: skip`, local cluster) | `deploy` | builds the image with the `docker` CLI, loads it into the kind/k3d/k3s/minikube node (nothing for Docker Desktop, Rancher Desktop, OrbStack), applies the Secret from the allow-listed keys of the env file, runs `helm upgrade --install --wait` |
| *direct, registry* (`cd: skip`, any other cluster) | `deploy` | builds, pushes to the manifest's registry, applies the Secret, runs `helm upgrade --install --wait` |
| *helm-push* (`cd: helm-push`) | GitHub Actions on a self-hosted runner | CI builds and pushes; the runner job runs `deploy --env <env> --image <ref> --context <ctx> --yes`, which only runs helm (after checking the live Secret's required keys). From a workstation `dev` is allowed; `staging`/`prod` are refused outside CI (`GITHUB_ACTIONS=true`) **even with `--image`**, unless `--force-direct` |
| *argocd* (`cd: argocd`) | Argo CD inside the cluster | **`deploy` never runs helm, never contacts the cluster and never merges.** `deploy --env <env> --image <ref>` writes the desired state: it updates `image.tag` in `deployment/helm/<name>/values-<env>.yaml` (and nothing else), commits on a branch `deploy/<env>/<short sha>` built with git plumbing from `origin/main` (the developer's checkout is never switched), and opens or updates a pull request with `gh` (else GitHub REST with `GITHUB_TOKEN`; `GH_HOST=<host>` for a GitHub Enterprise Server). `--image` names the CI-pushed image; without it the tag is `--tag` or the short git sha with a warning. Argo reconciles from `main`, so nothing reaches the cluster until the PR merges, and a PR touching `values-prod.yaml` merges only after the production approval, whoever opened it. In argocd environments only `--status`, `--restart`, and `secrets` touch the cluster |

Local-load is decided from the cluster itself (its nodes), confirmed with `kind get clusters`,
`k3d cluster list` or `minikube profile list`; the context name decides only when the nodes
cannot be read. `deploy --dry-run` prints the docker, helm, kubectl, and gh commands plus the
rendered manifests without running anything and never prompts, with one exception:
`helm dependency build deployment/helm/<name>` is printed with the `[dry-run]` prefix and still
executed when a declared subchart is missing from `charts/`, because it only writes into the
chart directory and the `helm template` render needs the subcharts. Use `--dry-run` to show the
user what will happen.

Images are tagged with the **short commit SHA**: `${GITHUB_SHA::7}` in the `staging` workflow,
`git rev-parse --short HEAD` for a workstation `deploy`, plus `-dirty-<YYYYmmddHHMMSS>` (with a
warning) when the tree has uncommitted changes outside `deployment/`, `.github/`, `tests/` and
`docs/`; a UTC timestamp outside git; `--tag TAG` overrides. The argocd branch is
`deploy/<env>/<short sha>` and the `promote-to-prod` `image_tag` input is a short sha. A
placeholder registry (`ghcr.io/CHANGE-ME`) or an invalid image reference is exit 3 before
`docker` runs. The chart refuses an empty `image.tag` and an unquoted numeric one.

Exit codes: `0` ok; `1` refused (policy or mode, a declined or missing context confirmation, a
Secret missing a required key); `2` tool failure (helm/kubectl/docker non-zero or missing from
`PATH`, a failed rollout, another helm operation holding the release, `helm dependency build`
failing because `registry-1.docker.io` is unreachable); `3` configuration error (no env file
outside dev, an unknown context, a placeholder registry, a blank `gateway.parentRef.name`).

## Environments

`dev`, `staging`, `prod`. Each has a values file `deployment/helm/<name>/values-<env>.yaml`, a
namespace `<name>-<env>` (recorded with its kube context under `environments:` in the manifest),
a Secret `<name>-app`, and under argocd an `Application`. `dev` may be a local-load cluster.

| Values file | Defaults |
|---|---|
| `values-dev.yaml` | `env.APP_ENV=dev`, `secretOptional: true`, `postgresql.enabled=true` (bundled subchart; Redis too under `langgraph-server`), `gateway.enabled=false` |
| `values-staging.yaml` | `secretOptional: false` (pods need the Secret), `postgresql.enabled=false` (external DSN from the Secret), gateway on, hostnames blank for the operator to fill |
| `values-prod.yaml` | as staging, plus `replicaCount: 2`, a PDB, topology spread and larger requests |

## Rules `deploy` and `secrets apply` follow

- **Env file:** `--env-file`, else `.env.<env>`. Only `dev` falls back to `.env`; any other
  environment without one exits 3 (local development keys never reach staging or prod).
- **Kube context:** `--context`, else `environments.<env>.context`, else the kubeconfig's current
  context. Outside `dev` the current context needs a confirmation (a prompt at a terminal,
  `--yes` otherwise; exit 1 without). An explicit context missing from the kubeconfig is exit 3.
  The resolved context and API server are printed before anything happens. Prefer recording the
  context in the manifest for staging and prod.
- **Order (direct mode):** project checks (chart, values, image reference, env file); the context;
  a read-only check that the Secret the deploy would produce holds every required key (exit 1
  before anything is built); a refusal (exit 2) while another helm operation holds the release,
  with the command that clears a lock left by an interrupted helm; then build, load or push, the
  namespace (created when missing), the Secret, and `helm upgrade --install --wait --timeout
  <--timeout, default 5m>`.
- **Failed rollout:** the pods' states, warning events and logs are printed, then with `--atomic`
  (default) the release is rolled back to the newest good revision, or a first install that
  never succeeded is uninstalled. Only the revision this run created is ever undone.
  `--no-atomic` leaves it in place.
- **Protected environments:** `deploy --env staging|prod` refuses while the manifest says
  `auth_policy_implemented: false` (the `custom` stub).

## Standard procedure

1. **Gate:** `graph-agents-cli eval run` exits 0 and the user approved deploying.
2. **Prerequisites:** `graph-agents-cli infra check --env <env>`. Read-only; reports the required
   tools and the kube context, Gateway API CRDs and classes / Ingress classes, cert-manager (only
   when `tls.certManager.enabled`), Argo CD (only when `cd: argocd`), metrics-server (only when
   `hpa.enabled`), the namespace, the image pull secret, the app Secret and its required keys,
   every `CHANGE-ME` placeholder (registry, chart image and env, CODEOWNERS, Argo CD `repoURL`),
   and, when `gh` is logged in, the environment protection, branch protection and (helm-push)
   `DEPLOY_KUBECONFIG` settings. Nothing is created; install hints are printed for the operator.
3. **Values:** fill `hostname`, `parentRef`, `tls`, `resources` in `values-<env>.yaml` and review
   `route.publicPaths` (what the Gateway or Ingress publishes). These are config files: never
   overwritten by upgrade, never containing secrets. `gateway.parentRef.name` is **required**
   whenever `gateway.enabled` (the staging/prod default): `deploy` checks the merged values
   before building or pushing anything and exits 3 naming the file when it is blank. Set
   `appUrl` (or a hostname) so the A2A card advertises a reachable URL. Keep `image:` a nested
   mapping (`image:` newline `tag: ...`), never an inline `{}` map, because `deploy` rewrites
   `image.tag` textually. Record `environments.<env>.context` in the manifest.
4. **Secrets:** `graph-agents-cli secrets apply --env <env>` (from `.env.<env>`). CD modes: the
   manifest's `secrets.owner` runs it from a workstation with cluster access; CI never holds
   application secrets; Argo never manages the Secret. `secrets status --env <env>` lists keys
   without values and exits 1 when a required key is missing. `deploy` refuses to touch Secrets
   in `helm-push` and `argocd` modes and prints the procedure.
5. **Deploy:** `graph-agents-cli deploy --env <env>` (direct), or let CI run it (`helm-push`), or
   open the PR (`argocd`).
6. **Verify:** `graph-agents-cli deploy --status --env <env>` (rollout status or `argocd app
   get`), then `graph-agents-cli run --url https://<host> "hello"` with the environment's
   credential (`--header 'Authorization: Bearer ...'` or `GRAPH_AGENTS_CLI_API_KEY`; a user's
   token under `jwt`; `--header` / `--cookie` under `custom`). Readiness is `/ready` (probed
   inside the cluster; not published on the route).

## Secrets and rotation (summary)

- Only variables in the manifest's `secrets.keys` are exported: the provider key
  (`OPENAI_API_KEY` | `ANTHROPIC_API_KEY` | `GOOGLE_API_KEY` | `MODEL_API_KEY`), `JUDGE_API_KEY`,
  `POSTGRES_DSN` (fastapi) or `DATABASE_URI` + `REDIS_URI` (langgraph-server), `API_KEY`,
  `LANGSMITH_API_KEY`, the `token_env` of every `auth: bearer` API in `api-policy.yaml`, and
  `AUTH_JWT_SECRET` automatically under `jwt` with HS*. Never the whole env file. Add other
  secrets (`METRICS_TOKEN`, `PRINCIPAL_HASH_SALT`) to the list.
- Server-side apply; allow-listed keys the env file leaves out are kept from the live Secret.
- `API_KEY`: the live key wins; it is replaced only when the env file sets another **and**
  `--rotate-api-key` is passed. When neither has one, a key is generated, applied and written to
  the env file (0600), never printed. Values must be single-line.
- **Rotation:** put the new value in `.env.<env>`, `secrets apply` (with `--rotate-api-key` for
  `API_KEY`), then `deploy --restart --env <env>` (`kubectl rollout restart`), because an
  externally managed Secret does not change the pod template. In argocd environments the restart
  is drift that self-heal may revert; the warning suggests an Argo resource action instead.
- Details: `references/secrets.md`.

## argocd PR flow and the single production gate (summary)

- Staging: the `staging` workflow (on `main`) builds and pushes `<registry>/<name>:<short sha>`
  (`${GITHUB_SHA::7}`), writes the tag into `values-staging.yaml` on `deploy/staging/<short sha>`
  (built on the latest `main`, superseding older staging PRs) and opens a PR with auto-merge;
  Argo syncs staging with self-heal. A PR opened with the workflow `GITHUB_TOKEN` never triggers
  `pr_checks`, so auto-merge on that required check needs a fine-grained PAT or GitHub App token
  (pull-request and contents write) in the `GH_PR_TOKEN` repository secret.
- Production: desired state changes **only** through a PR touching `values-prod.yaml`, opened by
  the `promote-to-prod` workflow (running in the GitHub `production` environment) or by a
  workstation `deploy --env prod --image <ref>`. Neither path merges. The merge requires review
  from the code owners of `values-prod.yaml`, cannot be self-approved, and requires `pr_checks`
  to pass. That merge is the single gate. Prod `Application` has no automated sync; an operator
  syncs in Argo after the merge.
- Rollback is a git revert (a PR under the same gate). No inbound access from GitHub to the
  cluster. Details: `references/gitops.md`.

## Required GitHub settings (summary)

Repository configuration a workflow cannot create for itself; the operator sets it, `infra
check` reports it, the scaffolded README documents it:

- Environment `production`: required reviewers (at least one), prevent self-review, deployment
  branches restricted to `main`, optional wait timer. Environment `staging`: deployment branches
  restricted to `main`.
- `helm-push`: the `DEPLOY_KUBECONFIG` secret in **each environment** (never a repository
  secret), and a self-hosted runner with `kubectl` and `curl`.
- Branch protection on `main` (argocd and helm-push): pull requests required; code-owner review
  required; dismiss stale approvals; prevent self-approval; `pr_checks` required; auto-merge
  allowed.
- `.github/CODEOWNERS` owns `deployment/` (except the dev and staging values), `.github/`,
  `api-policy.yaml`, `tests/eval/`, the extensions and the manifest; replace the
  `@CHANGE-ME/production-approvers` placeholder.
- Repository secret `GH_PR_TOKEN` so the desired-state PRs opened by CI trigger `pr_checks`.
- Optional: the provider key secret (or `MODEL_PROVIDER` / `MODEL_NAME` variables) so the
  `pr_checks` eval gate runs on a real model.
- GitHub Enterprise Server: set `GH_HOST=<host>` (plus `GH_ENTERPRISE_TOKEN` or `GITHUB_TOKEN`)
  where `deploy` runs so argocd-mode PRs go to that host.
- Details: `references/github-settings.md`.

## Local-load dev clusters

With `cd: skip` and a local cluster (kind, k3d, k3s on this machine, minikube, Docker Desktop,
Rancher Desktop, OrbStack), `deploy --env dev` skips the registry: it builds with `docker build`,
loads the image into the node (`kind load docker-image`, `k3d image import`, `docker save` then
`k3s ctr images import`, `minikube image load`; nothing for the shared-daemon clusters), applies
the Secret, runs `helm dependency build` when the subcharts are missing, and runs helm with
`values-dev.yaml` (bundled Postgres, and Redis under `langgraph-server`, gateway off,
`APP_ENV=dev` so `/playground` is served). The k3s import runs without `sudo` and needs root on
most k3s hosts. Reach it with `kubectl -n <name>-dev port-forward svc/<name> 8000:80`. Only the
`docker` CLI is supported for builds.

## Disconnected profile

The one configuration in which the whole lifecycle runs without internet access:

| Component | Requirement |
|---|---|
| Agent model | `MODEL_PROVIDER=openai-compatible`, `OPENAI_BASE_URL` at an in-cluster or on-network server (vLLM, TGI, Ollama) with a tool-capable model |
| Judge | same via `JUDGE_*`; may be the same endpoint |
| Runtime | `fastapi` (the LangGraph Server image checks for a licence at startup; the local `langgraph dev` server needs none) |
| Python deps | private index or mirror (`UV_INDEX_URL`), `install --locked` |
| Images | base images mirrored into `--registry` (`PYTHON_IMAGE` / `UV_IMAGE` build args); the chart's Bitnami subcharts vendored under `deployment/helm/<name>/charts/` and their images mirrored, otherwise `deploy` runs `helm dependency build` against `registry-1.docker.io` (exit 2 offline) |
| Tracing | `TRACING_ENABLED=true` with OTLP to an in-cluster collector, or off; no LangSmith |
| Evals | local datasets, deterministic checks, judge run in the project's environment against the on-network endpoint; `eval submit` disabled |
| CLI | installed from a mirror (`GRAPH_AGENTS_CLI_INSTALL_SPEC`); `GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1`; skills from the wheel bundle |
| Scaffold | built-in templates only |
| CD | only with an on-network GitHub Enterprise Server; otherwise `cd: skip` and direct-mode `deploy` |

`infra check --profile disconnected` and `login --profile disconnected` verify these and fail on
any hosted dependency. "Runs locally" (orchestration on the developer's machine) is not "runs
disconnected" (this profile).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `deploy` exit 3 "No env file for <env>" | create `.env.<env>` with the allow-listed keys, or pass `--env-file`; `.env` is used for `dev` only |
| `deploy` exit 1 "Refusing to deploy to <env> on the kubeconfig's current context" | record `environments.<env>.context` in the manifest, pass `--context <name>`, or `--yes` after checking the printed context |
| `deploy` exit 3 "Kube context 'x' ... is not in the kubeconfig" | fix the manifest's context or `--context`; the known contexts are listed |
| `deploy` exit 1 "Secret ... is missing required key(s)" | add them to `.env.<env>` and re-run (direct), or run `secrets apply` (helm-push); or drop the key from `secrets.keys` if the environment does not need it |
| `deploy` exit 2 "Another helm operation ... is in progress" | wait for it (`helm history`); if nothing else runs, the printed `helm rollback` / `helm uninstall` clears the lock an interrupted helm left |
| `deploy` exit 2 "helm upgrade failed ... rolled back to revision N" | read the printed pod diagnostics (states, events, logs); fix and deploy again |
| `deploy` exit 1 "Refusing to deploy <env> from outside CI in helm-push mode" | run from the CI runner, or `--force-direct` for a deliberate workstation deploy (dev is always allowed) |
| `deploy` exit 1 "--env-file is not accepted in argocd mode" (or helm-push) | Secrets are applied separately: `secrets apply --env <env>`; `deploy --env <env> --image <ref>` only opens the PR (argocd) or runs helm (helm-push) |
| `deploy` exit 1 "the manifest records auth_policy_implemented: false" | implement `app/policies/custom.py`, flip the manifest flag |
| `build` / `deploy` exit 3 "still the placeholder 'ghcr.io/CHANGE-ME'" | `graph-agents-cli scaffold enhance --registry <host>/<org>` (sets `create_params.registry` in the manifest, `image.repository` in the chart values and `IMAGE_REPOSITORY` in `.github/agent.env`); `build` also takes `--registry` |
| `deploy` exit 3 "gateway.parentRef.name is blank" | set `gateway.parentRef.name` in `values-<env>.yaml` (or `gateway.enabled: false` / `ingress.enabled: true`) |
| chart error "image.tag is empty" / "must be a quoted string" | deploy with a built image (`deploy` passes the tag); quote tags in values files (`tag: "0123456"`) |
| `deploy` exit 3 "is a digest reference" | pass `<registry>/<repo>:<tag>`; the chart has no `image.digest` |
| `deploy` exit 3 "not committed on origin/main" (argocd) | commit `values-<env>.yaml` on `main` first; the PR is built from the base branch's copy, never the working tree |
| `secrets apply` exit 3 "must be single-line" | put the value on one line (for example base64) or create the Secret with kubectl directly |
| "API_KEY in <file> differs from the live Secret; the live key is kept" | intended; pass `--rotate-api-key` to replace it, then `deploy --restart` |
| A2A client dials `127.0.0.1:8000` after fetching the card | `APP_URL` is unset in the pod: set `appUrl` or a gateway/ingress hostname in the values file |
| `deploy` exit 2 "missing in charts/ directory" or `helm dependency build` failed (429) | `registry-1.docker.io` is unreachable or rate-limited; retry, authenticate, or vendor the charts under `deployment/helm/<name>/charts/` |
| `deploy` exit 2 naming a tool | helm, kubectl, docker, git or gh is not on `PATH` |
| Staging PR opened by CI never runs `pr_checks` | PRs opened with `GITHUB_TOKEN` do not trigger workflows; store a PAT or App token as `GH_PR_TOKEN` |
| helm-push job: "DEPLOY_KUBECONFIG is empty" | store the kubeconfig as the `DEPLOY_KUBECONFIG` secret of that GitHub environment |
| Workflow step "Load project settings" fails on `.github/agent.env` | only the six known names are accepted; rename `CLI_VERSION_PIN` to `GRAPH_AGENTS_CLI_SPEC` |
| `k3s ctr images import` permission denied | the import needs root on that host |
| Pod `CreateContainerConfigError` | the Secret `<name>-app` is missing (required outside dev): `secrets apply`, then `secrets status --env <env>` |
| Pod not Ready, `/ready` 503 | the database is unreachable from the pod: check the DSN in the Secret and network policies |
| `ImagePullBackOff` | pull secret missing (`imagePullSecrets` in values; `infra check` reports it) or the image was not pushed / loaded |
| No route / 404 at the hostname | `gateway.parentRef` or `ingress.className` unset, or the path is not in `route.publicPaths`; `infra check` lists classes |
| TLS errors | `tls.existingSecret` name wrong, or cert-manager not installed while `tls.certManager.enabled` |
| 401 from `run --url` | wrong credential for the policy; `secrets status`; pass `--header` |
| 503 on every request under `custom` | the stub is still in place |
| Argo shows `OutOfSync` after `--restart` | self-heal reverted the restart annotation; use an Argo resource action |
| `ApiPolicyError` in tool results after deploy | the deployed `api-policy.yaml` differs from local (it is baked into the image; rebuild), or a base URL / token variable is missing in the pod |

## Not covered by this skill

- Writing the auth policy or tools: `/graph-agents-cli-langgraph-code`.
- Adding the Kubernetes target or CD mode to a project: `/graph-agents-cli-scaffold`.
- The eval gate that precedes deployment: `/graph-agents-cli-eval`.
- Tracing configuration after deploy: `/graph-agents-cli-observability`.
- Installing cluster components (Gateway controller, cert-manager, Argo CD, metrics-server),
  provisioning clusters or registries, or a managed-cloud deployment target: none exist here.
- External Secrets Operator, Sealed Secrets, Kustomize, Terraform, GitLab CI: not in this release.

## Migration note

Compared with google-agents-cli: Agent Runtime, Cloud Run, and GKE targets, Terraform
provisioning (`infra single-project`, `infra cicd`), Cloud Build, Secret Manager, and Agent
Gateway are gone. Any Kubernetes cluster via a Helm chart replaces them; secrets are Kubernetes
Secrets applied from allow-listed `.env` keys; `infra check` is read-only.
