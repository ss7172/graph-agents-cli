# Agent spec template

Write `.graph-agents-cli-spec.md` in the project's working directory. **If the project declares a
`process:` (manifest or guidance file), do not write this file as a substitute for that process's
documents; the process's approvals are the gate.** This template applies when no process is
declared.

```markdown
# Agent Spec

## Overview
The agent's purpose, who calls it, and how it works in two or three sentences.

## Example Use Cases
Concrete examples with expected inputs and outputs. These become the first eval cases.

## Tools Required
Each tool with its purpose. For product API tools: the HTTP method and operationId (or path) it
calls, and the credential (`forwarded-session`, `bearer`, or `none`). Every product operation
listed here must be allowed by `product-policy.yaml`.

## Model and Egress
Provider (`openai` | `anthropic` | `gemini` | `openai-compatible`) and model. State explicitly what
context may leave the network (prompts, tool results, retrieved data) and whether traces may be
exported (`TRACING_ENABLED`, `TRACE_CAPTURE`).

## Runtime and Persistence
`fastapi` (default) or `langgraph-server`; `memory` locally and `postgres` when deployed;
whether threads must survive restarts; whether any tool call requires a human-in-the-loop
interrupt.

## Authentication
`shared-bearer` (internal tools, dev) or `product-session` (validates the product's own session and
roles; conversation ownership enforced; read-across roles listed).

## Constraints and Safety Rules
Specific rules, not generic statements: what the agent must never do, which operations are denied,
what to refuse, how to handle missing data.

## Success Criteria
Measurable outcomes for evaluation: which `expect` checks each use case must satisfy, which judge
metrics apply with what threshold, and which of those are quality metrics with a `min_pass_rate`.

## Deployment Shape
Prototype first (`--prototype`) or Kubernetes; if Kubernetes: registry, CD mode
(`skip` | `helm-push` | `argocd`), environments, and who owns secrets.

## Assumptions
One line per decision you made for the user (data sources, auth method, model, schedule). The user
corrects these at review.
```

Optional sections for detailed specs: **Edge Cases to Handle**, **Graph Design** (nodes, edges,
subgraphs, interrupts), **Data Sources and Auth**, **Non-Functional Requirements** (latency,
token budgets), **Future Phases**.
