---
name: graph-agents-cli-scaffold
description: >
  This skill should be used when the user wants to "create an agent project",
  "start a new LangGraph project", "build me a new agent", "scaffold a
  project", "add Kubernetes deployment", "add CI/CD to my project", "add
  Argo CD", "enhance my project", or "upgrade my project". Part of the
  graph-agents-cli skills suite. Covers `graph-agents-cli create`,
  `scaffold enhance`, and `scaffold upgrade` with every flag, the valid
  runtime x checkpointer x target combinations, prototype semantics, the
  registry default, the authentic-baseline rule for upgrade, and the files
  upgrade never touches. Do NOT use for writing agent code
  (graph-agents-cli-langgraph-code) or deployment operations
  (graph-agents-cli-deploy).
metadata:
  author: graph-agents-cli contributors
  license: Apache-2.0
  version: "0.1.0"
  requires:
    bins:
      - graph-agents-cli
    install: "uv tool install graph-agents-cli"
---

# Project scaffolding guide

> **Requires:** `graph-agents-cli` (`uv tool install graph-agents-cli`).
> [Install uv](https://docs.astral.sh/uv/getting-started/installation/index.md) first if needed.

Use `graph-agents-cli create`, `scaffold enhance`, and `scaffold upgrade` to create a LangGraph
agent project, add deployment and CD to an existing one, or move a project to a newer template.

---

## Prerequisite: clarify requirements (MANDATORY for new projects)

**Before scaffolding, load `/graph-agents-cli-workflow` and complete Phase 0** (or the project's
declared process). Ask what the agent does, which product API operations it needs, which model
provider (and what may leave the network), and whether they want a prototype or Kubernetes.

---

## Step 1: choose the architecture

| Choice | Flag |
|---|---|
| Framework | `--agent langgraph` (default and only bundled template); `local@<path>` or `<org>/<repo>/<path>@<ref>` for a remote template |
| Runtime | `--runtime fastapi` (default) or `--runtime langgraph-server` |
| Model provider | `--model-provider openai\|anthropic\|gemini\|openai-compatible` (+ `--model <name>`) |
| Persistence | `--checkpointer memory\|postgres` (deployed default; local dev always starts with `CHECKPOINTER=memory` in `.env`) |
| Deployment | `--deployment-target kubernetes` (default) or `none`; `--prototype` |
| Registry | `--registry <url/org>` (default `ghcr.io/<org>`) |
| CD mode | `--cd argocd\|helm-push\|skip` (default `skip`) |
| Auth | `--auth-policy shared-bearer` (default) or `product-session` |
| Product API boundary | `--product-policy <file>` seeds `product-policy.yaml` |
| Governing process | `--process <path>` writes `process:` to the manifest and the guidance file |

### Valid runtime x checkpointer x target combinations (enforced by `create`)

| runtime | checkpointer | target | Valid | Notes |
|---|---|---|---|---|
| fastapi | memory | none | yes | local dev under uvicorn; state lost on restart |
| fastapi | memory | kubernetes | **no** | refused; multi-replica and restarts lose state |
| fastapi | postgres | none | yes | local dev against a local or docker Postgres |
| fastapi | postgres | kubernetes | yes | **default for kubernetes** |
| langgraph-server | memory | none | yes | `langgraph dev` in-memory server only; not deployable |
| langgraph-server | memory | kubernetes | **no** | refused |
| langgraph-server | postgres | kubernetes | yes | chart adds Redis; the server owns persistence |
| langgraph-server | postgres | none | yes | `run` and `playground` use `langgraph dev` (in-memory) locally; `postgres` is only the recorded deployed default |

Further validation: `--cd` other than `skip` requires `--deployment-target kubernetes`;
`--deployment-target none` defaults `--checkpointer memory`; `--auth-policy product-session`
scaffolds the stub and writes `auth_policy_implemented: false` (deploy to staging/prod refuses
until the project flips it).

### Prototype semantics

`--prototype`: the deployment target defaults to `none` unless `--deployment-target` is given
explicitly (an explicit target wins), and `--cd` is forced to `skip`. No chart, no Argo
manifests, only `pr_checks.yaml` among the workflows. Add deployment later with `scaffold enhance`.

### Registry default

`--registry` omitted: `ghcr.io/<org>`, where `<org>` under `-y` is the owner of the git `origin`
remote when present, else `ghcr.io/CHANGE-ME` with a warning. Private registries (Harbor,
`registry:2`) are the same flag with a different URL. The value lands in chart values and the
workflows. The image pull secret is an operator prerequisite reported by `infra check`.

### `langgraph-server` caveats

Needs Postgres and Redis in the cluster (the chart enables the Redis subchart in
`values-dev.yaml` for this runtime and sets `LANGGRAPH_SERVER=1`, `DATABASE_URI`, `REDIS_URI`)
and the `langchain/langgraph-api:3.12` base image (mirrored into your registry on disconnected
networks). Licensing: the local `langgraph dev` server (langgraph-cli[inmem]) was verified to
start and serve `/chat`, `/health` and `/playground` with no `LANGSMITH_API_KEY` and no network;
whether the deployed `langgraph-api` image needs a LangSmith license key at startup is still
unverified, so the runtime stays excluded from the disconnected profile. Under the in-memory
`langgraph dev` a one-off `run` advertises `--thread-id` resume, but the thread is gone once the
temporary server stops; use `--start-server` to keep it. Choose `fastapi` unless the team wants
the native Assistants/Threads/Runs API.

---

## Step 2: create, enhance, or upgrade

### Create a new project

```bash
graph-agents-cli create <project-name> \
  --model-provider openai \
  --runtime fastapi \
  --deployment-target kubernetes --checkpointer postgres \
  --registry ghcr.io/my-org --cd argocd \
  --auth-policy shared-bearer \
  --agent-guidance-filename CLAUDE.md \
  -y
```

**Constraints:**

- Project name: 26 characters or fewer, lowercase letters, numbers, hyphens. It is also the Helm
  release name and the namespace prefix (`<name>-dev`, `<name>-staging`, `<name>-prod`).
- Do NOT `mkdir` the project directory first; `create` creates it (a pre-existing directory
  triggers enhance semantics).
- Pass `--agent-guidance-filename` for the coding agent in use: `GEMINI.md` (Gemini CLI,
  Antigravity), `CLAUDE.md` (Claude Code), `AGENTS.md` (Codex and others).
- `create` copies the runtime's bundled lock (`uv-fastapi.lock` or `uv-langgraph-server.lock`)
  to `uv.lock` and runs `uv sync`.
- Non-interactive by default: every parameter has a default (`--deployment-target kubernetes`,
  `--agent langgraph`, `--runtime fastapi`, `--model-provider openai`, `--cd skip`, ...); `-y`
  skips prompts; `-i` shows menus for a human at a terminal. An invalid combination is a
  `UsageError` (exit 2) with the table's reason.
- `create` also renders `.github/agent.env` for every kubernetes project (used only by the CD
  workflows) and `pr_checks.yaml` runs the eval gate with `MODEL_PROVIDER=${{ vars.MODEL_PROVIDER || 'fake' }}`,
  so CI passes without a provider key until the repository variable is set.

### Enhance an existing project

```bash
graph-agents-cli scaffold enhance . --deployment-target kubernetes --checkpointer postgres --registry ghcr.io/my-org
graph-agents-cli scaffold enhance . --cd argocd
graph-agents-cli scaffold enhance . --auth-policy product-session
```

Run from inside the project (the positional argument names a template to apply, not the project;
it is ignored when the manifest records one). Enhance renders the template for the new
parameters and applies the 3-way merge; a backup goes to
`~/.graph-agents-cli/backups/<project>_<timestamp>/` first. When the agent code is not in `app/`,
pass `--agent-directory <dir>`. `--product-policy` is refused by `enhance` (copy the file into the
project and set `product_api.policy_file` in the manifest instead). When the merge changes the
manifest (for example `enhance --cd argocd`), `graph-agents-cli-manifest.yaml` is rewritten in
block style and its comments are dropped; app files stay byte-identical. **Always ask before
choosing the CD mode or auth policy.**

### Upgrade a project

```bash
graph-agents-cli scaffold upgrade                 # current directory
graph-agents-cli scaffold upgrade <project-path>
graph-agents-cli scaffold upgrade --dry-run       # preview
graph-agents-cli scaffold upgrade -y              # apply non-conflicting changes (--auto-approve / --yes)
graph-agents-cli scaffold upgrade -i              # resolve conflicts interactively
graph-agents-cli scaffold upgrade --baseline current   # explicit, logged opt-out of the authentic baseline
```

**Authentic baseline or stop.** `upgrade` regenerates the old template with the exact prior CLI
version (`uvx graph-agents-cli@<old-version> scaffold create ...`). If that version cannot be
fetched and run (index unreachable, version absent, `uvx` missing), `upgrade` **stops with a
non-zero exit and no changes**, because an inauthentic baseline would misclassify files.
`--baseline current` compares against the current templates instead; it is an explicit opt-in,
logged, and the result summary is labelled as such. Do not pass it just to make the error go
away; tell the user why the baseline is unavailable.

**What upgrade never touches:**

- *agent code:* `app/agent.py`, `app/tools/**`, `app/policies/**`, `app/prompts/**`, `app/graph/**`
- *config:* `.env`, `.env.*`, `product-policy.yaml`, `deployment/helm/<name>/values-*.yaml`
  (the environment values), `deployment/argocd/**`, `tests/eval/datasets/**`,
  `tests/eval/eval_config.yaml`
- files the project added that exist in neither template snapshot

*Dependencies* (`pyproject.toml`, the manifest) are merged semantically. *Scaffolding* (everything
else: `values.yaml`, `templates/**`, `app/app_utils/**`, `app/fast_api_app.py`, the Dockerfile,
workflows) is 3-way compared and replaced only when the project has not modified it; modified
scaffolding produces a conflict to resolve.

### Reference files

| File | Contents |
|---|---|
| `references/flags.md` | Full flag tables for `create`, `scaffold enhance`, `scaffold upgrade` |

---

## Step 3: load the dev workflow

After scaffolding, load `/graph-agents-cli-workflow` (lifecycle and rules) and
`/graph-agents-cli-langgraph-code` (what to edit: `app/agent.py`, `app/tools/`, `app/policies/`).

`.env` is yours. Preserve everything else the template generated; it wires serving, the auth
adapter, the checkpointer, the product client, telemetry, and A2A.

Verify: `graph-agents-cli run "test prompt"` for a smoke test, then `graph-agents-cli eval run`
for behaviour. Do not write pytest tests that assert on model output.

---

## Scaffold as reference

To inspect what the CLI generates without touching the current project, scaffold into a temporary
directory:

```bash
graph-agents-cli create ref-project --output-dir /tmp --deployment-target kubernetes --cd argocd -y
```

Copy the files you need (Dockerfile, chart, workflows), then delete the reference project.

---

## Critical rules

- **NEVER skip requirements clarification**; complete Phase 0 (or the declared process) first.
- **NEVER change the model** in an existing project unless asked; `--model` on `create` is the
  only place you choose it, and only with the user's say-so.
- **NEVER `mkdir` before `create`.**
- **NEVER create a git repository or push without asking**; confirm name, visibility, and intent.
- **Always ask before choosing the CD mode**; `argocd` and `helm-push` need repository settings
  the CLI cannot create (see the GitHub-settings reference in `/graph-agents-cli-deploy`).
- **Respect the combination table**; do not work around a refusal by editing the manifest.
- **`--process` when the project has a governing process**; it makes the workflow skill defer.
- **Start with `--prototype`** for quick iteration; add deployment later with `enhance`.
- **NEVER hand-write the A2A surface**; it is built into the scaffolded app.
- **NEVER edit `product-policy.yaml` on your own**; it is owned by the product.

---

## Examples

**Prototype first**

> "Build me an agent that answers questions about our incidents."

1. Phase 0: purpose, product operations (`getIncident`, `listIncidents`, GET only), provider.
2. `graph-agents-cli create incident-helper --model-provider anthropic --prototype --product-policy ./policy.yaml --agent-guidance-filename CLAUDE.md -y`
3. Implement tools, smoke test, eval.
4. Later: `graph-agents-cli scaffold enhance . --deployment-target kubernetes --checkpointer postgres --registry ghcr.io/acme --cd argocd`.

**Disconnected cluster**

> "Everything must stay on our network."

`graph-agents-cli create ops-agent --model-provider openai-compatible --model qwen2.5:14b --runtime fastapi --deployment-target kubernetes --registry harbor.internal/agents --cd skip -y`,
then set `OPENAI_BASE_URL` and `JUDGE_BASE_URL` in `.env`, `TRACING_ENABLED=false` or OTLP
in-cluster, and `GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1`.

**Project with its own process**

`graph-agents-cli create fabric-agent --process agentic-template/workflow.md ...` writes
`process: agentic-template/workflow.md` to the manifest and the guidance file; the workflow skill
then follows that process's gates.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `create` refuses the combination | consult the table; pick `postgres` for kubernetes |
| `upgrade` stops: "authentic baseline unavailable" | the old CLI version could not be fetched; fix index access or use `--baseline current` knowingly |
| `enhance` misplaces files | pass `--agent-directory` matching where the code lives |
| `ghcr.io/CHANGE-ME` in values | pass `--registry` or set a git `origin` remote before `create` |
| `graph-agents-cli` not found | `/graph-agents-cli-workflow` -> Setup |

## Not covered by this skill

- Writing the graph, tools, policies: `/graph-agents-cli-langgraph-code`.
- `deploy`, `secrets`, `infra check`, GitHub settings: `/graph-agents-cli-deploy`.
- Eval datasets and the gate: `/graph-agents-cli-eval`.
- Tracing configuration: `/graph-agents-cli-observability`.
- Remote template authoring beyond the `--agent` spec forms.

## Migration note

Compared with google-agents-cli: `--deployment-target agent_runtime|cloud_run|gke` became
`kubernetes|none`; `--session-type` became `--checkpointer`; `--cicd-runner` became `--cd`;
`--region`, `--bq-analytics`, `--agent-gateway`, the `adk@` shortcut, and the Terraform output are
gone; `--runtime`, `--model-provider`, `--model`, `--registry`, `--auth-policy`,
`--product-policy`, and `--process` are new.

## Related skills

- `/graph-agents-cli-workflow`, `/graph-agents-cli-langgraph-code`, `/graph-agents-cli-eval`,
  `/graph-agents-cli-deploy`, `/graph-agents-cli-observability`
