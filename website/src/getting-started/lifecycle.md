---
description: The stages of a graph-agents-cli project from create to operate, the commands in each, and how a change reaches production.
# The five-stage diagram needs the width the table of contents would take.
hide:
  - toc
---

# The lifecycle

<p class="gac-lede">Every project walks the same loop: create it once, then develop, evaluate
and deploy each change, and operate what runs. Each stage is a handful of commands, and
each command's exit code says what happened.</p>

<ol class="gac-lifecycle" markdown="block">

<li markdown="block">

[Create](quickstart.md)
{: .gac-lifecycle__stage }

A service with its API, auth, policy, chart and CI.

`create` `scaffold enhance`

</li>

<li markdown="block">

[Develop](../guides/develop.md)
{: .gac-lifecycle__stage }

Tools, the APIs they may call, quick runs.

`run` `playground` `api` `lint`

</li>

<li markdown="block">

[Evaluate](../guides/evaluation.md)
{: .gac-lifecycle__stage }

Every case graded; the exit code is the gate.

`eval run` `eval compare`

</li>

<li markdown="block">

[Deploy](../guides/deploy.md)
{: .gac-lifecycle__stage }

Image, Secret, Helm release or Argo CD pull request.

`build` `secrets apply` `deploy`

</li>

<li markdown="block">

[Operate](../guides/observability.md)
{: .gac-lifecycle__stage }

Rollouts, approvals, upgrades.

`deploy --status` `approvals` `scaffold upgrade`

</li>

</ol>

<p class="gac-lifecycle__loop" markdown>Create happens once; every later change goes round
develop, evaluate and deploy again.</p>

## Two ways to drive it

You can type every command yourself or ask a coding agent that has the skills. Both run the
same CLI, and the skills stop for your review where a human decision belongs: the spec, a
wider API policy, an approval, a deploy.

<div class="grid cards gac-cols-3" markdown>

-   :material-robot-outline:{ .lg } **[With a coding agent](tutorial-coding-agent.md)**

    "Use graph-agents-cli to build ..." and review each gate.

-   :material-console-line:{ .lg } **[By hand](tutorial-manual.md)**

    Every command, with its output and what to notice.

-   :material-rocket-launch-outline:{ .lg } **[Quickstart](quickstart.md)**

    Five minutes, no model key.

</div>

## Commands by stage

Each command links to its entry in the [CLI reference](../reference/cli.md), which lists
every flag.

### Before the first project

