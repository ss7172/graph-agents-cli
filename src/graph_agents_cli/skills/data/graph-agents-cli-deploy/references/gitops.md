# GitOps: the `argocd` CD mode

With `cd: argocd`, Argo CD inside the cluster reconciles from `main`. `graph-agents-cli deploy`
never runs helm and never merges; it writes desired state and opens a pull request.

## Scaffolded artefacts

- `deployment/argocd/application-<env>.yaml`: one Argo `Application` per environment;
  `targetRevision: main`, `path: deployment/helm/<name>`,
  `valueFiles: [values.yaml, values-<env>.yaml]`, destination namespace `<name>-<env>`.
  `dev` and `staging` have automated sync with self-heal; **prod has no automated sync** (an
  operator syncs in Argo after the merge).
- `.github/workflows/pr_checks.yaml`: ruff, unit, integration, the eval gate when a dataset is
  present, the API-policy check.
- `.github/workflows/staging.yaml` (on `main`): builds and pushes
  `<registry>/<name>:<short sha>` (`${GITHUB_SHA::7}`, exposed as the `tag` job output), then
  opens a PR from branch `deploy/staging/<short sha>` updating `image.tag` in
  `values-staging.yaml` with auto-merge enabled (`gh pr merge --auto --squash`); branch
  protection must permit auto-merge. The bot-owned branch is pushed with `git push --force` and
  the PR is created only when none is open, so a re-run for the same commit replaces the branch
  and keeps the PR (a shallow checkout has no `origin/<branch>` ref, which made a
  `--force-with-lease` re-run fail). Inputs and job outputs reach the scripts through `env:` only.
- `.github/workflows/promote-to-prod.yaml`: runs in the GitHub `production` environment (which
  controls who may *initiate* a promotion from CI); its `image_tag` input is the short sha. It
  installs uv, sets a git identity and runs
  `uvx --from "$GRAPH_AGENTS_CLI_SPEC" graph-agents-cli deploy --env prod --image <registry>/<name>:<short sha>`, which opens a
  PR updating `values-prod.yaml`. It does not merge.
- Both workflows set `GH_TOKEN: ${{ secrets.GH_PR_TOKEN || secrets.GITHUB_TOKEN }}`. A pull
  request opened with the workflow `GITHUB_TOKEN` does not trigger `pr_checks` (GitHub never
  starts workflows from `GITHUB_TOKEN` events), so auto-merge on a required check needs a
  fine-grained PAT or GitHub App token stored as the `GH_PR_TOKEN` repository secret.
- `.github/agent.env` (rendered for every project, read only by the workflows):
  `GRAPH_AGENTS_CLI_SPEC` (where CI installs the CLI: the creating version's git tag) and, for
  kubernetes projects, `IMAGE_REPOSITORY`, `RELEASE_NAME`, `CHART_PATH`, `RUNTIME`, `CD`.
  `pr_checks` loads it with comments and blank lines stripped (the `GITHUB_ENV` file format).
- `.github/CODEOWNERS`: `deployment/helm/<name>/values-prod.yaml` and
  `deployment/argocd/application-prod.yaml` -> `@<org>/production-approvers` (placeholder to fill).

The `Application` manifests are config: `upgrade` never overwrites them.

## The `deploy` PR flow

```bash
graph-agents-cli deploy --env staging --image ghcr.io/acme/my-agent:abc123
graph-agents-cli deploy --env prod --image ghcr.io/acme/my-agent:abc123
```

1. Reads `values-<env>.yaml` **as `origin/main` holds it** (`git cat-file blob origin/main:<path>`;
   `HEAD` when `origin/main` is unavailable), changes `image.tag` and nothing else. Your working
   tree is not modified and never used: a checkout behind `main` or with local edits cannot leak
   into the PR, and "nothing to change" is judged on the base branch (a closed, unmerged PR is
   re-opened by a re-run). A values file not committed on the base is exit 3. Paths are
   repository-relative, so a project below the git root works.
2. Builds branch `deploy/<env>/<short sha>` with git plumbing (`hash-object --stdin`,
   `read-tree`, `update-index`, `write-tree`, `commit-tree`, `update-ref`) and pushes it with
   `git push --force-with-lease -u origin`. Your checkout and index are never switched (no
   `git checkout -b`). A git identity must be configured (`commit-tree` needs one; the scaffolded
   workflow sets it).
3. Opens or updates a PR against `main` with `gh` when available, else GitHub REST with
   `GITHUB_TOKEN`, `GH_TOKEN` or `GH_ENTERPRISE_TOKEN`. Only GitHub and GitHub Enterprise Server
   hosts are supported; a GHES host is recognised from `GH_HOST` (or `GITHUB_HOST`,
   `GITHUB_SERVER_URL`), API base `https://<host>/api/v3`. Without `gh` and without a token the
   branch is still pushed and the command exits 3 with instructions.
4. Prints the PR URL and stops. **You never merge it.** Argo reconciles after the merge.

`--dry-run` prints the values rewrite, the git plumbing and the `gh pr list/create` commands.
`--image` names the CI-pushed image (a tagged reference; a digest reference is refused with exit
3 because the chart has no `image.digest`); without it `deploy` uses `--tag` or the short git sha and
warns that the image must already have been pushed by CI (the CLI does not build or push in this
mode). The tag written must match what CI pushed: both are the short sha.

## The single production gate

Production desired state changes only through a PR touching `values-prod.yaml`. Two paths open
such a PR (`promote-to-prod` from CI, `deploy --env prod` from a workstation); neither merges.
The merge requires:

- review from the production approvers in `CODEOWNERS` for `values-prod.yaml`,
- no self-approval (branch protection "prevent self-approval" and environment "prevent
  self-review"),
- `pr_checks` green.

Argo watches `main`, so approval necessarily precedes the production change on both paths. No
inbound access from GitHub to the cluster exists. No Argo Image Updater is used.

## Operating in argocd environments

| Need | Command |
|---|---|
| Status | `graph-agents-cli deploy --status --env <env>` (`argocd app get <name>-<env>`) |
| Restart after secret rotation | `graph-agents-cli deploy --restart --env <env>` (`kubectl rollout restart`); the CLI warns that self-heal may revert the annotation and recommends an Argo resource action (`argocd app actions run <name>-<env> restart --kind Deployment`) |
| Secrets | `graph-agents-cli secrets apply/status --env <env>` from the owner's workstation; Argo never manages the Secret |
| Rollback | revert the values change on `main` through a PR (same gate); for emergencies `argocd app history` / `argocd app rollback` on the Argo side, then reconcile git |
| Diff before merge | `argocd app diff <name>-<env> --revision <branch>` |

Direct `helm upgrade` or `kubectl apply` against an argocd environment is drift: self-heal reverts
it (dev, staging) or Argo reports `OutOfSync` (prod). Do not do it.

## `helm-push` for comparison

A self-hosted GitHub runner inside the network holds a kubeconfig as a repository secret and runs
`graph-agents-cli deploy --image <ref> --env <env>` (helm only). The `production` job declares
`environment: production` and is gated by that environment's reviewers. Workstation deploys are
allowed for `dev`, refused for `staging`/`prod` unless `--force-direct`. Secrets are still
provisioned by the owner from a workstation, never by CI.
