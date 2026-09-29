---
description: graph-agents-cli-manifest.yaml, the file that records how a project was created and configures the CLI for it, and the workflows' .github/agent.env.
---

# Project manifest

<p class="gac-lede"><code>graph-agents-cli-manifest.yaml</code> records how a project was
created and configures the CLI for it: its settings, the build that rendered it, the auth
policy, the secrets allow-list and the environments.</p>

The manifest sits at the project root; finding it is how every command knows it runs in a
project (outside one, project commands exit 3). Most keys change through commands; a few are
yours to edit.

## A fresh manifest

`graph-agents-cli create my-agent` with the defaults (kubernetes target, `fastapi` runtime,
no git remote) writes:

```yaml
# Project manifest written by `graph-agents-cli create`.
# Authoritative for `info`, `upgrade`, `deploy` and `secrets`; edit deliberately.
name: 'my-agent'
cli_version: '0.2.0'
# The build that rendered this project and a digest of it; `scaffold upgrade` reads both
cli_build:
  id: 0.2.0+g181aebc
  commit: 181aebc93fcc537ebb87420128f354eb9a3f3dd0
  template_digest: sha256:94e37df85f73c13dc7f543f29ed0a6c7ae6b424b4e4266c330154fb4f7d98e08
agent_directory: 'app'
base_template: 'langgraph'
generated_at: '2026-09-25T02:44:19.773124+00:00'
language: 'python'
create_params:
  deployment_target: 'kubernetes'
  runtime: 'fastapi'
  model_provider: 'openai'
  model: 'gpt-5-mini'
  checkpointer: 'postgres'
  registry: 'ghcr.io/CHANGE-ME'
  cd: 'skip'
  auth_policy: 'shared-bearer'
  # false while the custom auth policy stub is in place; deploy --env staging|prod refuses until true
  auth_policy_implemented: true
  agent_guidance_filename: 'AGENTS.md'
environments:
  dev: { context: "", namespace: my-agent-dev }
  staging: { context: "", namespace: my-agent-staging }
  prod: { context: "", namespace: my-agent-prod }
secrets:
  # Only these keys are exported from an env file into the environment's Secret.
  keys: [OPENAI_API_KEY, JUDGE_API_KEY, POSTGRES_DSN, API_KEY, LANGSMITH_API_KEY]
  # Team or role responsible for provisioning and rotating the Secret (free text).
  owner: ""
process: null
```

This project was made by a build between releases, so `cli_build.id` carries a commit
(`0.2.0+g181aebc`); a release build records `0.2.0`. `graph-agents-cli info --json` prints the
same settings as the CLI reads them, defaults filled in.

## Keys

### Identity and build

| Key | Meaning |
|---|---|
| `name` | The project name: the Helm release, the chart directory `deployment/helm/<name>/`, the image name, the namespaces `<name>-<env>` and the Secret `<name>-app`. |
| `cli_version` | The release that created (or last upgraded) the project. `scaffold upgrade` rebuilds the old templates from its tag `v<cli_version>`. |
| `cli_build.id` | The build id `graph-agents-cli --version` printed: `0.2.0` for a release, `0.2.0+g<commit>` between releases, `.dirty` with uncommitted changes. |
| `cli_build.commit` | That build's full commit; `null` when it was not built from git. |
| `cli_build.template_digest` | A digest of what that build renders for the project's settings. Two builds with the same digest render the same project, so an upgrade between them has nothing to do. `null` when `create` rendered more than the templates (a seed `--api-policy`, a remote template). |
| `generated_at` | When the project was generated (UTC). The first place to look for the build that made it when `cli_build` is missing. |
| `agent_directory` | The agent's package (`app`), where `lint` looks for `tools/`; also the default A2A mount name. |
| `base_template`, `language` | The template the project was rendered from (`langgraph`, `python`). |
| `process` | The governing process document given to `create --process` (a path or a string), rendered into the guidance file; `null` otherwise. |

A manifest without `cli_build` (made before builds were recorded) is compared by version only.
How `scaffold upgrade` uses these keys, and `--baseline-ref` when they cannot name the build,
is in [Upgrading projects](../guides/upgrading.md).

### `create_params`

The settings `create` resolved. Change them with `graph-agents-cli scaffold enhance` (which
re-renders the files they shape), not by hand.

| Key | Values | Read by |
|---|---|---|
| `deployment_target` | `kubernetes`, `none` | `deploy`, `secrets`, `infra check` (refused for `none`) |
| `runtime` | `fastapi`, `langgraph-server` | `build` (the Dockerfile), `run`, the default secret keys |
| `model_provider`, `model` | `openai`, `anthropic`, `gemini`, `openai-compatible`; the model name | `login`, the judge default, the provider's secret key |
| `checkpointer` | `memory`, `postgres` | `approvals` and `run` (a paused run on `memory` needs its server kept running) |
| `registry` | `<host>/<org>` | `build` and `deploy`: the image is `<registry>/<name>`. The placeholder `ghcr.io/CHANGE-ME` is refused (exit 3); `scaffold enhance --registry` sets it here, in the chart values and in `.github/agent.env`. |
| `cd` | `skip`, `helm-push`, `argocd` | `deploy`: how a change reaches a cluster. See [CI/CD](../guides/cicd.md). |
| `auth_policy` | `shared-bearer`, `jwt`, `custom` | `run`, `eval`, `login`, `auth dev-token`, the required Secret keys |
| `auth_policy_implemented` | `true`, `false` | `deploy`: `false` (the `custom` stub) refuses staging and prod. |
| `agent_guidance_filename` | `AGENTS.md` by default | The coding-agent guidance file `create` rendered. |

