# GitOps: the `argocd` CD mode

With `cd: argocd`, Argo CD inside the cluster reconciles from `main`. `graph-agents-cli deploy`
never runs helm, never contacts the cluster and never merges; it writes desired state and opens
a pull request.

## Scaffolded artefacts

- `deployment/argocd/application-<env>.yaml`: one Argo `Application` per environment;
  `targetRevision: main`, `path: deployment/helm/<name>`,
  `valueFiles: [values.yaml, values-<env>.yaml]`, destination namespace `<name>-<env>`
  (`CreateNamespace`). `dev` and `staging` have automated sync with self-heal; **prod has no
  automated sync** (an operator syncs in Argo after the merge). They ignore the data of the
  chart-managed `<name>-postgresql-auth` Secret (`RespectIgnoreDifferences`), so the dev database
  password is not regenerated on every sync. Set `repoURL` (a `CHANGE-ME` placeholder that
  `infra check` reports). An environment shows a comparison error until its values file has an
  image tag: the chart refuses an empty one.
- `.github/workflows/pr_checks.yaml`: ruff, unit and integration tests on the fake model, `lint`
  (with the API-policy check) and the eval gate. The gate uses the project's real provider when
  its key is a repository secret (or the `MODEL_PROVIDER` / `MODEL_NAME` repository variables);
  on the fake model it warns that the gate only checks the plumbing.
- `.github/workflows/staging.yaml` (on `main`, or by hand from `main`): builds and pushes
  `<registry>/<name>:<short sha>` (`${GITHUB_SHA::7}`), then writes the tag (double-quoted) into
  `values-staging.yaml` on branch `deploy/staging/<short sha>`, built on the latest `main`, and
  opens a PR with auto-merge (`gh pr merge --auto --squash`); branch protection must permit
  auto-merge. It closes older open `deploy/staging/*` PRs it supersedes (with a comment, deleting
  their branches) and does nothing when `main` or an open PR already carries this build or a
  newer one, so a re-run of an older build never moves staging back. PRs whose tag is not a
  commit (a workstation `<sha>-dirty-<time>`) are left alone. Pushes that only change
  `values-*.yaml` do not start the workflow, so a merged staging PR never triggers another build.
- `.github/workflows/promote-to-prod.yaml`: runs in the GitHub `production` environment (which
  controls who may *initiate* a promotion from CI), only from `main`; its `image_tag` input is the
  short sha (empty = the tag in `values-staging.yaml`). It runs
  `uvx --from "$GRAPH_AGENTS_CLI_SPEC" graph-agents-cli deploy --env prod --image <registry>/<name>:<short sha> --yes`,
  which opens a PR updating `values-prod.yaml`. It does not merge.
- Both workflows use `secrets.GH_PR_TOKEN || secrets.GITHUB_TOKEN`. A pull request opened with
  the workflow `GITHUB_TOKEN` does not trigger `pr_checks` (GitHub never starts workflows from
  `GITHUB_TOKEN` events), so auto-merge on a required check needs a fine-grained PAT or GitHub
  App token stored as the `GH_PR_TOKEN` repository secret, with pull-request **and contents**
  write access (the staging job pushes the branch with it).
- Every job that runs graph-agents-cli sets `GRAPH_AGENTS_CLI_DISABLE_OVERRIDES=1`, so project or
  user extensions cannot replace `lint`, `eval` or `deploy` in CI and CD.
