---
description: Build a LangGraph agent end to end by asking your coding agent, with the graph-agents-cli skills leading each step and stopping for your review.
---

# Tutorial: build with a coding agent

<p class="gac-lede">Describe the agent you want and let your coding agent build it. The
graph-agents-cli skills give it the whole lifecycle (spec, scaffold, code, policy,
evaluation, a local deploy) and make it stop for your decision at every gate.</p>

!!! tip "Prefer to type the commands?"
    [Tutorial: manual workflow](tutorial-manual.md) builds the same agent command by
    command. Reading it first shows you what your coding agent will run.

## What you will build

The same agent as the manual tutorial: it answers questions about orders from an orders
API and can put an order on hold. Reading is free; each hold waits until you approve the
exact call. It is evaluated with an enforceable gate and deployed to a local Kubernetes
cluster.

## Before you start

You run two commands yourself; the coding agent runs everything else.

```bash
uv tool install graph-agents-cli
graph-agents-cli setup
```

`setup` installs six skills into the coding agents it finds: Claude Code, Codex, Gemini
CLI, Cursor, Antigravity and others that read skills
([Installation & setup](installation.md) has the options). To check them, run
`graph-agents-cli info`: its "Installed skills" line counts them.

Then open your coding agent in an **empty directory**. Nothing else is needed to follow
along: the agent can work on the keyless fake model, and the deploy step needs Docker and
kind, like the manual tutorial.

## 1. Ask for the agent

Name graph-agents-cli, the job, the APIs and the rules in one request:

> *"Use graph-agents-cli to build an agent that answers questions about orders from our
> orders API (`GET /orders`) and can put an order on hold (`PATCH /orders/{order_id}`).
> Holding an order must wait for my approval. Use the fake model locally; when the evals
> pass, deploy it to my local kind cluster."*

The `graph-agents-cli-workflow` skill takes over. It is always active and names the phase
you are in: understand, scaffold, build, evaluate, deploy, observe.

## 2. Agree on a spec

Before it scaffolds or writes anything, the agent runs a short design dialogue, **one
question at a time**: which operations each API needs and with what credential, which
access you grant (read-only, read-write, or a list of methods; never assumed), which
writes need a human, which model provider sees your data, and how the agent is deployed
and authenticated.

It then writes `.graph-agents-cli-spec.md` from the skill's template and waits for your
approval. This is the first gate: no code before you approve the spec.

!!! example "Illustrative: the spec's API and safety sections"
    ```markdown
    ## Tools Required
    - list_orders: GET /orders (operationId listOrders) on the `orders` API, auth: bearer
    - hold_order: PATCH /orders/{order_id} (operationId updateOrder), auth: bearer

    ## Constraints and Safety Rules
    - Access: GET and HEAD, plus PATCH on updateOrder only.
    - Every write waits for the requester's approval.
    - Act only on an order id the user typed.
    ```

What you review: the purpose, the APIs and their access, the approval gates, the model
and what may leave your network, and the success criteria that become eval cases.

!!! note "Your project has its own process?"
    If the project's guidance file or manifest declares a `process:` (a document chain such
    as requirements, design and stories), the skill follows that process and its approvals
    instead of writing a spec.

## 3. Scaffold

The `graph-agents-cli-scaffold` skill maps the spec to flags and creates the project:

```bash
graph-agents-cli create my-agent --registry localhost/dev
cd my-agent
graph-agents-cli install
```

The skill recommends a prototype first (`--prototype`, no deployment files) unless you
want a cluster from the start. You asked for one, so the project keeps its Kubernetes
target.

## 4. Build

