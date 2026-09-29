---
description: "How changes reach a cluster in each graph-agents-cli CD mode, what the generated GitHub Actions workflows do, and the repository settings they need."
---

# CI/CD

<p class="gac-lede">Every project gets a pull-request check with an eval gate. Choose a CD mode
and it also gets workflows that build each commit once, deploy it to staging and promote it to
production behind a human gate.</p>

## Choose a CD mode

The mode is fixed at `create` time with `--cd` and changed later with
`graph-agents-cli scaffold enhance --cd <mode>`:

```bash
graph-agents-cli create my-agent --cd argocd --registry ghcr.io/my-org
graph-agents-cli scaffold enhance --cd helm-push      # switch an existing project
```

| Mode | What `deploy` does | What CI does |
|---|---|---|
| `skip` (default) | Builds the image, side-loads it into a local cluster or pushes it, applies the Secret, `helm upgrade --install`, for any environment. | `pr_checks` only (lint, tests, eval gate). |
| `helm-push` | Deploys `dev` directly; `staging` and `prod` are refused outside CI (even with `--image`) unless `--force-direct`. Checks the live Secret, never applies it. | `staging` builds and deploys from `main` on a self-hosted runner; `promote-to-prod` deploys behind the GitHub `production` environment. Both verify the rollout through `/health` and `/ready`. |
| `argocd` | Never runs Helm and contacts no cluster: opens a pull request that changes only `image.tag` in `values-<env>.yaml`. `--status` and `--restart` use the cluster. | Builds, pushes and opens the staging pull request with auto-merge (newer ones supersede older ones). Production is a pull request merged by a human after code-owner review, then an Argo CD sync. |

[Deploy to Kubernetes](deploy.md) describes the `deploy` column in full. Pick `skip` to deploy
from a workstation, `helm-push` when a runner inside your network may hold cluster credentials,
and `argocd` when the cluster should pull its desired state from git.

## The generated workflows

| Workflow | Modes | Trigger | Runner | What it does |
|---|---|---|---|---|
| `pr_checks` | all | pull requests to `main` | `ubuntu-latest` | `uv sync --locked`, `ruff check`, unit and integration tests on the fake model, `graph-agents-cli lint` (code and API policy), then the eval gate. |
| `staging` | `helm-push`, `argocd` | pushes to `main`; by hand from `main` | `ubuntu-latest`, then self-hosted for `helm-push` | Builds and pushes the image tagged with the short commit sha, then lands it in staging. |
| `promote-to-prod` | `helm-push`, `argocd` | by hand from `main` (`image_tag`, default: the tag in `values-staging.yaml`) | `ubuntu-latest`, self-hosted for `helm-push` | Deploys that image to prod in the `production` environment, or opens the production pull request. |

