# Phase 0 brainstorming playbook

Turn the user's idea into an agreed design through a collaborative dialogue, *before* any
scaffolding or code. Adapt the depth to the agent's complexity.

## Process deference first

Before anything else, check the project guidance file (`AGENTS.md`, `CLAUDE.md`, `GEMINI.md`) and
`graph-agents-cli-manifest.yaml` for `process:`. If a process is declared, this playbook serves
that process's design stage; its documents and approvals replace the spec and the user-review gate
below. Do not run both.

## HARD GATE

Do NOT scaffold, run `graph-agents-cli create`, or write any code until the user has approved the
spec (or the declared process has granted its approvals). Reading `/graph-agents-cli-langgraph-code`
to name a matching pattern is exempt; it is design input, not implementation. This applies even to
"obvious" agents; unexamined assumptions cause the most wasted work.

## Scale to complexity

- **Trivial agent:** single tool or none, fixed persona, no external API, no per-user identity.
  A couple of adaptive questions, a 2-3 sentence spec, one approval.
- **Complex agent:** multi-step graph or subgraphs, external API access with a policy, per-user
  identity and roles, human-in-the-loop, safety-critical.
  Full treatment: adaptive Q&A across all topics, 2-3 approaches, sectioned design with approval
  per section, self-review, user-review gate.

When unsure, start light and escalate as complexity surfaces.

## One question at a time

- Ask a single question per message; let the answer shape the next. Two questions in one message
  is a batch. Ask the one that most shapes the design first (usually problem and scope before
  integrations).
- Ask at least one clarifying question before proposing approaches, and never present a full spec
  in your first reply. (Exceptions: a trivial agent, or a genuinely non-interactive run.)
- Prefer multiple-choice questions.
- Cover: problem, tools and the API operations they need plus credential, safety, model provider
  and egress, runtime and persistence, auth policy, deployment shape. Follow the user's lead
  rather than a script.
- YAGNI: prune features that do not serve the stated purpose.

## When you cannot ask

When you genuinely cannot get an answer (non-interactive run, a one-liner "just build it", or the
user defers a choice), make a concrete choice and **list it in the spec under `## Assumptions`**,
one line each, so the user can correct it. Always surface the axes users leave implicit: **data
sources, auth method, which model and what may leave the network, whether threads persist.**

Non-interactive does not mean skip the thinking. For non-trivial agents still record the
approaches you weighed and the one you chose, flag oversized scope, and route each capability to
the pattern in `/graph-agents-cli-langgraph-code` that implements it.

## Propose 2-3 approaches (non-trivial agents)

Present 2-3 architecture options with trade-offs and **end with one explicit recommendation**.
Typical axes:

- **`create_agent` (ReAct loop) versus an explicit `StateGraph`** with named nodes, conditional
  edges, and subgraphs. Start with `create_agent`; move to an explicit graph when the flow has
  fixed stages, branching, or a human approval step.
- **Tool and integration choices:** which API operations, with which methods and credential
  (none, a service token, or the caller's own); everything goes through the API client and must be
  allowed by `api-policy.yaml`. Ask which access each API gets (read-only, read-write, or a custom
  set of methods, then which operations are allowed or denied, and any per-run or per-minute
  limits); never assume a default. There is no generic HTTP tool.
- **Human-in-the-loop:** which tool calls pause for approval (`interrupt`), and how the calling
  application resumes the thread.
- **Persistence:** `memory` locally; `postgres` when deployed; `thread_id` is the continuity key.
- **Model and egress:** hosted provider versus on-network OpenAI-compatible server; tool-capable
  model required for the ReAct pattern.
- **Auth:** `shared-bearer` (one shared key) versus `jwt` (per-user OIDC tokens, conversation
  ownership) versus `custom` (the project's own policy, for example an existing session cookie).
- **Deployment shape:** prototype-first (recommended) versus Kubernetes with a CD mode.

## Present the design in sections

For complex agents, present the design in sections and get approval after each:

- **Graph:** nodes, edges, subgraphs, interrupts; `create_agent` or explicit.
- **Tools:** each tool's purpose, API operation, credential, and `API_CALLS` declaration.
- **Data flow and state:** inputs, state schema, what is checkpointed, what is stored in run
  records.
- **Safety and policy:** denied operations, refusal behaviour, capture policy, egress.
- **Success criteria:** expect checks and judge metrics per use case.

## Right-size the scope first

If the request spans multiple sub-systems (3+ specialist subgraphs, several integrations, distinct
domains), stop and flag it before designing. Recommend the smallest end-to-end slice that proves
the architecture and defer the rest under `## Future Phases` in the spec. This holds
non-interactively too.

## Write the spec

Write `.graph-agents-cli-spec.md` from `references/spec-template.md` in the **project's working
directory** (Phase 0 resumes by reading `./.graph-agents-cli-spec.md`). Name the path in your
approval message.

**Self-review before showing the user:**

1. **Placeholders:** any "TBD" or vague requirement? Fill it in.
2. **Consistency:** do sections contradict? Does the graph match the tools and use cases?
3. **Scope:** 3+ subgraphs or integrations? Did you carve out a first slice?
4. **Measurable success criteria:** each criterion is an `expect` check, a judge threshold, or a
   pass/fail eval, not "works well".
5. **Policy:** every API operation a tool needs is in the policy allow-list, and nothing the
   policy denies is assumed.
6. **Ambiguity:** could a requirement be read two ways? Pick one and make it explicit.

## User-review gate

> "Spec written to `.graph-agents-cli-spec.md`. Please review it and tell me if you want changes
> before we scaffold."

If they request changes, make them and re-run the self-review. Only once they approve do you
proceed to **Phase 1 (scaffold)**.