### `environments`

Present for the kubernetes target: `dev`, `staging` and `prod`, each with:

| Key | Default | Meaning |
|---|---|---|
| `context` | `""` | The kube context `deploy` and `secrets` use. Empty means the kubeconfig's current context, which outside `dev` needs a confirmation (or `--yes`). `--context` wins over it. `system deploy` and `system check --live` require it outside `dev`. |
| `namespace` | `<name>-<env>` | The namespace of the release and its Secret. |

Record the contexts of staging and prod here, so a deploy never lands on whatever context is
current. See [Deploy to Kubernetes](../guides/deploy.md).

### `secrets`

| Key | Meaning |
|---|---|
| `keys` | The allow-list: the only variables `secrets apply` and `deploy` copy from an env file into the app Secret. |
| `owner` | Free text: the team or role that provisions and rotates the Secret (in `argocd` and `helm-push` modes, who runs `secrets apply`). |

The default list follows the settings: the provider key (`OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, `GOOGLE_API_KEY` or `MODEL_API_KEY`), `JUDGE_API_KEY`, `POSTGRES_DSN`
(`DATABASE_URI` and `REDIS_URI` under `langgraph-server`), `API_KEY` under `shared-bearer`,
`LANGSMITH_API_KEY`, then the `token_env` of every `auth: bearer` API. Add any other secret
your project reads (for example `METRICS_TOKEN`, `PRINCIPAL_HASH_SALT`); remove a key from the
Secret by dropping it here. See [Secrets](../guides/secrets.md).

### `api_policy`

Present only when the project has an `api-policy.yaml`:

```yaml
api_policy:
  policy_file: api-policy.yaml
```

The `api` commands keep it in step with the policy file. The policy itself is described in
[api-policy.yaml](api-policy-schema.md).

## Who writes it

| Writer | What changes | Comments |
|---|---|---|
| `create` | The whole file | Written from the template, with its comments |
| `graph-agents-cli api ...` | `api_policy` and the bearer tokens in `secrets.keys` | Kept: the edit is made in place |
| `scaffold enhance` | The settings it changes, `secrets.keys` (defaults follow the new settings; keys you added stay), `cli_build` | Rewritten without comments |
| `scaffold upgrade` | `cli_version`, `cli_build` | Rewritten without comments |

Edit by hand only what nothing else owns:

- `environments.<env>.context` (and `namespace`, if your cluster's naming differs)
- `secrets.keys` and `secrets.owner`
- `auth_policy_implemented: true`, once the `custom` policy is implemented and tested
  (see [Authentication](../guides/authentication.md))

The rest (`create_params`, `cli_version`, `cli_build`, `api_policy`) changes through
`scaffold enhance`, `scaffold upgrade` and `api`, which also update the files that depend on
it. A manifest with the retired `product_api:` block stops `create`, `enhance`, `upgrade` and
`lint` with a migration message (exit 3); see the [Changelog](changelog.md).

## The workflows' settings

Generated projects also carry `.github/agent.env`, the settings the GitHub workflows read:

```ini
IMAGE_REPOSITORY=ghcr.io/CHANGE-ME/my-agent
RELEASE_NAME=my-agent
CHART_PATH=deployment/helm/my-agent
RUNTIME=fastapi
CD=skip
# Where the workflows install graph-agents-cli from (uvx --from "$GRAPH_AGENTS_CLI_SPEC").
GRAPH_AGENTS_CLI_SPEC=git+https://github.com/ss7172/graph-agents-cli@v0.2.0
```

It is read as `NAME=VALUE` data, never sourced by a shell: only these six names are accepted,
and any other name, a duplicate or a control character fails the step before anything is
exported. `GRAPH_AGENTS_CLI_SPEC` pins the CLI version CI installs; it follows
`GRAPH_AGENTS_CLI_INSTALL_SPEC` when that is set as the project is rendered
(see [Environment variables](environment.md#install-source-override)). The required GitHub
settings are in [CI/CD](../guides/cicd.md).

<div class="grid cards gac-cols-3" markdown>

-   :material-update:{ .lg } **[Upgrading projects](../guides/upgrading.md)**

    How `cli_version` and `cli_build` pick the baseline of an upgrade.

-   :material-key-chain-variant:{ .lg } **[Secrets](../guides/secrets.md)**

    The allow-list in practice: apply, check and rotate.

-   :material-kubernetes:{ .lg } **[Deploy to Kubernetes](../guides/deploy.md)**

    Environments, contexts and the CD modes.

</div>