- `.github/agent.env` (rendered for every project, read only by the workflows): `IMAGE_REPOSITORY`,
  `RELEASE_NAME`, `CHART_PATH`, `RUNTIME`, `CD` and `GRAPH_AGENTS_CLI_SPEC` (where CI installs the
  CLI: the creating version's git tag). It is data, never sourced: one `NAME=VALUE` per line, `#`
  comments and blank lines skipped, one pair of surrounding quotes and a trailing ` # comment`
  dropped. Any other name (including the retired `CLI_VERSION_PIN`), a duplicate or a control
  character fails the `Load project settings` step before anything is exported. To add a setting,
  extend `known` in all three workflows.
- `.github/CODEOWNERS`: `/.github/`, `/CODEOWNERS`, `/api-policy.yaml`, `/tests/eval/`, the
  extensions files, the manifest, and `/deployment/` except `values-dev.yaml` and
  `values-staging.yaml` (left unowned so the staging PR can auto-merge) ->
  `@CHANGE-ME/production-approvers` (placeholder to fill; `infra check` reports it).

The `Application` manifests and the values files are config: `upgrade` never overwrites them.

## The `deploy` PR flow

```bash
graph-agents-cli deploy --env staging --image ghcr.io/acme/my-agent:abc1234
graph-agents-cli deploy --env prod --image ghcr.io/acme/my-agent:abc1234
```

1. Reads `values-<env>.yaml` **as `origin/main` holds it** (`git cat-file blob origin/main:<path>`;
   `HEAD` when `origin/main` is unavailable), changes `image.tag` and nothing else. Your working
   tree is not modified and never used: a checkout behind `main` or with local edits cannot leak
   into the PR, and "nothing to change" is judged on the base branch (a closed, unmerged PR is
   re-opened by a re-run). A values file not committed on the base is exit 3. Paths are
   repository-relative, so a project below the git root works.
2. Builds branch `deploy/<env>/<short sha>` with git plumbing (`hash-object --stdin`,
   `read-tree`, `update-index`, `write-tree`, `commit-tree`, `update-ref`) and pushes it with
   `git push --force-with-lease=refs/heads/<branch>:<sha on origin>`; when `origin/<branch>`
   already holds the change it prints "nothing to push", so a retry is idempotent. Your checkout
   and index are never switched. A git identity must be configured (`commit-tree` needs one; the
   scaffolded workflow sets it).
3. Opens or updates a PR against `main` with `gh` when available, else GitHub REST with
   `GITHUB_TOKEN`, `GH_TOKEN` or `GH_ENTERPRISE_TOKEN`. Only GitHub and GitHub Enterprise Server
   hosts are supported; a GHES host is recognised from `GH_HOST` (or `GITHUB_HOST`,
   `GITHUB_SERVER_URL`), API base `https://<host>/api/v3`. Without `gh` and without a token the
   branch is still pushed and the command exits 3 with instructions.
4. Prints the PR URL and stops. **You never merge it.** Argo reconciles after the merge.

`deploy` prints "No cluster is contacted" in this mode: no kube context is used, so `--context`
and `--yes` change nothing here. `--env-file` and `--rotate-api-key` are refused (exit 1):
Secrets are applied with `secrets apply`. `--dry-run` prints the values rewrite, the git plumbing
and the `gh pr list/create` commands. `--image` names the CI-pushed image (a tagged reference; a
digest reference is refused with exit 3 because the chart has no `image.digest`; a repository
other than the chart's `image.repository` gets a warning, since only `image.tag` is written);
without it `deploy` uses `--tag` or the short git sha and warns that the image must already have
been pushed by CI (the CLI does not build or push in this mode). A chart `image.repository` that
still holds `CHANGE-ME` is exit 3.

## The single production gate

Production desired state changes only through a PR touching `values-prod.yaml`. Two paths open
such a PR (`promote-to-prod` from CI, `deploy --env prod` from a workstation); neither merges.
The merge requires:

- review from the production approvers in `CODEOWNERS` for `values-prod.yaml`,
- no self-approval (branch protection "prevent self-approval" and environment "prevent
  self-review"),
- `pr_checks` green.

Argo watches `main`, so approval necessarily precedes the production change on both paths. No
inbound access from GitHub to the cluster exists. No Argo Image Updater is used. After deploying,
Argo's own health assessment (the readiness probe on `/ready`) shows whether the rollout landed;
the workflows have no cluster access by design.

## Operating in argocd environments

| Need | Command |
|---|---|
| Status | `graph-agents-cli deploy --status --env <env>` (`argocd app get <name>-<env>`) |
| Restart after secret rotation | `graph-agents-cli deploy --restart --env <env>` (`kubectl rollout restart`); the CLI warns that self-heal may revert the annotation and recommends an Argo resource action (`argocd app actions run <name>-<env> restart --kind Deployment`) |
| Secrets | `graph-agents-cli secrets apply/status --env <env>` from the owner's workstation; Argo never manages the app Secret |
| Rollback | revert the values change on `main` through a PR (same gate); for emergencies `argocd app history` / `argocd app rollback` on the Argo side, then reconcile git |
| Diff before merge | `argocd app diff <name>-<env> --revision <branch>` |

`--status`, `--restart` and `secrets` follow the kube context rules (recorded context or
`--context`; outside dev the current context needs a confirmation or `--yes`). Direct
`helm upgrade` or `kubectl apply` against an argocd environment is drift: self-heal reverts it
(dev, staging) or Argo reports `OutOfSync` (prod). Do not do it.

## `helm-push` for comparison

A self-hosted GitHub runner inside the network gets the cluster credentials from the
`DEPLOY_KUBECONFIG` secret of the `staging` or `production` **environment** (written under
`RUNNER_TEMP` and removed after the job), resolves one kube context (the manifest's
`environments.<env>.context`, else the kubeconfig's current one), runs
`graph-agents-cli deploy --env <env> --image <ref> --context "$KUBE_CONTEXT" --yes` (helm only),
and verifies the rollout (`kubectl rollout status`, `/health` and `/ready` through a
port-forward; the runner needs `kubectl` and `curl`). The `production` job declares
`environment: production` and is gated by that environment's reviewers. Workstation deploys are
allowed for `dev`; `staging`/`prod` are refused outside CI (`GITHUB_ACTIONS=true`) even with
`--image`, unless `--force-direct`. Secrets are still provisioned by the owner from a
workstation, never by CI; the runner's kubeconfig needs permission to read the Secret (deploy
checks its required keys).
