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
  version: "0.2.0"
  requires:
    bins:
      - graph-agents-cli
    install: "uv tool install git+https://github.com/ss7172/graph-agents-cli@v0.2.0"
---

# Project scaffolding guide

> **Requires:** `graph-agents-cli` (`uv tool install git+https://github.com/ss7172/graph-agents-cli@v0.2.0`).
> [Install uv](https://docs.astral.sh/uv/getting-started/installation/index.md) first if needed.

Use `graph-agents-cli create`, `scaffold enhance`, and `scaffold upgrade` to create a LangGraph
agent project, add deployment and CD to an existing one, or move a project to a newer template.

---

## Prerequisite: clarify requirements (MANDATORY for new projects)

**Before scaffolding, load `/graph-agents-cli-workflow` and complete Phase 0** (or the project's
declared process). Ask what the agent does, which external API operations it needs, which model
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
| Auth | `--auth-policy shared-bearer` (default), `jwt` (per-user OIDC/JWT tokens) or `custom` (your own policy; fail-closed stub) |
| Outbound API boundary | `--api-policy <file>` seeds `api-policy.yaml` (validated first); or none now and `graph-agents-cli api add` later. Access is always the user's explicit choice: never assume a level |
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
`--deployment-target none` defaults `--checkpointer memory`; `--auth-policy custom`
scaffolds the stub and writes `auth_policy_implemented: false` (deploy to staging/prod refuses
until the project flips it); an `--api-policy` with an `auth: forward` API is refused under
`--runtime langgraph-server` (the server would persist the forwarded credentials). The retired
`--product-policy` and `--auth-policy product-session` are refused with a rename hint.

### Prototype semantics

`--prototype`: the deployment target defaults to `none` unless `--deployment-target` is given
explicitly (an explicit target wins), and `--cd` is forced to `skip`. No chart, no Argo
manifests, only `pr_checks.yaml` among the workflows. Add deployment later with `scaffold enhance`.

### Registry default

`--registry` omitted: `ghcr.io/<org>`, where `<org>` under `-y` is the owner of the git `origin`
remote when present, else `ghcr.io/CHANGE-ME` with a warning. `build` and `deploy` refuse the
placeholder (exit 3) until `graph-agents-cli scaffold enhance --registry <host>/<org>` replaces
it in the manifest (`create_params.registry`, read by `build` and `deploy`), the chart values
(`image.repository`) and `.github/agent.env` (`IMAGE_REPOSITORY`); for a local cluster any
valid name works, since the image is side-loaded. Private registries (Harbor, `registry:2`)
are the same flag with a different URL. The image pull secret is an operator prerequisite
reported by `infra check`.

### `langgraph-server` caveats

Needs Postgres and Redis in the cluster (the chart enables the Redis subchart in
`values-dev.yaml` for this runtime and sets `LANGGRAPH_SERVER=1`, `DATABASE_URI`, `REDIS_URI`)
and the `langchain/langgraph-api:0.14.4-py3.12` base image (its tag moves together with the
`langgraph-api` pin in `uv.lock`; the image build refuses a mismatch; mirror it into your registry
on disconnected networks). The server image disables LangGraph Server's unauthenticated meta
routes (`/docs`, `/openapi.json`, `/info`, `/metrics`). Licensing: the deployed image checks for a
LangGraph licence at startup (a LangSmith API key or a licence key, per LangChain's
documentation; add the variable to `secrets.keys`) and exits without one; the local
`langgraph dev` server (langgraph-cli[inmem]) needs none, so `run` and `playground` work
keyless. The runtime stays excluded from the disconnected profile. Thread ids must be UUIDs
under this runtime, and `DELETE /threads/{id}` is the server's own route. Under the in-memory
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
- `--agent-guidance-filename` defaults to `AGENTS.md` (read by Codex and most coding agents);
  pass `CLAUDE.md` (Claude Code) or `GEMINI.md` (Gemini CLI, Antigravity) when that agent is in use.
- `create` copies the runtime's bundled lock (`uv-fastapi.lock` or `uv-langgraph-server.lock`)
  to `uv.lock`; it installs nothing. Run `graph-agents-cli install` (`uv sync` from that lock)
  before `run`, `eval` or the project's tests.
- Non-interactive by default: every parameter has a default (`--deployment-target kubernetes`,
  `--agent langgraph`, `--runtime fastapi`, `--model-provider openai`, `--cd skip`, ...); `-y`
  skips prompts; `-i` shows menus for a human at a terminal. An invalid combination is a
  `UsageError` (exit 2) with the table's reason.
- `create` also renders `.github/agent.env` (read as data only by the workflows):
  `GRAPH_AGENTS_CLI_SPEC`, the pinned source CI installs the CLI from
  (`git+https://github.com/ss7172/graph-agents-cli@v<creating version>`, used as
  `uvx --from "$GRAPH_AGENTS_CLI_SPEC" graph-agents-cli ...`), plus the chart settings for
  kubernetes projects, and one `.github/CODEOWNERS` for every project. `pr_checks.yaml` runs the
  tests on the fake model and the eval gate on the project's real provider when its key is a
  repository secret (or the `MODEL_PROVIDER` / `MODEL_NAME` variables); without one the gate runs
  on the fake model with a warning that it is not a quality signal.
- `create --api-policy <file>` validates the policy first (exit 3 on errors), copies the OpenAPI
  specs it references into the project (a spec outside the policy's directory goes to
  `openapi/<api>/<file>` and the reference is rewritten), adds every `auth: bearer` API's
  `token_env` to `secrets.keys`, and renders `app/tools/example_api.py` with the first operation
  the first API allows, whatever its method (a `body` argument for POST, PUT and PATCH; none,
  with a note, when it allows nothing the example can make).
- `create` only seeds the policy. It evolves with the agent through `graph-agents-cli api`
  (`add`, `access`, `allow`, `deny`, `revoke`, `limits`, `remove`, `show`, `check`), which keeps
  the manifest (`api_policy`, `secrets.keys`), `.env.example` and the chart's `values.yaml` in
  step; see `/graph-agents-cli-langgraph-code` for the schema.
- After `create`, the printed "Get Started" is `cp .env.example .env`,
  `graph-agents-cli login --write-env`, `install`, `playground`, `eval run` (and `deploy --env
  dev` for kubernetes): the local server answers 503 until `.env` has the provider key and, under
  `shared-bearer`, an `API_KEY`. Under `jwt`, after `install`,
  `export GRAPH_AGENTS_CLI_API_KEY="$(graph-agents-cli auth dev-token --sub <user>)"` gives local
  runs a token (a dev key in `.env`, `APP_ENV=dev` only).

### Enhance an existing project

```bash
graph-agents-cli scaffold enhance . --deployment-target kubernetes --checkpointer postgres --registry ghcr.io/my-org
graph-agents-cli scaffold enhance . --cd argocd
graph-agents-cli scaffold enhance . --auth-policy custom
```

Run from inside the project (the positional argument names a template to apply, not the project;
it is ignored when the manifest records one). Enhance renders the template for the new
parameters and applies the 3-way merge; a backup goes to
`~/.graph-agents-cli/backups/<dir>_<project id>_<timestamp>/` first (private, the newest 5 per
project kept). When the agent code is not in `app/`, pass `--agent-directory <dir>`.
`--api-policy` is refused by `enhance` (exit 2): change the policy with `graph-agents-cli api`
instead. When the merge changes
the manifest (for example `enhance --cd argocd`), `graph-agents-cli-manifest.yaml` is rewritten in
block style and its comments are dropped; app files stay byte-identical. **Always ask before
choosing the CD mode or auth policy.**

`--runtime` and `--model-provider` changes are reconciled everywhere they matter, so the result
matches a fresh `create` for the affected files: it prints "Recomputed for the new settings"
(runtime, provider, model, `secrets.keys` added and removed; keys you added are kept), updates
untouched `.env.example`, values files and Argo CD Applications, and merges the change into
edited ones (the chart's `values.yaml` and `.github/agent.env` key by key, keeping the keys you
changed and listing them). It ends with a numbered **Left for you** list. Items marked
`(required)` (a chart key still on the old runtime or model, an edited `Dockerfile`, whose new
version is written beside it as `Dockerfile.new`, dependency changes uv could not write) make
`enhance` exit 1: do them before building or deploying. A provider change keeps the model only
when it was the old provider's default; a model chosen for the old provider is refused (exit 2):
pass `--model` as well. After a runtime change run `graph-agents-cli install` to update
`uv.lock`. Exit codes: 0 applied, 1 required steps left, 2 usage error (or `uvx` missing for a
version-locked project), 3 configuration error.

### Upgrade a project

```bash
graph-agents-cli scaffold upgrade                 # current directory
graph-agents-cli scaffold upgrade <project-path>
graph-agents-cli scaffold upgrade --dry-run       # preview
graph-agents-cli scaffold upgrade -y              # apply non-conflicting changes (--auto-approve / --yes)
graph-agents-cli scaffold upgrade -i              # resolve conflicts interactively
graph-agents-cli scaffold upgrade --baseline-ref <ref> --dry-run   # name the build that created the project
graph-agents-cli scaffold upgrade --baseline current   # explicit, logged opt-out of the authentic baseline
```

**Authentic baseline or stop.** `upgrade` regenerates the old template with the exact CLI build
that created the project. The manifest names it: `cli_version` (a release, rebuilt with
`uvx --from git+https://github.com/ss7172/graph-agents-cli@v<old-version> graph-agents-cli scaffold create ...`,
or `GRAPH_AGENTS_CLI_INSTALL_SPEC` with `{version}` filled in) and `cli_build` (written by
`create`, `enhance` and `upgrade`: the build id `graph-agents-cli --version` prints, its commit,
and `template_digest`, a digest of what that build renders for the recorded settings). A build
between two releases (id `0.2.0+g<commit>`) is rebuilt from its commit in the repository. If the
build cannot be fetched and run (source unreachable, ref absent, `uvx` missing, or an
install-spec override without `{version}`, which would install some other build), `upgrade`
**stops with no changes** (exit 2 when `uvx` is missing or failed; exit 3 when the manifest's
`cli_version` is missing or not a release, the override lacks `{version}`, the recorded build
had uncommitted changes, or a build between releases is recorded while an override is set:
`{version}` names releases only), because an inauthentic baseline would misclassify files.
`--baseline current` compares against the current templates instead; it is an explicit opt-in,
logged, and the result is labelled as such. It cannot tell the user's edits from template
changes since the old version: every file listed under "Will preserve (differs from the current
template)" that the user did not edit keeps its old content, and dependency changes are not
merged. Do not pass it just to make the error go away; tell the user why the baseline is
unavailable.