The `graph-agents-cli-langgraph-code` skill writes the agent code: tools that declare
their calls in `API_CALLS` and send them through `get_client`, never a generic "call any
URL" tool. Changes to `api-policy.yaml` are yours to approve. Among the commands you will
see it run ([the manual tutorial](tutorial-manual.md#6-allow-exactly-that-write) has the
full sequence):

```bash
graph-agents-cli api add orders --base-url-env ORDERS_API_BASE_URL \
  --auth bearer --token-env ORDERS_API_TOKEN --access read-only
graph-agents-cli lint
graph-agents-cli api allow orders updateOrder --method PATCH --path "/orders/{order_id}" --dry-run
graph-agents-cli api approval orders --methods POST,PATCH,DELETE --approvers requester
graph-agents-cli run "Place a hold for ORD-1018"
```

When `lint` refuses a call, it prints the `api` command that would allow it. The skill
**proposes** that command with its `--dry-run` diff and runs it only when you agree:
widening access is your decision and, in a real repository, a reviewed pull request.

When a run pauses on a gated call, the agent shows you the call and the
`graph-agents-cli approvals` commands. Deciding it is yours; the skill never approves a
call or loosens a gate on its own.

## 5. Evaluate

The `graph-agents-cli-eval` skill writes eval cases for the behaviour in the spec,
including how a human decides each gate, then runs the gate:

```bash
graph-agents-cli eval run
```

It reports the per-status counts and the exit code, fixes what fails, and runs again until
the gate is met. Expect several rounds with a real model. The exit code, not the agent's
impression, says whether the gate is met. See [Evaluation](../guides/evaluation.md).

## 6. Deploy

The `graph-agents-cli-deploy` skill checks the prerequisites and previews the deploy:

```bash
graph-agents-cli info
graph-agents-cli infra check --env dev
graph-agents-cli deploy --env dev --dry-run
```

Then it **asks you**. It deploys only with your explicit approval, and outside `dev` it
never accepts the kubeconfig's current context without showing it to you. In Argo CD mode
a production change is a pull request that a code owner merges, never the agent.

```bash
graph-agents-cli deploy --env dev
graph-agents-cli deploy --env dev --status
```

## 7. Observe

The `graph-agents-cli-observability` skill explains what you can see once it runs. Tracing
is off until you set `TRACING_ENABLED=true`, and by default traces carry structure and
timing, not prompts or tool data. See [Observability](../guides/observability.md).

## What you review, gate by gate

| Gate | The agent shows you | You decide |
|---|---|---|
| Spec | `.graph-agents-cli-spec.md` | Scope, APIs and access, approval gates, model and egress |
| Policy change | The `api` command and its diff (`--dry-run`) | Whether to widen access |
| Gated call | The concrete call: method, path, body | Approve or reject it |
| Eval | Per-status counts and the exit code | Whether the gate is good enough to ship |
| Deploy | The context, the dry run | Go or no go |

## Rules the skills follow

| Rule | What it means for you |
|---|---|
| Process deference | A `process:` your project declares wins over the skill's own gates |
| Spec before code | Nothing is scaffolded or written until you approve the spec. A request to build, or an instruction to proceed on its own, is not approval: with nobody to ask, the agent stops at a draft spec and ends with its open questions |
| Code preservation | The agent changes only the lines your request targets |
| Never change the model | `MODEL_PROVIDER` and `MODEL_NAME` stay as chosen unless you ask; they are also an egress decision |
| Human approval before deploy | No deploy, and no merge of a production pull request, without you |
| 3-strikes loop breaker | After the same error three times it stops, runs the underlying tool directly and reports |

## Which skill takes over when

| Phase | Skill |
|---|---|
| Every phase: lifecycle, rules, spec | [`graph-agents-cli-workflow`](../reference/skills.md#graph-agents-cli-workflow) |
| Create, enhance, upgrade a project | [`graph-agents-cli-scaffold`](../reference/skills.md#graph-agents-cli-scaffold) |
| Agent code, tools, the API client, auth | [`graph-agents-cli-langgraph-code`](../reference/skills.md#graph-agents-cli-langgraph-code) |
| Datasets, checks, judges, the gate | [`graph-agents-cli-eval`](../reference/skills.md#graph-agents-cli-eval) |
| Environments, secrets, rollouts, GitOps | [`graph-agents-cli-deploy`](../reference/skills.md#graph-agents-cli-deploy) |
| Tracing, logs, metrics, run records | [`graph-agents-cli-observability`](../reference/skills.md#graph-agents-cli-observability) |

## Troubleshooting

**The agent does not use the skills.** Run `graph-agents-cli info` and check the
"Installed skills" line. Install for your agent explicitly with
`graph-agents-cli setup --agent <name>`, or into the project folder with
`graph-agents-cli setup --workspace`, then restart the coding agent so it reads them.
Naming "graph-agents-cli" in the request helps the agent pick them.

**The agent skipped the evaluation.** Ask it to run `graph-agents-cli eval run` and show
the per-status counts and the exit code. In the workflow skill, the deploy phase starts
only once you agree the gate is met; a smoke test with `run` does not replace it.

**The agent is stuck in a loop.** The skill stops after the same error three times. If it
keeps retrying (new image tags, the same deploy again), tell it to stop and run the
underlying command directly: every `--help` ends with a `Source:` line naming the file that
implements the command, and `deploy --dry-run` prints the exact helm and kubectl calls.

**The skills seem out of date.** `graph-agents-cli update` moves the CLI and the skills to
the latest release together.

## Next steps

<div class="grid cards gac-cols-3" markdown>

-   :material-console-line:{ .lg } **[Tutorial: manual workflow](tutorial-manual.md)**

    The same agent, one command at a time, with every output.

-   :material-puzzle-outline:{ .lg } **[Skills reference](../reference/skills.md)**

    What each skill covers and the references it carries.

-   :material-account-check-outline:{ .lg } **[Human approval](../guides/approvals.md)**

    Who may decide a gated call, and how.

</div>
