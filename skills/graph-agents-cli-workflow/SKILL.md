---
name: graph-agents-cli-workflow
description: >
  This skill should be used when the user wants to "develop an agent",
  "build an agent with LangGraph", "build a LangGraph agent", "run the agent
  locally", "debug agent code", "test an agent", "evaluate an agent",
  "deploy an agent to Kubernetes", "monitor an agent", or needs the
  graph-agents-cli development lifecycle and coding guidelines.
  Entrypoint for building LangGraph agents with graph-agents-cli.
  Always active: provides the full workflow (understand, scaffold, build,
  evaluate, deploy, observe), process deference to a project's declared
  process, the spec-before-code gate, code preservation rules, the
  never-change-the-model rule, human approval before deploy, and the
  3-strikes loop breaker.
metadata:
  author: graph-agents-cli contributors
  license: Apache-2.0
  version: "0.1.0"
  requires:
    bins:
      - graph-agents-cli
    install: "uv tool install git+https://github.com/ss7172/graph-agents-cli"
---

# Agent Development Workflow and Guidelines

**graph-agents-cli** is a CLI and skills toolkit for building, evaluating, and deploying
[LangGraph](https://langchain-ai.github.io/langgraph/) agents on self-hosted Kubernetes. It works
with any coding agent (Claude Code, Codex, Gemini CLI, Cursor, Antigravity, others). The agent's
model is a scaffold-time and runtime choice among OpenAI, Anthropic, Gemini (AI Studio API key),
and any OpenAI-compatible endpoint (Ollama, vLLM, TGI, OpenRouter). Install with
`uv tool install git+https://github.com/ss7172/graph-agents-cli` and `graph-agents-cli setup`.

> **Before writing agent code, make sure a scaffolded project exists (see Phase 1).** Skipping the
> scaffold loses the chat API, the auth policy adapter, the eval gate, the Helm chart, and the
> CI workflows the template wires up.

> Requires: graph-agents-cli ~= 0.1.0. Check with `graph-agents-cli --version` or
> `graph-agents-cli info`. [Install uv](https://docs.astral.sh/uv/getting-started/installation/index.md)
> first if needed.

## Session continuity and skill cross-references

Re-read the relevant skill **before** each phase, not after you have started and hit a problem.
Context compaction may have dropped earlier skill content. If skills are missing, run
`graph-agents-cli setup` to install them.

| Phase | Skill | When to load |
|-------|-------|--------------|
| 0 - Understand | this skill, `references/brainstorming.md` | Read the project's process document if one is declared (see *Process deference*), else `.graph-agents-cli-spec.md` if present, else clarify goals with the user |
| 1 - Scaffold | `/graph-agents-cli-scaffold` | Before creating, enhancing, or upgrading a project |
| 2 - Build | `/graph-agents-cli-langgraph-code` | Before writing agent code: graph, tools, checkpointer, streaming, interrupts, auth policy, API client |
| 3 - Evaluate | `/graph-agents-cli-eval` | Before running any eval: dataset schema, expect checks, judge metrics, the gate rule and exit codes |
| 4 - Deploy | `/graph-agents-cli-deploy` | Before deploying: modes, environments, secrets, GitOps PR flow, GitHub settings, `infra check` |
| 5 - Observe | `/graph-agents-cli-observability` | After deploying: tracing opt-in, capture policy, LangSmith or OTLP, run records |

---

## Setup

If `graph-agents-cli` is not installed:

```bash
uv tool install git+https://github.com/ss7172/graph-agents-cli   # or a pinned tag: ...@v<version>
graph-agents-cli setup          # installs the six skills into detected coding agents
```

`uv` missing: follow the [official installation guide](https://docs.astral.sh/uv/getting-started/installation/index.md).

Users name things inconsistently ("Agent Server", "GitOps", "air-gapped", "Studio"). Map user terms
to CLI values with `references/terminology.md`.

---

## Process deference (read this before Phase 0)

A consuming project may govern agent work through its **own process** (for example a document
chain such as BRD -> PRD -> TRD/ADRs -> epics/stories -> acceptance cases). graph-agents-cli
records that in two places:

- the project manifest `graph-agents-cli-manifest.yaml`, key `process:` (a path to the governing
  process document, or `null`); this is what `info` and `scaffold upgrade` read;
- the project guidance file (`AGENTS.md` by default, or `CLAUDE.md` / `GEMINI.md`), which renders the same
  value; this is what you, the coding agent, read.

**Rule.** If the guidance file or the manifest declares `process:`:

1. Read the named process document first and follow **its** gates, roles, and approval sequence
   for everything in this skill (design, scaffolding, coding, evaluation, deployment).
2. Treat this skill's generic spec-before-code gate (`.graph-agents-cli-spec.md`) as **satisfied
   only by that process's own approvals**. Do not write a `.graph-agents-cli-spec.md` as a
   substitute for the process's documents, and never treat an approved spec as permission the
   process has not given.
3. Where the process is silent, the rules below still apply (code preservation, never change the
   model, human approval before deploy, the eval gate, the 3-strikes breaker).
4. Choices that the process owns stay with the process: the outbound API policy
   (`api-policy.yaml`), which credentials and roles the auth policy validates, data-egress and
   trace-capture decisions, and whether the project may be deployed at all.

If no process is declared, the generic gate in Phase 0 applies.

---

## Phase 0: Understand

Before scaffolding or writing anything, understand what you are building through a **design
dialogue**, not a checklist. Load `references/brainstorming.md` and follow it: ask **one question
at a time**, propose 2-3 architecture approaches for non-trivial agents, and validate the design
before any scaffolding.

If `.graph-agents-cli-spec.md` exists in the project directory (and no process is declared), read
it; it is your primary source of truth. Otherwise:

**Do NOT proceed to scaffolding or coding until the user approves the spec** (or, under a declared
process, until that process's approvals exist). Do not assume, research, or fill in the blanks on
your own; the user's intent drives everything.

**Scale the ceremony to complexity:** a trivial agent (single tool, fixed persona) needs a couple
of questions, a 2-3 sentence spec, and one approval; a complex agent (multi-step graph, external API
access, per-user identity and roles, safety-critical) gets the full treatment in `references/brainstorming.md`.

**Topics to cover** (one question at a time):

1. **What problem will the agent solve?** Core purpose, capabilities, who calls it.
2. **External APIs or data sources?** Which API operations the agent may call, and with what
   credential (none, a service token, or the caller's own). Every outbound API is declared in
   `api-policy.yaml` (see `/graph-agents-cli-langgraph-code`); the agent never gets a generic
   "call any endpoint" tool.
3. **Safety constraints?** What the agent must NOT do; which tool calls need human approval
   (LangGraph interrupts); what may leave the network (model egress, traces).
4. **Model provider?** `openai`, `anthropic`, `gemini`, or `openai-compatible` (on-network servers
   such as vLLM, Ollama, TGI). Selecting a hosted provider sends prompts, tool results, and assembled
   context to that provider; the user must decide that explicitly.
5. **Deployment preference?** Prototype first (recommended, `--prototype`, no deployment files) or
   Kubernetes from the start. If Kubernetes: runtime `fastapi` (default) or `langgraph-server`;
   CD mode `skip`, `helm-push`, or `argocd`; registry; auth policy `shared-bearer`, `jwt` or
   `custom`.

**Ask based on context:**

- Persistent conversations across restarts or replicas: `--checkpointer postgres` (the default
  for Kubernetes); local development uses `CHECKPOINTER=memory` from `.env` and needs no database.
- Callers are individual users with an OIDC identity provider: `--auth-policy jwt` (per-user
  principals from verified tokens). Callers already carry another credential (for example an
  existing application's session cookie): `--auth-policy custom`; the template ships the
  interface and a stub that fails closed until the project implements it.
- Disconnected or air-gapped cluster: the **disconnected profile** (`openai-compatible` model and
  judge on-network, runtime `fastapi`, tracing off or OTLP in-cluster, `cd: skip` unless an
  on-network GitHub Enterprise Server exists). See `/graph-agents-cli-deploy`.
- Other agents must call this one: A2A is built into every scaffolded app; scaffold normally.
- CI/CD wanted: does a GitHub repository exist? Creating one (public or private) needs the user's
  say-so.

Once the design is agreed, write `.graph-agents-cli-spec.md` from `references/spec-template.md`,
self-review it, then get the user's approval. `/graph-agents-cli-scaffold` maps the choices to flags.

## Phase 1: Scaffold

Check whether a project already exists: run `graph-agents-cli info` from the project root. If it
was created or enhanced by graph-agents-cli, skip this phase.

Otherwise scaffold **before writing any code**:

- **No project yet:** `graph-agents-cli create <name> ...` (alias of `scaffold create`)
- **Existing code to import:** `graph-agents-cli scaffold enhance .`
- **Older scaffold:** `graph-agents-cli scaffold upgrade`

Use `/graph-agents-cli-scaffold` for every flag, the valid runtime x checkpointer x target
combinations, prototype semantics, and what `upgrade` never touches.

## Phase 2: Build and implement

1. Read the project's guidance file for the agent directory (default `app/`).
2. Edit only agent code: `app/agent.py` (exports `graph`, an unbound compiled `StateGraph`),
   `app/tools/**`, `app/policies/**`, and the reserved `app/prompts/**` and `app/graph/**`
   directories you may create. `upgrade` never modifies these.
3. **Smoke test:** `graph-agents-cli run "your prompt"` starts the local server for the project's
   runtime, sends one chat message over the same `/chat` SSE API the product will call, and prints
   the reply. Use `--start-server` when iterating on several prompts, and `--thread-id` to continue
   a thread. `-v` prints every SSE event (tool calls, results, usage).
4. Interactive testing: `graph-agents-cli playground` (the selected application with reload and
   the `/playground` dev page). `playground --graph` opens LangGraph Studio through `langgraph dev`;
   it bypasses the auth policy and the chat API, so use it for graph debugging only.
5. `graph-agents-cli lint` runs ruff and the API-policy check: every module under
   `app/tools/` declares a literal `API_CALLS` (and `TOOLS`), which the CLI reads statically
   with `ast` and checks against `api-policy.yaml` (and the API's OpenAPI spec when it names one).

Load `/graph-agents-cli-langgraph-code` for `create_agent` versus explicit `StateGraph`, tools and
their `API_CALLS` declaration, checkpointers and `thread_id`, streaming events, interrupts
(a LangGraph pattern; resume over `/chat` is not implemented in this milestone), subgraphs,
`init_chat_model` provider switching, the deterministic `fake` provider for tests, the auth policy
adapter, the API client, and telemetry.

> **Smoke-test only here; do not write behavioural unit tests.** Model output is
> non-deterministic; behavioural checks belong in eval (Phase 3), not in `pytest`. Unit tests may
> cover tools, the API client, and policy code with the `fake` provider.

## Phase 3: Evaluate

**This is the most important phase.** Evaluation validates agent behaviour end to end, and the
gate is enforceable: `eval run` exits non-zero when the gate is not met, and `pr_checks` treats
that as a failed check.

**MANDATORY:** load `/graph-agents-cli-eval` before running evaluation. It has the dataset schema,
the expect checks, the judge metrics, the gate rule, and the exit codes.

**Unit tests versus `graph-agents-cli eval`:**

- **Unit tests** (`uv run pytest`) test code correctness: imports, tool functions, policy
  enforcement, the API client, with the `fake` model provider. They never test whether the
  agent behaves well.
- **`graph-agents-cli eval run`** tests agent behaviour: response content, tool trajectories,
  latency, tokens, and subjective quality through a model judge.
- **`graph-agents-cli run "prompt"`** is a one-off smoke test during development.

**NEVER write unit tests that assert on model response content.** Put those checks in an eval case
(`expect.contains`, `expect.tool_calls`, a judge metric) instead.

1. Start small: 1-2 cases in `tests/eval/datasets/`.
2. `graph-agents-cli eval run` (chains `generate` and `grade`). For debugging use `eval generate`
   then `eval grade` on the traces file.
3. Discuss results with the user; paste the per-status counts and the exit code.
4. Fix issues; iterate on the core cases first, then add edge cases.
5. Repeat until `eval run` exits 0. The exit code is the gate; a passing run has no `failed`,
   `error`, or `missing` case and every quality metric meets its `min_pass_rate`.

Expect several iterations here.

## Phase 4: Deploy

Once the user agrees the eval gate is met:

1. `graph-agents-cli info` shows the deployment target, runtime, CD mode, registry, and auth policy.
2. Prototype (`deployment_target: none`)? Add deployment first:
   `graph-agents-cli scaffold enhance . --deployment-target kubernetes [--cd ...]`.
3. `graph-agents-cli infra check --env <env>` reports the cluster and repository prerequisites for
   the project's mode (read-only, never creates anything).
4. Secrets: `graph-agents-cli secrets apply --env <env>` in direct modes; in `helm-push` and
   `argocd` modes the named owner provisions them from a workstation, never CI.
5. `graph-agents-cli deploy --env <env>`; what that does depends on the CD mode (direct helm,
   helm from a CI runner, or a pull request that Argo CD reconciles). `--dry-run` prints every
   command and the rendered manifests without running them.

**IMPORTANT: never deploy without explicit human approval.** In `argocd` mode a production change
is a PR that a code owner merges; the merge is the single gate and you never merge it yourself.
`/graph-agents-cli-deploy` has the mode table, environments, rotation, and the required GitHub
settings.

## Phase 5: Observe

Tracing is **off** unless `TRACING_ENABLED=true`; `TRACE_CAPTURE` defaults to `metadata` (no
prompt or tool text). See `/graph-agents-cli-observability` for LangSmith versus OTLP, the capture
policy, hashed principal ids, and run records.

---

# Operational guidelines for coding agents

## Common shortcuts to resist

| Shortcut | Why it fails |
|----------|-------------|
| "The request is clear enough, no need to clarify" | You are guessing at requirements. Phase 0 (or the project's process) exists to confirm intent before scaffolding. |
| "The project has a process document, but a quick spec is faster" | The process owns the gates. A generic spec cannot stand in for the approvals it requires. |
| "It answered correctly in `run`, so eval is unnecessary" | One prompt is not a test suite. The eval gate catches regressions, tool trajectory errors, and edge cases. |
| "I'll switch to a newer/better model" | The provider and model were chosen deliberately and written to `.env` and the manifest. Changing them without being asked violates code preservation and is an egress decision the user owns. |
| "I'll add a generic HTTP tool so the agent can call whatever it needs" | `app_utils.api_client` is the only path to external APIs and it enforces `api-policy.yaml`. A generic tool bypasses the policy the team reviewed. |
| "I'll `helm upgrade` / `kubectl apply` directly, it's quicker" | In `argocd` mode the cluster follows `main`; direct changes are drift that self-heal reverts, and they skip the production gate. |
| "I can skip the scaffold and set up manually" | Manual setup misses the chat API, auth adapter, eval gate, chart, and workflows. Use `create` even for experiments (`--prototype`). |

## Principle 1: code preservation and isolation

Change only the lines the user's request targets; preserve everything else (code, configuration
values such as `MODEL_PROVIDER`, `MODEL_NAME`, `CHECKPOINTER`, comments, formatting).

**Before finalizing any edit, verify:**

1. **Target identification:** the exact lines to change, from the user's explicit instruction only.
2. **Preservation check:** everything outside the target is identical.

Example. User: "Change the system prompt to a recipe suggester."

```python
# VIOLATION: the model was not requested to change
graph = create_agent(
    model=init_chat_model("openai:gpt-5"),  # replaced get_model() -- NOT asked
    tools=TOOLS,
    system_prompt="You are a recipe suggester.",
)

# COMPLIANT
graph = create_agent(
    model=get_model(),                      # PRESERVED: reads MODEL_PROVIDER / MODEL_NAME
    tools=TOOLS,                            # PRESERVED
    system_prompt="You are a recipe suggester.",   # the direct target
)
```

## Principle 2: execution best practices

- **Model selection (CRITICAL):**
  - **NEVER change the model or provider unless explicitly asked.** The model is configured by
    `MODEL_PROVIDER` and `MODEL_NAME` in `.env` and the chart, never in code.
  - New projects get the provider default that `create` records. Do not hard-code model names from
    memory; your training data is likely out of date. If the user wants a different model, change
    `MODEL_NAME` in `.env` (and the manifest through `scaffold enhance` when relevant), not `agent.py`.
- **Running Python:** always through `uv` (`uv run python ...`, `uv run pytest`). Run
  `graph-agents-cli install` (which is `uv sync`) after dependency changes.
- **3-strikes loop breaker:**
  - **Stop immediately** if you see the same error three times in a row.
  - Red flags: retrying the same `deploy`, incrementing image tags v5 -> v6 -> v7, "I'll try one
    more time" repeatedly, re-running `eval` hoping the judge scores differently.
  - When stuck: run the underlying command directly (`references/internals.md` says which:
    uvicorn, `langgraph dev`, `docker build`, `helm template`, `kubectl`, `gh`), read its output,
    and report to the user instead of retrying.
- **Troubleshooting:**
  - `/graph-agents-cli-langgraph-code` first; it covers the template contract and the patterns.
  - `graph-agents-cli <command> --help` ends with a `Source:` line pointing at the file that
    implements the command. Read it. `graph-agents-cli info` prints the CLI install path.
  - For LangGraph and LangChain API questions, fetch the upstream docs rather than guessing.

### Systematic debugging

1. **Reproduce:** run the exact command that failed; save the full output.
2. **Localize:** agent code, a tool, the policy, configuration, or the environment? Use
   `graph-agents-cli run "prompt" -v` to see every SSE event; use `playground --graph` for graph
   state; use `deploy --dry-run` for rendered manifests.
3. **Fix one thing** at a time.
4. **Verify** by re-running the reproduction.
5. **Guard** with an eval case (behaviour) or a unit test (code).

**Stop-the-line rule:** if a change breaks something that worked, fix the regression before
continuing feature work.

- **Environment variables:** `.env`, `.env.<env>`, and the manifest are essential configuration;
  never remove or rewrite entries unless the user asks. Never commit `.env` files. Secrets reach
  the cluster only through `secrets apply` from the allow-listed keys in the manifest
  (`secrets.keys`), never through values files or CI.

---

## Using a temporary scaffold as reference

When you need specific files (Dockerfile, chart, workflows) without touching the current project,
create a reference project in a temporary directory with `/graph-agents-cli-scaffold` and copy what
you need.

---

## Not covered by this skill

- LangGraph and LangChain API details: `/graph-agents-cli-langgraph-code`.
- Scaffold flags and the combination table: `/graph-agents-cli-scaffold`.
- Dataset schema, judge configuration, gate exit codes: `/graph-agents-cli-eval`.
- Deployment modes, secrets, GitOps, GitHub settings, `infra check`: `/graph-agents-cli-deploy`.
- Tracing destinations and capture policy: `/graph-agents-cli-observability`.
- Any cloud-managed agent runtime, registry, or publishing catalog: graph-agents-cli has none.

## Migration note

graph-agents-cli is a fork of google-agents-cli (ADK on Google Cloud). ADK became LangGraph;
Agent Runtime, Cloud Run, and GKE became any Kubernetes cluster via Helm; Cloud Trace and BigQuery
analytics became LangSmith or OpenTelemetry behind an opt-in; the Gemini Enterprise `publish`
command was removed; gcloud authentication became provider keys plus a kubeconfig. If a user asks
for one of the old names, `references/terminology.md` maps it.

## Reference files

| File | Contents |
|------|----------|
| `references/commands.md` | Every command with its flags |
| `references/internals.md` | What each command runs under the hood (uvicorn, `langgraph dev`, docker, helm, kubectl, gh) |
| `references/terminology.md` | User terms to CLI values; migration mapping of old names |
| `references/extension.md` | Author an ad-hoc extension or adopt an existing one (override or add commands) |
| `references/spec-template.md` | `.graph-agents-cli-spec.md` template |
| `references/brainstorming.md` | Phase 0 design-dialogue playbook |

## Skills version

If skills seem outdated or incomplete, reinstall with `graph-agents-cli setup` (or
`graph-agents-cli update`). Set `GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1` on disconnected installs to
silence the update and skills-version checks.