**Same version, other build.** A project whose `cli_build` names another build of the running
version is upgraded from that build, unless both render the same files for its settings (same
`template_digest`: "already at version"). A manifest without `cli_build` (made before builds were
recorded, for example by a pre-release 0.2.0 build) is compared by version only: `upgrade` says
"already at version" and prints how to name the build. Name it with `--baseline-ref`, which
wins over the manifest: a commit or tag of the repository (`d99c816`, `v0.1.0`),
`<clone>@<commit>` for a local clone (the commit is looked up there first), a path to a checkout
or wheel (rebuilt, never a stale uv cache), or a full install spec
(`git+https://<mirror>/graph-agents-cli@<commit>`). The baseline must render the manifest's
`cli_version` (exit 3 otherwise); a different recorded commit is a warning. Without a known
commit, `git -C <clone> log -1 --format=%H --before=<generated_at from the manifest>` gives a
first candidate (the newest commit before the project was generated); the build may be older
(a checkout behind its branch, or a stale uv build). Check it with `--dry-run`: with the right
build only files the user edited are listed under "Will preserve" or as conflicts; many
untouched scaffolding files there mean the wrong build. After the upgrade the manifest records
the running build. Tags on the remote are the owner's to create; until `v<version>` exists there,
`--baseline-ref <clone>@<commit>` is the way to name any build.

