# Required GitHub settings

These are repository settings a workflow cannot create for itself with the default token. The
operator sets them once; `graph-agents-cli infra check` reports whether they exist when a
`GITHUB_TOKEN` (or `gh auth`) is available; the scaffolded README documents them. The CLI never
creates or changes them.

## Environments

| Environment | Settings |
|---|---|
| `production` | required reviewers (at least one); "prevent self-review" enabled; deployment branches restricted to `main`; optional wait timer; in `helm-push` mode the `DEPLOY_KUBECONFIG` secret |
| `staging` | deployment branches restricted to `main`; no reviewers; in `helm-push` mode the `DEPLOY_KUBECONFIG` secret |

The production jobs (`promote-to-prod` in both modes) declare `environment: production` and run
only from `main`.

## Branch protection on `main` (argocd and helm-push modes)

- Pull requests required.
- Required review from code owners.
- "Dismiss stale approvals" enabled.
- "Prevent self-approval" enabled.
- `pr_checks` as a required status check.
- No bypass for the Actions token, except the staging auto-merge mechanism (auto-merge allowed
  on the repository; the staging PR merges once `pr_checks` passes).

## `CODEOWNERS`

Scaffolded at `.github/CODEOWNERS` for every project:

```
/deployment/ @CHANGE-ME/production-approvers          # kubernetes target only
/deployment/helm/*/values-dev.yaml                    # unowned, so the staging PR can auto-merge
/deployment/helm/*/values-staging.yaml
/.github/ @CHANGE-ME/production-approvers             # workflows, agent.env, this file
/api-policy.yaml @CHANGE-ME/production-approvers
/tests/eval/ @CHANGE-ME/production-approvers
/graph-agents-cli-extensions.yaml @CHANGE-ME/production-approvers
/extensions/ @CHANGE-ME/production-approvers
/.graph-agents-cli/ @CHANGE-ME/production-approvers
/graph-agents-cli-manifest.yaml @CHANGE-ME/production-approvers
```

Replace the placeholder team (`infra check` reports it: a required item under `helm-push` or
`argocd`, a warning under `skip`; GitHub ignores unknown owners, so the gate would require
nobody). With code-owner review required, a PR touching `values-prod.yaml`, the policy, the eval
gate inputs, the workflows or the extensions cannot merge without a production approver, whoever
opened it.

## Secrets and variables

| Name | Kind | Mode | Purpose |
|---|---|---|---|
| (none for images on GHCR) | | argocd, helm-push | `GITHUB_TOKEN` pushes to `ghcr.io/<org>` |
| `REGISTRY_USERNAME`, `REGISTRY_PASSWORD` | repository secrets | any CD mode with a non-GHCR registry | `docker login` in `staging.yaml` |
| `DEPLOY_KUBECONFIG` | **environment** secret of `staging` and of `production` | helm-push | the self-hosted runner's cluster credentials, written under `RUNNER_TEMP` and removed after the job. Never a repository secret: any branch's workflow could read that one and it would bypass the production reviewers. The retired repository secret `KUBECONFIG` is no longer read; delete it |
| `GH_PR_TOKEN` | repository secret | argocd, helm-push | a fine-grained PAT or GitHub App token with pull-request and contents write access, used to push the desired-state branches and open the PRs (`secrets.GH_PR_TOKEN \|\| secrets.GITHUB_TOKEN`). A PR opened with the workflow `GITHUB_TOKEN` does not trigger `pr_checks`, so auto-merge on a required check needs it |
| provider key (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY` or `MODEL_API_KEY`) | repository secret | any | the `pr_checks` eval gate runs on the project's real provider when its key exists; tests always run on the fake model |
| `MODEL_PROVIDER`, `MODEL_NAME` | repository variables | any | override the eval gate's model (`MODEL_NAME` is required when `MODEL_PROVIDER` differs from the project's provider) |
| `JUDGE_MODEL_PROVIDER`, `JUDGE_MODEL_NAME`, `JUDGE_BASE_URL`; `JUDGE_API_KEY` | variables; secret | any | a separate eval judge |

Without a provider key the gate runs on the deterministic fake model and prints the warning
"Eval gate is not a quality signal". **Never** application secrets (`API_KEY`, `POSTGRES_DSN`,
provider keys for the deployed app) in GitHub: those are Kubernetes Secrets applied by the owner
(`secrets.md`).

## Self-hosted runner (helm-push)

A runner with network access to the cluster (the jobs use `runs-on: self-hosted`), with `kubectl`
and `curl` on `PATH` (the jobs resolve the kube context and verify the rollout with them; `uv` is
installed by the job). Its kubeconfig (`DEPLOY_KUBECONFIG`) needs rights to deploy the release
and to read the app Secret (deploy checks its required keys).

## GitHub Enterprise Server

Set `GH_HOST=<host>` (or `GITHUB_HOST`; `GITHUB_SERVER_URL` is honoured inside Actions) with
`GH_ENTERPRISE_TOKEN` or `GITHUB_TOKEN` where `deploy` runs, so argocd-mode PRs go to that host
(API base `https://<host>/api/v3`). `infra check --profile disconnected` and
`login --profile disconnected` treat GitHub-hosted CI as outside the disconnected profile unless
`GH_HOST` / `GITHUB_SERVER_URL` names an on-network GHES.

## What `infra check` reports

Through the `gh` CLI (`gh api`, so `gh auth login` or `GITHUB_TOKEN`), skipped when `cd: skip`:
whether the `production` environment exists with required reviewers, prevent self-review and
deployment branches restricted; whether the `staging` environment exists with restricted
branches; whether `main` branch protection requires pull requests, code-owner review, dismisses
stale approvals and lists `pr_checks` as a required status check. For `helm-push` it also
reports whether `DEPLOY_KUBECONFIG` exists as a secret of each environment and warns about a
repository-level (or shared organization) `KUBECONFIG` / `DEPLOY_KUBECONFIG` secret; these rows
are informational when the account cannot read secrets. When the GitHub API does not answer, the
GitHub rows collapse into one informational row. Independently of GitHub it reports every
`CHANGE-ME` placeholder (registry, chart `image.repository` and `env` (required outside dev,
a warning in dev, as `deploy` treats it), CODEOWNERS, Argo CD `repoURL`), and with `--env` the
`jwt` policy's verification settings (a JWKS URL or public key; issuer and audience outside
dev), the ServiceMonitor's token Secret and whether an external DSN requires TLS. Rows for
components the environment does not use (a disabled Gateway, cert-manager, metrics-server,
Argo CD) are `skip` with no install hint. It never changes anything.

```bash
GITHUB_TOKEN=... graph-agents-cli infra check --env prod --json
```

## Verifying by hand

```bash
gh api repos/<owner>/<repo>/environments
gh api repos/<owner>/<repo>/environments/production
gh api repos/<owner>/<repo>/environments/production/secrets
gh api repos/<owner>/<repo>/branches/main/protection
gh api repos/<owner>/<repo> --jq .allow_auto_merge
```
