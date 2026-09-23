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
  version: "0.1.0"
  requires:
    bins:
      - graph-agents-cli
    install: "uv tool install git+https://github.com/ss7172/graph-agents-cli"
---

# Deployment guide

> **Requires:** `graph-agents-cli`, plus `docker`, `helm`, `kubectl`, a kubeconfig, and `argocd`
> when `cd: argocd`. The CLI shells out to these; it never installs cluster components. If the
> project has no deployment target, `/graph-agents-cli-scaffold` adds one with `scaffold enhance`.

> **Never deploy without explicit human approval.** In `argocd` mode you open a PR; a code owner
> merges it. You never merge it.

## Reference files

| File | Contents |
|---|---|
| `references/kubernetes.md` | Helm chart values, environments, traffic entry (Gateway API / Ingress / TLS), Postgres and Redis toggles, HPA/PDB, local-load dev clusters, disconnected profile |
| `references/gitops.md` | The argocd mode: `Application` manifests, the `deploy` PR flow, staging auto-merge, production promotion, rollback by revert, `--restart` under self-heal |
| `references/secrets.md` | The Secret contract, allow-listed keys, `secrets apply/status`, ownership in CD modes, rotation |
| `references/github-settings.md` | Required GitHub environment and branch-protection settings, `CODEOWNERS`, what `infra check` reports |

---

## Deployment modes (determined by the manifest's `cd` and the environment)