A project created with 0.1.0 is upgraded against the `v0.1.0` tag (commit `fc3f2f9`). Never
use `--baseline current` for it: nearly every scaffolding file changed in 0.2.0, so the project
would keep 0.1.0's `app_utils`, chart and workflows, its tests would fail to import and `/chat`
would answer 500. If the tag cannot be fetched, name the commit in a clone that holds it:
`graph-agents-cli scaffold upgrade --baseline-ref <clone>@fc3f2f9`. Manual steps follow: see
the CHANGELOG's "Upgrading a project created with 0.1.0" (a new `app/policies/__init__.py` and
`app/agent.py`, `API_CALLS` instead of `PRODUCT_CALLS`, `secretOptional: true` in
`values-dev.yaml`). Outside a project `upgrade` exits 3.

**What upgrade never touches:**

- *agent code:* `app/agent.py`, `app/tools/**`, `app/policies/**`, `app/prompts/**`, `app/graph/**`
- *config:* `.env`, `.env.*`, `api-policy.yaml`, `deployment/helm/<name>/values-*.yaml`
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
adapter, the checkpointer, the API client, telemetry, and A2A.

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
- **NEVER change `api-policy.yaml` on your own**; it is the project's reviewed security boundary.
  When the user asks, use `graph-agents-cli api ...` with `--dry-run` first and show the diff;
  ask which access (read-only, read-write, custom methods) rather than choosing one.

