# Required GitHub settings

These are repository settings a workflow cannot create for itself with the default token. The
operator sets them once; `graph-agents-cli infra check` reports whether they exist when a
`GITHUB_TOKEN` (or `gh auth`) is available; the scaffolded README documents them. The CLI never
creates or changes them.

## Environments

| Environment | Settings |
|---|---|
| `production` | required reviewers (at least one); "prevent self-review" enabled; deployment branches restricted to `main`; optional wait timer |
| `staging` | deployment branches restricted to `main`; no reviewers |

The production job (`promote-to-prod` in argocd mode, the `production` job in helm-push mode)
declares `environment: production` and must not be reachable from `workflow_dispatch` on other
branches.

## Branch protection on `main` (argocd and helm-push modes)

- Pull requests required.
- Required review from code owners.
- "Dismiss stale approvals" enabled.
- "Prevent self-approval" enabled.
- `pr_checks` as a required status check.
- No bypass for the Actions token, except the staging auto-merge mechanism (auto-merge allowed
  on the repository; the staging PR merges once `pr_checks` passes).

## `CODEOWNERS`

Scaffolded at `.github/CODEOWNERS`:

```
deployment/helm/<name>/values-prod.yaml      @<org>/production-approvers
deployment/argocd/application-prod.yaml      @<org>/production-approvers
```

Replace the placeholder team. With code-owner review required, a PR touching `values-prod.yaml`
cannot merge without a production approver, whether it was opened by `promote-to-prod` or by a
workstation `deploy --env prod`.

## Repository secrets

| Secret | Mode | Purpose |
|---|---|---|
| (none for images on GHCR) | argocd, helm-push | `GITHUB_TOKEN` pushes to `ghcr.io/<org>` |
| registry credentials | any mode with a non-GHCR registry | `docker login` in `staging.yaml` |
| `KUBECONFIG` | helm-push | the self-hosted runner's cluster access |
| `GH_PR_TOKEN` | argocd, helm-push | a fine-grained PAT or GitHub App token used by `staging.yaml` and `promote-to-prod.yaml` to open the desired-state PRs (`GH_TOKEN: ${{ secrets.GH_PR_TOKEN \|\| secrets.GITHUB_TOKEN }}`). A pull request opened with the workflow `GITHUB_TOKEN` does not trigger `pr_checks` (GitHub never starts workflows from GITHUB_TOKEN events), so auto-merge on a required check needs this secret |
| provider key (`OPENAI_API_KEY`, ...) | any | `pr_checks` eval gate; the gate runs with `MODEL_PROVIDER=${{ vars.MODEL_PROVIDER \|\| 'fake' }}`, so without the repository variable it uses the deterministic fake model and needs no key |

**Never** application secrets (`API_KEY`, `POSTGRES_DSN`, provider keys for the deployed app);
those are Kubernetes Secrets applied by the owner (`secrets.md`).

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
stale approvals and lists `pr_checks` as a required status check. Without `gh` or a token it
reports the checks as skipped with the checklist. It never changes anything.

```bash
GITHUB_TOKEN=... graph-agents-cli infra check --env prod --json
```

## Verifying by hand

```bash
gh api repos/<owner>/<repo>/environments
gh api repos/<owner>/<repo>/environments/production
gh api repos/<owner>/<repo>/branches/main/protection
gh api repos/<owner>/<repo> --jq .allow_auto_merge
```
