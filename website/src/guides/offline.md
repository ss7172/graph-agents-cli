---
description: "Run the whole graph-agents-cli lifecycle without internet access: an on-network model, mirrored dependencies and images, and checks that fail on any hosted dependency."
---

# Offline profile

<p class="gac-lede">Run the whole lifecycle offline, from <code>create</code> to production,
with no internet access at all. The CLI calls this the <em>disconnected profile</em>, and two
commands check it for you.</p>

## Local is not disconnected

Two different promises:

- **Runs locally:** the orchestration runs on your machine. `run`, `playground` and `eval`
  start a local server, but the model can still be a hosted API, and `setup` still fetches
  skills from GitHub.
- **Runs disconnected:** nothing in the lifecycle needs the internet. The model, the judge, the
  package index, the images, the charts, the traces and CI all live on your network.

The disconnected profile is the second. Nothing in it is on by default; the checklist below
sets it up, and [`login --profile disconnected`](#check-it) and
[`infra check --profile disconnected`](#check-it) verify it.

## The profile

- [ ] **An on-network model.** `MODEL_PROVIDER=openai-compatible` with `OPENAI_BASE_URL` at a
      server on your network (vLLM, TGI, Ollama) and a model that supports tool calling.
      `MODEL_API_KEY` is optional for servers that need no key. In a cluster, set
      `env.OPENAI_BASE_URL` in `values-<env>.yaml`; `deploy` refuses the `CHANGE-ME` default
      outside `dev`.
- [ ] **An on-network judge.** `JUDGE_MODEL_PROVIDER=openai-compatible` with `JUDGE_BASE_URL`,
      `JUDGE_MODEL_NAME` and, if needed, `JUDGE_API_KEY`. Without them the judge uses the
      agent's provider.
- [ ] **The `fastapi` runtime.** The LangGraph Server image checks its licence with a hosted
      service at startup, so `langgraph-server` is outside the profile.
- [ ] **A private package index.** Point uv at it (`UV_INDEX_URL`) and install from the lock
      with `graph-agents-cli install --locked`. The image's builder stage runs
      `uv sync --frozen` from `uv.lock` as well, so the build needs the same index.
- [ ] **Mirrored base images.** The Dockerfile's `PYTHON_IMAGE`
      (`python:3.12.14-slim-bookworm`) and `UV_IMAGE` (`ghcr.io/astral-sh/uv:0.12.18`) build
      arguments name the images; change their defaults to your mirror.
- [ ] **An on-network registry**, such as Harbor or `registry:2`, set with
      `create --registry` or `scaffold enhance --registry`. `ghcr.io`, `docker.io`, `quay.io`,
      `gcr.io` and the cloud registries (`*.amazonaws.com`, `*.pkg.dev`, `*.azurecr.io`) are
      hosted.
- [ ] **Vendored subcharts.** The dev Postgres and Redis subcharts come from
      `registry-1.docker.io`. Run `helm dependency build deployment/helm/<name>` on a
      connected machine and bring `deployment/helm/<name>/charts/` across (the scaffold's
      `.gitignore` excludes it, so drop that line if you commit it). With `charts/` in place,
      `deploy` skips the fetch. Point `postgresql.image.registry` (and `redis.image.registry`)
      at your mirror.
- [ ] **Tracing off, or OTLP to a collector in the cluster.** Leave `TRACING_ENABLED` off, or
      set it with `OTEL_EXPORTER_OTLP_ENDPOINT` (`tracing.otlpEndpoint` in the chart) at an
      in-cluster collector. No `LANGSMITH_API_KEY`: hosted LangSmith is outside the profile.
      See [Observability](observability.md#tracing).
- [ ] **No update check.** `export GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1`, or the CLI asks GitHub
      for a newer release (at most every 12 hours) and `info` runs `npx skills list`.
- [ ] **The CLI from a mirror.** Set `GRAPH_AGENTS_CLI_INSTALL_SPEC`, with `{version}` where
      the version goes, so `setup`, `update`, the `scaffold upgrade` baseline and new projects'
      CI install from your mirror (see [Upgrading projects](upgrading.md#install-the-baseline-from-a-mirror)).
      A mirror of PyPI serves releases from 0.3.1 on as `graph-agents-cli=={version}`, with
      `UV_INDEX_URL` naming the mirror; earlier releases exist only as git tags.
- [ ] **The skills from the wheel.** `setup` tries the repository first, then installs the
      skills bundled in the CLI's wheel with `npx skills add`, and without `npx` copies them
      into `~/.agents/skills` (`./.agents/skills` with `--workspace`). No git or network is
      needed for the last two.
- [ ] **No GitHub-hosted CI.** Use `cd: skip` and deploy directly, unless an on-network GitHub
      Enterprise Server runs Actions (declare it with `GH_HOST`). Every project also has
      `pr_checks.yaml` with `runs-on: ubuntu-latest`: point it at your own runner label, or
      remove it.

## Set it up

```bash
export GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1
export GRAPH_AGENTS_CLI_INSTALL_SPEC='git+https://git.example.com/graph-agents-cli@v{version}'
export UV_INDEX_URL=https://pypi.example.com/simple

graph-agents-cli create my-agent --model-provider openai-compatible --model my-model \
    --registry registry.internal.example/agents
cd my-agent
cp .env.example .env        # set OPENAI_BASE_URL to the on-network server
graph-agents-cli install --locked
graph-agents-cli login --write-env --profile disconnected
```

`create` defaults to `fastapi` and `cd: skip`, and `--registry` keeps the image off hosted
registries.

## Check it

Both checks fail on any hosted dependency. `login` reads your environment and `.env`;
`infra check` reads the project and the chart values.

| Check | `login --profile disconnected` | `infra check --profile disconnected` |
|---|---|---|
| A hosted model provider | fails | fails |
| A hosted judge (`JUDGE_MODEL_PROVIDER`) | fails | |
| `LANGSMITH_API_KEY` set | fails | |
| Tracing on without an OTLP endpoint | fails | fails (`tracing.enabled` without `tracing.otlpEndpoint`) |
| `OPENAI_BASE_URL` | required; `<url>/models` is probed (advisory) | must be set in the chart `env` |
| GitHub-hosted CI | fails on a GitHub-hosted runner label (`ubuntu-*`, `windows-*`, `macos-*`) in `.github/workflows/`; warns when `cd` is not `skip` | fails when `cd` is not `skip`; warns instead when `GH_HOST` names a GitHub Enterprise Server |
| The `langgraph-server` runtime | fails | fails |
| A hosted registry | | fails |
| `GRAPH_AGENTS_CLI_NO_UPDATE_CHECK` not `1` | warns | warns |

`login` exits 1 when a check fails (0 with `--status`); `infra check` exits 1 when a required
item is missing.

A new `openai-compatible` project fails one check, because of the `pr_checks` runner:

```console
$ graph-agents-cli login --profile disconnected
...
  ✓ provider: model provider openai-compatible (.env)
  ! provider_key: MODEL_API_KEY not set (optional for servers that do not require a key)
      Set MODEL_API_KEY in .env or run 'graph-agents-cli login --write-env'.
  ! openai_base_url: OPENAI_BASE_URL set but http://127.0.0.1:21248/v1/models is unreachable (ConnectError)
      Start the on-network model server or fix OPENAI_BASE_URL; the check is advisory.
  ✓ api_key: API_KEY set (.env)
  - judge: judge defaults to the agent's provider and key
  - tracing: TRACING_ENABLED is not true; tracing off
  ✓ kubeconfig: current context: docs-unreachable
  ✓ profile.runtime: runtime fastapi
  ✗ profile.ci: GitHub-hosted CI detected: .github/workflows/pr_checks.yaml uses a GitHub-hosted runner
      CI/CD is outside the disconnected profile unless an on-network GitHub Enterprise Server hosts Actions; use 'cd: skip' with direct-mode deploy.
  ✓ profile.update_check: GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1

  5 ok, 2 warning(s), 1 failed, 2 skipped.
  Nothing is stored by the CLI.
```

With `runs-on: [self-hosted, linux]` in `pr_checks.yaml` it passes:

```text
  ✓ profile.ci: no GitHub-hosted CI dependency
  ...
  6 ok, 2 warning(s), 0 failed, 2 skipped.
```

A default project (the `openai` provider, a LangSmith key and `argocd` on GitHub-hosted
runners) fails three checks:

```text
  ✗ provider: model provider openai (manifest) is hosted; the disconnected profile requires openai-compatible
      Set MODEL_PROVIDER=openai-compatible and OPENAI_BASE_URL to an on-network server.
  ✗ tracing: LANGSMITH_API_KEY set (.env); LangSmith is a hosted dependency
      Unset LANGSMITH_API_KEY and export traces over OTLP to an in-cluster collector, or disable tracing.
  ✗ profile.ci: GitHub-hosted CI detected: .github/workflows/pr_checks.yaml uses a GitHub-hosted runner; .github/workflows/promote-to-prod.yaml uses a GitHub-hosted runner; .github/workflows/staging.yaml uses a GitHub-hosted runner
```

`infra check --profile disconnected` adds its rows to the usual report:

```text
│ disconnected: model         │ ok     │ yes      │ openai-compatible          │
│ provider                    │        │          │                            │
│ disconnected:               │ ok     │ yes      │ http://vllm.models.svc.cl… │
│ OPENAI_BASE_URL             │        │          │                            │
│ disconnected: runtime       │ ok     │ yes      │ fastapi                    │
│ disconnected: registry      │ ok     │ yes      │ registry.internal.example… │
│ disconnected: tracing       │ ok     │ yes      │ off                        │
│ disconnected: ci/cd         │ ok     │ yes      │ cd is skip: lifecycle      │
│                             │        │          │ covered by direct-mode     │
│                             │        │          │ deploy                     │
│ disconnected: update check  │ ok     │ no       │ GRAPH_AGENTS_CLI_NO_UPDAT… │
└─────────────────────────────┴────────┴──────────┴────────────────────────────┘
All required prerequisites are present.
```

Add `--env <env>` to check the cluster as well; see [Deploy to Kubernetes](deploy.md#check-the-cluster).

## What stays outside the profile

- `eval submit`, which uploads results to LangSmith.
- The `langgraph-server` runtime, until your licence works without a hosted check.
- `helm-push` and `argocd` on GitHub-hosted Actions. On an on-network GitHub Enterprise Server,
  `infra check` warns instead of failing; set `GH_HOST` (and `GH_ENTERPRISE_TOKEN` or
  `GITHUB_TOKEN`) so `deploy` opens its pull requests there.

## Next steps

<div class="grid cards" markdown>

-   :material-download:{ .lg } **[Installation & setup](../getting-started/installation.md)**

    Install the CLI and the skills, and what `setup` falls back to.

-   :material-kubernetes:{ .lg } **[Deploy to Kubernetes](deploy.md)**

    Direct deploys, local clusters and vendored chart dependencies.

-   :material-chart-timeline-variant:{ .lg } **[Observability](observability.md)**

    OTLP tracing to a collector in your cluster.

-   :material-tune-variant:{ .lg } **[Environment variables](../reference/environment.md)**

    `GRAPH_AGENTS_CLI_*`, `OPENAI_BASE_URL`, `JUDGE_*` and the rest.

</div>