---

## Examples

**Prototype first**

> "Build me an agent that answers questions about our incidents."

1. Phase 0: purpose, API operations and the access the user chooses for them (here
   `listIncidents`, `getIncident` and `acknowledgeIncident`), provider.
2. `graph-agents-cli create incident-helper --model-provider anthropic --prototype --agent-guidance-filename CLAUDE.md -y`
3. `graph-agents-cli api add incidents --base-url-env INCIDENTS_API_BASE_URL --auth bearer --token-env INCIDENTS_API_TOKEN --access custom --methods GET,POST`,
   then `api allow incidents listIncidents`, `api allow incidents getIncident`,
   `api allow incidents acknowledgeIncident` (without a spec, add each one's
   `--method M --path P` so the entry pins the endpoint, not only the label).
4. Implement tools, smoke test, eval.
5. Later: `graph-agents-cli scaffold enhance . --deployment-target kubernetes --checkpointer postgres --registry ghcr.io/acme --cd argocd`.

**Disconnected cluster**

> "Everything must stay on our network."

`graph-agents-cli create ops-agent --model-provider openai-compatible --model qwen2.5:14b --runtime fastapi --deployment-target kubernetes --registry harbor.internal/agents --cd skip -y`,
then set `OPENAI_BASE_URL` and `JUDGE_BASE_URL` in `.env`, `TRACING_ENABLED=false` or OTLP
in-cluster, and `GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1`.

**Project with its own process**

`graph-agents-cli create claims-agent --process docs/delivery-process.md ...` writes
`process: docs/delivery-process.md` to the manifest and the guidance file; the workflow skill
then follows that process's gates.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `create` refuses the combination | consult the table; pick `postgres` for kubernetes |
| `upgrade` exits 2: "Could not build the baseline" | the old CLI build could not be fetched (no `vX` tag on the remote, a commit that was never pushed, no network, no `uvx`); fix access, or name the build with `--baseline-ref <clone>@<commit>` (or point `GRAPH_AGENTS_CLI_INSTALL_SPEC`, with `{version}`, at a mirror that has the tag). `--baseline current` only knowingly, never for a 0.1.0 project |
| `upgrade` says "already at version X" for a project an earlier build of X created | its manifest records no `cli_build` (made before builds were recorded): name that build with `--baseline-ref` (the message shows the forms and how to find the commit) |
| `enhance` misplaces files | pass `--agent-directory` matching where the code lives |
| `ghcr.io/CHANGE-ME` in values | pass `--registry` or set a git `origin` remote before `create`; afterwards `graph-agents-cli scaffold enhance --registry <host>/<org>` sets it in the manifest, the chart values and `.github/agent.env`. `build` and `deploy` refuse the placeholder (exit 3) until then |
| `enhance` exits 1: "item(s) marked (required) above must be done by hand" | do each `(required)` step in the "Left for you" list (for example merge `Dockerfile.new` into your `Dockerfile`, or set the chart key it names) |
| `enhance` exits 2: "--model-provider X changes the provider, but the recorded model ..." | the model was chosen for the old provider: pass `--model <name>` for the new one |
| any command exits 3: "This project uses the retired product API policy" | follow the printed steps (rename to `api-policy.yaml`, `apis:` with `allowed_methods`, `API_CALLS`) |
| `create` exits 3: "GRAPH_AGENTS_CLI_INSTALL_SPEC contains ..." | the override has whitespace or a control character; fix or unset it |
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
`--api-policy`, and `--process` are new.

## Related skills

- `/graph-agents-cli-workflow`, `/graph-agents-cli-langgraph-code`, `/graph-agents-cli-eval`,
  `/graph-agents-cli-deploy`, `/graph-agents-cli-observability`