Every job that runs the CLI installs it with `uvx --from "$GRAPH_AGENTS_CLI_SPEC"`, pinned by
[`.github/agent.env`](#githubagentenv), and sets `GRAPH_AGENTS_CLI_DISABLE_OVERRIDES=1`, so an
extension can never replace the gate's `lint` or `eval`. `staging` and `promote-to-prod` run in
the concurrency groups `staging` and `production`, one run at a time.

`staging` ignores pushes that only change Markdown, `docs/` or the `values-<env>.yaml` files,
so a merged image-tag pull request never triggers another build. Run it by hand to re-deploy
`main`, for example after changing only `values-staging.yaml` in `helm-push` mode.

### The eval gate in CI

The tests always run on the deterministic fake model. The eval gate uses a real model when the
repository provides one:

| Setting | Kind | Effect |
|---|---|---|
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `MODEL_API_KEY` (openai-compatible) | secret | The project's own provider (from the manifest) runs the gate when its key is set. |
| `MODEL_PROVIDER`, `MODEL_NAME` | variable | Another provider and model; `MODEL_NAME` is required when the provider differs from the project's. |
| `OPENAI_BASE_URL` | variable | The server of an `openai-compatible` provider. |
| `JUDGE_MODEL_PROVIDER`, `JUDGE_MODEL_NAME`, `JUDGE_BASE_URL` | variable | A separate judge. |
| `JUDGE_API_KEY` | secret | The judge's key. |

Without any of them the gate runs on the fake model: it checks the plumbing, not answer
quality, and the run says so with a warning and a job summary. With no dataset under
`tests/eval/datasets/` the gate is skipped. See [Evaluation](evaluation.md).

## The `argocd` flow

Argo CD reconciles each environment from `main`, so nothing reaches a cluster until a pull
request merges.

<div class="grid cards gac-steps" markdown>

-   **Apply the Applications once**

    Set `spec.source.repoURL` in `deployment/argocd/application-<env>.yaml`, give Argo CD a
    repository credential, and apply the three Applications (an operator, once).

-   **Merge to `main`**

    `staging` builds and pushes `<registry>/<name>:<short sha>` and opens
    `deploy/staging/<short sha>` with auto-merge. Argo CD syncs staging automatically.

-   **Promote to prod**

    Run `promote-to-prod`. It opens `deploy/prod/<tag>`; a code owner reviews and merges it,
    then an operator syncs the prod Application by hand.

</div>

The staging pull request changes only `image.tag` in `values-staging.yaml`. A newer build
closes the older pull requests it supersedes, and an older build never replaces a newer one.

You can open the same pull request from a workstation with `deploy`. It builds the change with
git plumbing from `origin/main` (`HEAD` when that is unavailable), commits the one file on
`deploy/<env>/<tag>`, pushes it and opens or updates the pull request with `gh`, or through the
GitHub REST API with `GITHUB_TOKEN` when `gh` is not installed. Your checkout, index and working
tree are left alone. Without `--image` the tag is your short commit sha, which CI must already
have built and pushed.

A dry run prints every git and `gh` command instead of running it (the secrets notice is left
out here):

```console
$ graph-agents-cli deploy --env staging --dry-run
Environment: staging  namespace: my-agent-staging
Mode: argocd (deploy writes desired state; Argo CD reconciles from main)
  No cluster is contacted (kube context docs-unreachable is not used): Argo CD applies the change after the pull request merges.
  No --image given; writing tag '97346c3'. The image must already be pushed by CI for Argo CD to roll it out.
  [dry-run] git fetch origin main
   origin/main is not available locally; the real run would branch from HEAD.
  [dry-run] would set image.tag '' -> '97346c3' in deployment/helm/my-agent/values-staging.yaml (from HEAD; the working tree is left unchanged).
  [dry-run] git hash-object -w --stdin
  [dry-run] git read-tree HEAD
  [dry-run] git update-index --add --cacheinfo '100644,<blob>,deployment/helm/my-agent/values-staging.yaml'
  [dry-run] git write-tree
  [dry-run] git commit-tree '<tree>' -p HEAD -m 'deploy(staging): my-agent -> 97346c3'
  [dry-run] git update-ref refs/heads/deploy/staging/97346c3 '<commit>'
  [dry-run] git push '--force-with-lease=refs/heads/deploy/staging/97346c3:<sha on origin, empty when absent>' -u origin deploy/staging/97346c3:deploy/staging/97346c3
  [dry-run] gh pr list --repo github.com/example/my-agent --head deploy/staging/97346c3 --base main --state open --json number,url
  [dry-run] gh pr create --repo github.com/example/my-agent --base main --head deploy/staging/97346c3 --title 'deploy(staging): my-agent -> 97346c3' --body '...'
Would open pull request on branch deploy/staging/97346c3.
```

Only GitHub and GitHub Enterprise Server remotes are supported. For a GitHub Enterprise Server
remote, set `GH_HOST=<host>` (and `GH_ENTERPRISE_TOKEN` or `GITHUB_TOKEN`) so `deploy` opens
the pull request there.

## The `helm-push` flow

1. A push to `main` runs `staging`: the image is built and pushed on a GitHub-hosted runner,
   then a job in the `staging` environment on your self-hosted runner writes
   `DEPLOY_KUBECONFIG` to a private temporary file and runs
   `graph-agents-cli deploy --env staging --image <ref> --context <ctx> --yes`.
2. It verifies the rollout: `kubectl rollout status` (up to 300 s), then `/health` and `/ready`
   through a port-forward. The kubeconfig is removed when the job ends.
3. `promote-to-prod`, run by hand, deploys the same image to prod in the `production`
   environment, whose required reviewers approve the job before it starts.

The kube context is `environments.<env>.context` from the manifest, else the current context
of `DEPLOY_KUBECONFIG`. A workstation `deploy --env staging|prod` is refused outside CI unless
you pass `--force-direct`.

## Secrets in the CD modes

CI never holds the application's secrets. In `helm-push` and `argocd` mode an operator (the
manifest's `secrets.owner`) runs `graph-agents-cli secrets apply --env <env>` from a workstation
with cluster access, once per environment, and again to rotate a key. `deploy` in those modes
refuses `--env-file` and `--rotate-api-key` and says so:

```text
Error: --env-file is not accepted in argocd mode.
  In argocd mode `deploy` never touches Secrets. the platform operator named in secrets.owner provisions the Secret once per environment from a workstation with cluster access:
    graph-agents-cli secrets apply --env staging   (reads .env.staging; --env-file <file> reads another)
```

See [Secrets](secrets.md).

## Required GitHub settings

`helm-push` and `argocd` rely on these repository settings. None of them is created for you.

- [ ] **Environments** `staging` and `production`. On `production`: required reviewers and
      "Prevent self-review". On both: deployment branches limited to `main`.
- [ ] **Branch protection on `main`:** `pr_checks` required, code-owner review required
      (with stale approvals dismissed), no self-approval, and auto-merge allowed (the staging
      pull request uses it).
- [ ] **CODEOWNERS:** replace `@CHANGE-ME/production-approvers` in `.github/CODEOWNERS`. GitHub
      ignores an owner that does not exist, which silently removes the gate.
- [ ] **`GH_PR_TOKEN`:** a fine-grained personal access token or GitHub App token with
      pull-request and contents write access. Pull requests opened with the workflow's
      `GITHUB_TOKEN` never trigger `pr_checks`, so auto-merge on a required check needs it.
- [ ] **`helm-push`:** `DEPLOY_KUBECONFIG` as a secret of the `staging` and `production`
      *environments*, never a repository secret, so the production reviewers gate it; and a
      self-hosted runner with network access to the cluster and `kubectl` and `curl` installed.
- [ ] **Registry credentials** when the registry is not GHCR: `REGISTRY_USERNAME` and
      `REGISTRY_PASSWORD` secrets (GHCR uses the workflow token).
- [ ] **`argocd`:** Argo CD with a credential for this repository, and the
      `deployment/argocd/` Applications applied once with `repoURL` set.
- [ ] **Optional:** a provider key secret or the `MODEL_PROVIDER` variable (with `MODEL_NAME`
      when it differs from the project's provider), so the eval gate runs on a real model
      ([above](#the-eval-gate-in-ci)).

[`graph-agents-cli infra check --env <env>`](../reference/cli.md#graph-agents-cli-infra-check)
reports these settings when `gh` is logged in or `GITHUB_TOKEN` is set: the two environments
and their reviewers and branch rules, the protection of `main`, whether `DEPLOY_KUBECONFIG`
exists as an environment secret (and no repository-level kubeconfig secret does), and the
`CHANGE-ME` owners and `repoURL`. It changes nothing.

## `.github/agent.env`

The workflows are copied into the project verbatim; their project settings live in
`.github/agent.env`:

```bash title=".github/agent.env"
IMAGE_REPOSITORY=ghcr.io/example/my-agent
RELEASE_NAME=my-agent
CHART_PATH=deployment/helm/my-agent
RUNTIME=fastapi
CD=argocd
GRAPH_AGENTS_CLI_SPEC=git+https://github.com/ss7172/graph-agents-cli@v0.3.0
```

The file is read as `NAME=VALUE` data, never sourced by a shell. Only these six names are
accepted: any other name, a duplicate or a control character fails the step before anything
is exported, so the file cannot change how later steps run. `GRAPH_AGENTS_CLI_SPEC` is where
the workflows install the CLI from; `GRAPH_AGENTS_CLI_INSTALL_SPEC` at `create` time sets it
for a mirror. `scaffold enhance --registry` updates `IMAGE_REPOSITORY`. See
[Project manifest](../reference/manifest.md) for the files the CLI reads.

## Limitations

| Limitation | What to do |
|---|---|
| The generated workflows use actions by version tag, not commit SHA, and the Dockerfiles pin base images by tag ([KI-037](../reference/known-issues.md#ki-037-generated-workflows-and-dockerfiles-pin-by-tag-not-by-commit-sha-or-digest)). | Pin each action to a commit SHA (and base images to a digest) if your organisation requires it. |
| `argocd` staging promotion trusts the branch names of open pull requests, and closes superseded pull requests before its own push ([KI-035](../reference/known-issues.md#ki-035-argocd-staging-promotion-trusts-the-branch-names-of-open-pull-requests), [KI-036](../reference/known-issues.md#ki-036-argocd-staging-promotion-closes-superseded-pull-requests-before-its-own-push)). | Close unexpected `deploy/staging/*` pull requests; give `GH_PR_TOKEN` contents write. |
| Staging pull requests can stall: workstation `-dirty-` tags are never superseded, "require branches to be up to date" holds auto-merge ([KI-080](../reference/known-issues.md#ki-080-staging-promotion-edge-cases-in-argocd-mode)). | Close stale pull requests, set `GH_PR_TOKEN`, re-run the workflow. |
| Post-deploy verification is thin: no check after an `argocd` deploy, no authenticated smoke test, and `pr_checks` never builds the image ([KI-079](../reference/known-issues.md#ki-079-post-deploy-verification-in-the-generated-workflows-is-thin)). | Add a smoke test and an image build to the workflows. |
| No provisioning: the cluster, database, Gateway, GitHub environments and runners are set up by hand; `infra check` only reports ([KI-078](../reference/known-issues.md#ki-078-no-infrastructure-provisioning-or-cicd-bootstrap)). | Follow the checklist above. |
| The `helm-push` workflows refuse kube context names with whitespace or a leading `-` ([KI-083](../reference/known-issues.md#ki-083-the-helm-push-workflows-refuse-kube-context-names-the-cli-accepts)); `infra check`'s secret rows are worded imprecisely ([KI-073](../reference/known-issues.md#ki-073-infra-checks-github-secret-rows-are-worded-imprecisely)). | Use plain context names; check `gh auth status` and secret names directly. |

## Next steps

<div class="grid cards" markdown>

-   :material-kubernetes:{ .lg } **[Deploy to Kubernetes](deploy.md)**

    What `deploy` checks and how it rolls out and rolls back.

-   :material-key-chain-variant:{ .lg } **[Secrets](secrets.md)**

    The operator's side of the CD modes: `secrets apply` and rotation.

-   :material-check-decagram-outline:{ .lg } **[Evaluation](evaluation.md)**

    Datasets, judges and the gate `pr_checks` enforces.

-   :material-file-cog-outline:{ .lg } **[Project manifest](../reference/manifest.md)**

    `environments`, `secrets` and the other settings the workflows read.

</div>