| Command | What it does |
|---|---|
| [`setup`](../reference/cli.md#graph-agents-cli-setup) | Install graph-agents-cli and the skills into the coding agents it detects |
| [`update`](../reference/cli.md#graph-agents-cli-update) | Refresh the skills, then move the CLI and the skills to the latest release |
| [`login`](../reference/cli.md#graph-agents-cli-login) | Check provider keys, LangSmith and the kubeconfig; optionally write `.env` |

### Create

| Command | What it does |
|---|---|
| [`create`](../reference/cli.md#graph-agents-cli-create) | Create a LangGraph agent project from a template (same as `scaffold create`) |
| [`scaffold enhance`](../reference/cli.md#graph-agents-cli-scaffold-enhance) | Add or change the deployment target, CD mode, runtime or model provider of a project |

### Develop

| Command | What it does |
|---|---|
| [`install`](../reference/cli.md#graph-agents-cli-install) | Install the project's dependencies (`uv sync`) |
| [`run`](../reference/cli.md#graph-agents-cli-run) | Send one prompt to a local server (started on demand) or a deployed URL |
| [`playground`](../reference/cli.md#graph-agents-cli-playground) | Serve the app locally with reload and the dev chat page |
| [`api`](../reference/cli.md#graph-agents-cli-api) | Declare and change the outbound APIs tools may call (`api-policy.yaml`) |
| [`lint`](../reference/cli.md#graph-agents-cli-lint) | Run ruff and the API-policy check |
| [`auth dev-token`](../reference/cli.md#graph-agents-cli-auth-dev-token) | Mint a JWT for local runs of a `jwt` project |
| [`info`](../reference/cli.md#graph-agents-cli-info) | Show the project's configuration, paths and the CLI version |

### Evaluate

| Command | What it does |
|---|---|
| [`eval run`](../reference/cli.md#graph-agents-cli-eval-run) | `eval generate`, then `eval grade`; the exit code is the gate |
| [`eval generate`](../reference/cli.md#graph-agents-cli-eval-generate) | Run the agent over the eval dataset and write traces |
| [`eval grade`](../reference/cli.md#graph-agents-cli-eval-grade) | Grade traces against the checks and judges and apply the gate |
| [`eval compare`](../reference/cli.md#graph-agents-cli-eval-compare) | Compare two results files, a baseline and a candidate |
| [`eval analyze`](../reference/cli.md#graph-agents-cli-eval-analyze) | Cluster the failed, errored and missing cases by reason |
| [`eval metric list`](../reference/cli.md#graph-agents-cli-eval-metric-list) | List the deterministic checks, built-in judges and the project's metrics |
| [`eval submit`](../reference/cli.md#graph-agents-cli-eval-submit) | Upload a dataset and a results file to LangSmith |

### Deploy

| Command | What it does |
|---|---|
| [`build`](../reference/cli.md#graph-agents-cli-build) | Build the agent's container image |
| [`secrets apply`](../reference/cli.md#graph-agents-cli-secrets-apply) | Create or update the app Secret from the allow-listed keys of an env file |
| [`secrets status`](../reference/cli.md#graph-agents-cli-secrets-status) | List which allow-listed keys the Secret holds (never their values) |
| [`infra check`](../reference/cli.md#graph-agents-cli-infra-check) | Report which cluster and repository prerequisites exist (read-only) |
| [`deploy`](../reference/cli.md#graph-agents-cli-deploy) | Deploy to Kubernetes the way the project's CD mode says |

### Operate

| Command | What it does |
|---|---|
| [`deploy --status`](../reference/cli.md#graph-agents-cli-deploy) | Report the rollout, the pods and their warning events |
| [`deploy --restart`](../reference/cli.md#graph-agents-cli-deploy) | Restart the pods (after a Secret rotation) and wait for the new ones |
| [`approvals`](../reference/cli.md#graph-agents-cli-approvals) | List and decide the gated API calls that runs are waiting on |
| [`scaffold upgrade`](../reference/cli.md#graph-agents-cli-scaffold-upgrade) | Upgrade the project to a newer graph-agents-cli, keeping your edits |

### Extend

| Command | What it does |
|---|---|
| [`extension`](../reference/cli.md#graph-agents-cli-extension) | Add, list, remove or update extensions that override or add commands (experimental) |

## Exit codes

Every command follows one contract, which is what CI jobs and coding agents act on:

| Code | Meaning |
|---|---|
| 0 | Success: the eval gate is met, the Secret holds every required key, a run finished or waits for an approval |
| 1 | Refused or failed: a policy or mode said no, a confirmation was declined, a gate failed |
| 2 | A tool failed: helm, kubectl, docker, git or gh failed or is missing, the agent could not be reached, an eval case errored |
| 3 | Configuration error: not in a project, an invalid manifest, env file, policy, port or context |

A failed gate (1) asks for a change to the agent; a tool failure (2) asks for a retry or a
fix to the machine; a configuration error (3) asks for a fix to the project. The details of
each command are on [Exit codes](../reference/exit-codes.md).

## Environments and CD modes

A Kubernetes project has three environments, `dev`, `staging` and `prod`, each with its own
values file (`deployment/helm/<name>/values-<env>.yaml`), namespace (`<name>-<env>`) and
Secret. `dev` bundles its own Postgres; staging and prod use a database you run.

The `--cd` choice at create time decides how a change reaches a cluster:

| Mode | How a change reaches the cluster |
|---|---|
| `skip` (default) | `deploy` builds the image, loads it into a local cluster or pushes it, applies the Secret and runs Helm, for any environment |
| `helm-push` | CI builds and deploys `main` to staging; production deploys behind a GitHub environment gate |
| `argocd` | `deploy` never runs Helm: it opens a pull request that changes the image tag, and Argo CD applies what is merged |

[Deploy to Kubernetes](../guides/deploy.md) covers environments and direct deploys;
[CI/CD](../guides/cicd.md) covers the two GitOps modes and the GitHub settings they need.

## Runs locally, runs disconnected

"Runs locally" means the orchestration runs on your machine: the server, the evals, the
image build. "Runs disconnected" means the whole lifecycle works without internet access:
an on-network model server behind the `openai-compatible` provider, dependencies from a
private index, mirrored images, tracing to an in-cluster collector, no update check.
`login --profile disconnected` and `infra check --profile disconnected` verify it; see
[Offline profile](../guides/offline.md).

## What you own, what the CLI renews

`create` writes the whole project once. After that, some files are yours alone and others
follow the CLI's templates through [`scaffold upgrade`](../guides/upgrading.md):

| Files | Who changes them |
|---|---|
| `app/agent.py`, `app/tools/`, `app/policies/`, `app/prompts/`, `app/graph/` | You. `scaffold upgrade` never touches agent code |
| `api-policy.yaml` | You, through `graph-agents-cli api` and a reviewed pull request. `create` only seeds it |
| `.env`, `.env.<env>`, `values-<env>.yaml`, `tests/eval/datasets/`, `tests/eval/eval_config.yaml` | You. Upgrades leave them alone |
| `graph-agents-cli-manifest.yaml` | `create`, `scaffold enhance`, `scaffold upgrade` and `api` write it; edit it deliberately |
| Everything else: `app/app_utils/`, `app/fast_api_app.py`, the Dockerfile, the chart's `values.yaml` and templates, the workflows | The templates. `scaffold upgrade` replaces what you did not edit and reports a conflict where you did |

The policy travels with the code: each image carries exactly one `api-policy.yaml`, so what
passed staging is what reaches production. Only base URLs and tokens differ between
environments.

## Next steps

<div class="grid cards gac-cols-3" markdown>

-   :material-console-line:{ .lg } **[Tutorial: manual workflow](tutorial-manual.md)**

    Walk the whole loop once, command by command.

-   :material-book-open-variant:{ .lg } **[Guides](../guides/index.md)**

    One page per task: auth, the API policy, approvals, evaluation, deployment.

-   :material-console:{ .lg } **[CLI reference](../reference/cli.md)**

    Every command and flag.

</div>