| Mode | Who applies the release | What `deploy` does |
|---|---|---|
| *direct, local-load* (`cd: skip`, dev cluster) | `deploy` | builds the image with the `docker` CLI, loads it into the kind/k3s/k3d/minikube/Docker Desktop node, applies the Secret from the allow-listed keys of `--env-file`, runs `helm upgrade --install` |
| *direct, registry* (`cd: skip`, any cluster) | `deploy` | builds, pushes to the manifest's registry, applies the Secret, runs `helm upgrade --install` |
| *helm-push* (`cd: helm-push`) | GitHub Actions on a self-hosted runner | CI builds and pushes; the runner job runs `deploy --image <ref> --env <env>`, which only runs helm. `deploy` from a workstation is allowed for `dev` and refused for `staging`/`prod` unless `--force-direct` |
| *argocd* (`cd: argocd`) | Argo CD inside the cluster | **`deploy` never runs helm and never merges.** `deploy --env <env> --image <ref>` writes the desired state: it updates the image tag in `deployment/helm/<name>/values-<env>.yaml` (and nothing else), commits on a branch `deploy/<env>/<short sha>` built with git plumbing from `origin/main` (the developer's checkout is never switched), and opens or updates a pull request with `gh` (else GitHub REST with `GITHUB_TOKEN`; `GH_HOST=<host>` for a GitHub Enterprise Server). `--image` names the CI-pushed image; without it the tag is `--tag` or the short git sha with a warning. Argo reconciles from `main`, so nothing reaches the cluster until the PR merges, and a PR touching `values-prod.yaml` merges only after the production approval, whoever opened it. In argocd environments only `--status`, `--restart`, and `secrets` touch the cluster |

The dev-cluster detection reads the kube context name (`kind-*`, `k3d-*`, `k3s`, `minikube`,
`docker-desktop`, `rancher-desktop`, `orbstack`). `deploy --dry-run` prints the docker, helm,
kubectl, and gh commands plus the rendered manifests without running anything, with one
exception: `helm dependency build deployment/helm/<name>` is printed with the `[dry-run]` prefix
and still executed when a declared subchart is missing from `charts/` (or `Chart.lock` is
absent), because it only writes into the chart directory and the `helm template` render needs
the subcharts. Use `--dry-run` to show the user what will happen.

Images are tagged with the **short commit SHA**: `${GITHUB_SHA::7}` in the `staging` workflow,
`git rev-parse --short HEAD` for a workstation `deploy` (else a UTC timestamp with a warning);
`--tag TAG` overrides the local build tag. The argocd branch is `deploy/<env>/<short sha>` and
the `promote-to-prod` `image_tag` input is a short sha.

Exit codes: `0` ok, `1` refused (policy or mode), `2` tool failure (helm/kubectl/docker
non-zero, or helm/kubectl/docker/git/gh missing from `PATH`, or `helm dependency build` failing
because `registry-1.docker.io` is unreachable), `3` configuration error.

## Environments

`dev`, `staging`, `prod`. Each has a values file `deployment/helm/<name>/values-<env>.yaml`, a
namespace `<name>-<env>` (recorded with its kube context under `environments:` in the manifest),
a Secret `<name>-app`, and under argocd an `Application`. `dev` may be a local-load cluster.
`deploy --env <env>` and `secrets apply --env <env>` read `--env-file`, defaulting to
`.env.<env>` when present, else `.env`.

| Values file | Defaults |
|---|---|
| `values-dev.yaml` | `env.APP_ENV=dev`, `postgresql.enabled=true` (bundled subchart), `gateway.enabled=false` |
| `values-staging.yaml`, `values-prod.yaml` | `postgresql.enabled=false` (external DSN from the Secret), gateway on, hostnames blank for the operator to fill |

## Standard procedure

1. **Gate:** `graph-agents-cli eval run` exits 0 and the user approved deploying.
2. **Prerequisites:** `graph-agents-cli infra check --env <env>`. Read-only; reports the
   required tools and the kube context, Gateway API CRDs and classes / Ingress classes,
   cert-manager (only when `tls.certManager.enabled`), Argo CD (only when `cd: argocd`),
   metrics-server (only when `hpa.enabled`), the namespace, the image pull secret, the app
   Secret, and, when `gh` is logged in, the environment protection and branch protection
   settings. Nothing is created; install hints are printed for the operator.
3. **Values:** fill `hostname`, `className` / `parentRef`, `tls`, `resources` in
   `values-<env>.yaml`. These are config files: never overwritten by upgrade, never containing
   secrets. `gateway.parentRef.name` is **required** by the HTTPRoute whenever `gateway.enabled`
   (the staging/prod default): set it in the values file (`deploy` checks the merged values
   before building or pushing anything and exits 3 naming the file when it is blank; a manual
   `helm template` may pass `--set gateway.parentRef.name=<gateway>`). Set `appUrl` (or a
   hostname) so the A2A card advertises a reachable URL. Keep `image:`
   a nested mapping (`image:` newline `tag: ...`), never an inline `{}` map, because `deploy`
   rewrites `image.tag` textually. Run `helm dependency build deployment/helm/<name>` once (or
   let `deploy` do it) so the bitnami `postgresql`/`redis` subcharts are under `charts/`.
4. **Secrets:** direct modes: `graph-agents-cli secrets apply --env <env> --env-file .env.<env>`.
   CD modes: the manifest's `secrets.owner` runs it from a workstation with cluster access; CI
   never holds application secrets; Argo never manages the Secret. `secrets status --env <env>`
   lists present keys without values (exit 1 when the Secret or a key is missing). `deploy`
   refuses to touch Secrets in `helm-push` and `argocd` modes and prints the procedure. The CLI
   does not refuse `secrets apply` under CI; keeping application secrets out of CI is the
   procedure, not a runtime check.
5. **Deploy:** `graph-agents-cli deploy --env <env>` (direct), or let CI run it (`helm-push`), or
   open the PR (`argocd`). `deploy --env staging|prod` refuses while the manifest says
   `auth_policy_implemented: false`.
6. **Verify:** `graph-agents-cli deploy --status --env <env>` (rollout status or `argocd app
   get`), then `graph-agents-cli run --url https://<host> --mode chat "hello"` with the
   environment's credential (`--header 'Authorization: Bearer ...'` or `GRAPH_AGENTS_CLI_API_KEY`;
   a user's token under `jwt`; `--header` / `--cookie` under `custom`). `GET /health` reports runtime and
   checkpointer.

## Secrets and rotation (summary)

- Only variables in the manifest's `secrets.keys` are exported: the provider key
  (`OPENAI_API_KEY` | `ANTHROPIC_API_KEY` | `GOOGLE_API_KEY` | `MODEL_API_KEY`), `JUDGE_API_KEY`,
  `POSTGRES_DSN` (fastapi) or `DATABASE_URI` + `REDIS_URI` (langgraph-server), `API_KEY`,
  `LANGSMITH_API_KEY`, and the `token_env` of every `auth: bearer` API in `api-policy.yaml`.
  Never the whole `.env`.
- `secrets apply` generates `API_KEY` (32 random bytes, hex) only when it is absent from the env
  file and from the live Secret, and prints it once; an existing key is kept, so one key per
  environment survives repeated deploys. Values must be single-line.
- **Rotation:** re-run `secrets apply` with the new value, then `deploy --restart --env <env>`
  (`kubectl rollout restart`), because an externally managed Secret does not change the pod
  template. In argocd environments the restart is drift that self-heal may revert; the warning
  suggests an Argo resource action instead.
- Details: `references/secrets.md`.

## argocd PR flow and the single production gate (summary)

- Staging: the `staging` workflow (on `main`) builds and pushes `<registry>/<name>:<short sha>`
  (`${GITHUB_SHA::7}`), then runs `uvx --from "$GRAPH_AGENTS_CLI_SPEC" graph-agents-cli deploy --env staging --image ...`, which
  opens a PR from `deploy/staging/<short sha>` updating `values-staging.yaml` with auto-merge
  enabled; Argo syncs staging with self-heal. A PR opened with the workflow `GITHUB_TOKEN` never
  triggers `pr_checks`, so auto-merge on that required check needs a fine-grained PAT or GitHub
  App token in the `GH_PR_TOKEN` repository secret (the workflows use
  `secrets.GH_PR_TOKEN || secrets.GITHUB_TOKEN`).
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
  branches restricted to `main`, optional wait timer.
- Environment `staging`: deployment branches restricted to `main`; no reviewers.
- Branch protection on `main` (argocd and helm-push): pull requests required; code-owner review
  required; dismiss stale approvals; prevent self-approval; `pr_checks` required; no bypass for
  the Actions token except the staging auto-merge mechanism.
- `.github/CODEOWNERS` maps `deployment/helm/<name>/values-prod.yaml` and
  `deployment/argocd/application-prod.yaml` to a production-approvers team placeholder the
  operator fills in.
- Repository secret `GH_PR_TOKEN` (fine-grained PAT or GitHub App token) so the desired-state
  PRs opened by CI trigger `pr_checks`; `GITHUB_TOKEN`-opened PRs never do.
- GitHub Enterprise Server: set `GH_HOST=<host>` (plus `GH_ENTERPRISE_TOKEN` or `GITHUB_TOKEN`)
  where `deploy` runs so argocd-mode PRs go to that host; `infra check --profile disconnected`
  treats GitHub-hosted CI as outside the profile unless `GH_HOST` / `GITHUB_SERVER_URL` names an
  on-network GHES.
- Details: `references/github-settings.md`.

## Local-load dev clusters

With `cd: skip` and a kube context named `kind-*`, `k3d-*`, `k3s`, `minikube`, or
`docker-desktop`, `deploy --env dev` skips the registry: it builds with `docker build`, loads the
image into the node (`kind load docker-image`, `k3d image import`, `docker save` then
`k3s ctr images import`, `minikube image load`; nothing for docker-desktop, rancher-desktop,
orbstack), applies the Secret, runs `helm dependency build` when the subcharts are missing, and
runs helm with `values-dev.yaml` (bundled Postgres, and Redis under `langgraph-server`, gateway
off, `APP_ENV=dev` so `/playground` is served). The k3s import runs without `sudo` and needs root
on most k3s hosts, so run `deploy` as a user allowed to invoke `k3s ctr`. Reach it with
`kubectl -n <name>-dev port-forward svc/<name> 8000:80`. Only the `docker` CLI is supported for
builds.

## Disconnected profile

The one configuration in which the whole lifecycle runs without internet access:

| Component | Requirement |
|---|---|
| Agent model | `MODEL_PROVIDER=openai-compatible`, `OPENAI_BASE_URL` at an in-cluster or on-network server (vLLM, TGI, Ollama) with a tool-capable model |
| Judge | same via `JUDGE_*`; may be the same endpoint |
| Runtime | `fastapi` (LangGraph Server is excluded: the deployed `langgraph-api` image's licensing is unverified; the local `langgraph dev` server was verified to start with no LangSmith key) |
| Python deps | private index or mirror (`UV_INDEX_URL`), `install --locked` |
| Images | base images mirrored into `--registry`; `deploy` and `build` never pull from Docker Hub; the chart's bitnami `postgresql`/`redis` subcharts vendored under `deployment/helm/<name>/charts/`, otherwise `deploy` runs `helm dependency build` against `registry-1.docker.io` (exit 2 offline) |
| Tracing | `TRACING_ENABLED=true` with OTLP to an in-cluster collector, or off; no LangSmith |
| Evals | local datasets, deterministic checks, judge run in the project's environment against the on-network endpoint; `eval submit` disabled |
| CLI | `GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1`; skills from the wheel bundle |
| Scaffold | built-in templates only |
| CD | only with an on-network GitHub Enterprise Server; otherwise `cd: skip` and direct-mode `deploy` |

`infra check --profile disconnected` and `login --profile disconnected` verify these and fail on
any hosted dependency. "Runs locally" (orchestration on the developer's machine) is not "runs
disconnected" (this profile).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `deploy` exit 1 "refused: helm-push mode" | run from the CI runner, or `--force-direct` for a deliberate workstation deploy to staging/prod (dev is always allowed) |
| `deploy` exit 1 "refused: argocd mode touches the cluster only via --status/--restart" | expected; `deploy --env <env> --image <ref>` opens a PR instead |
| `deploy` exit 1 "auth_policy_implemented is false" | implement `app/policies/custom.py`, flip the manifest flag |
| `deploy` exit 3 "gateway.parentRef.name is blank" | set `gateway.parentRef.name` in `values-<env>.yaml` (or `gateway.enabled: false` / `ingress.enabled: true`); checked before any tool runs |
| `deploy` exit 3 "is a digest reference" | pass `<registry>/<repo>:<tag>`; the chart has no `image.digest` |
| `deploy` exit 3 "not committed on origin/main" (argocd) | commit `values-<env>.yaml` on `main` first; the PR is built from the base branch's copy, never the working tree |
| `secrets apply` exit 3 "must be single-line" | put the value on one line (for example base64) or create the Secret with kubectl directly |
| A2A client dials `127.0.0.1:8000` after fetching the card | `APP_URL` is unset in the pod: set `appUrl` or a gateway/ingress hostname in the values file |
| `deploy` exit 2 from helm | run the printed `helm upgrade --install ... --debug` yourself; `helm template` to inspect |
| `deploy` exit 2 "missing in charts/ directory: postgresql, redis" or `helm dependency build` failed | the bitnami subcharts could not be fetched from `registry-1.docker.io`; give the machine access or vendor the charts under `deployment/helm/<name>/charts/` |
| `deploy` exit 2 naming a tool | helm, kubectl, docker, git or gh is not on `PATH` |
| `helm template` fails: `gateway.parentRef.name is required` | set `gateway.parentRef.name` in `values-<env>.yaml` or pass `--set gateway.parentRef.name=<gateway>` |
| Staging PR opened by CI never runs `pr_checks` | PRs opened with `GITHUB_TOKEN` do not trigger workflows; store a PAT or App token as `GH_PR_TOKEN` |
| `k3s ctr images import` permission denied | the import needs root on that host |
| Pod `CreateContainerConfigError` | the Secret `<name>-app` is missing a key: `secrets status --env <env>` |
| `ImagePullBackOff` | pull secret missing (`imagePullSecrets` in values; `infra check` reports it) or the image was not pushed / loaded |
| No route / 404 at the hostname | `gateway.className` / `parentRef` or `ingress.className` unset; `infra check` lists classes |
| TLS errors | `tls.existingSecret` name wrong, or cert-manager not installed while `tls.certManager.enabled` |
| 401 from `run --url` | `API_KEY` mismatch; `secrets status`; pass `--header` |
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
