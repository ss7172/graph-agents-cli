---
name: graph-agents-cli-observability
description: >
  This skill should be used when the user wants to "set up tracing",
  "enable LangSmith", "send traces to our collector", "monitor my agent",
  "debug production traffic", "see what the agent sent to the model", "log
  prompts", or needs guidance on observability for a graph-agents-cli
  project. Covers the TRACING_ENABLED opt-in, the TRACE_CAPTURE metadata
  versus full policy, LangSmith versus OTLP, the hashed principal id, run
  records, and what is never captured by default. Part of the
  graph-agents-cli skills suite. Do NOT use for deployment
  (graph-agents-cli-deploy) or agent code (graph-agents-cli-langgraph-code).
metadata:
  author: graph-agents-cli contributors
  license: Apache-2.0
  version: "0.1.0"
  requires:
    bins:
      - graph-agents-cli
    install: "uv tool install git+https://github.com/ss7172/graph-agents-cli"
---

# Observability guide

> **Tracing is off unless `TRACING_ENABLED=true`.** No exporter is configured and no LangSmith
> client is created otherwise. Setting `LANGSMITH_API_KEY` alone does not enable tracing.
> Enabling tracing to a hosted destination is an egress decision the user makes explicitly.

## Reference files

| File | Contents |
|---|---|
| `references/langsmith.md` | LangSmith destination: variables, self-hosted endpoint, projects, what the capture policy does to LangSmith runs, `eval submit` |
| `references/otel.md` | OTLP destination: LangChain OpenTelemetry instrumentation, collector configuration, span attributes, in-cluster collectors |

---

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `TRACING_ENABLED` | `false` | the opt-in; nothing is exported while false |
| `TRACE_CAPTURE` | `metadata` | `metadata` or `full`; applied identically to LangSmith, OTLP, and run records |
| `LANGSMITH_API_KEY` | (Secret) | when set with tracing enabled, traces go to LangSmith |
| `LANGSMITH_PROJECT` | project name | LangSmith project |
| `LANGSMITH_ENDPOINT` | LangSmith cloud | override for a self-hosted LangSmith |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | (chart) | OTLP fallback used when tracing is enabled and no LangSmith key is set |

Chart values: `tracing: { enabled, capture, otlpEndpoint, langsmith: { project } }` render these
into `env:`; the key comes from the Secret. Locally, `.env` sets `TRACING_ENABLED=false` and
`TRACE_CAPTURE=metadata`.

## Destination selection

```
TRACING_ENABLED=false            -> nothing
TRACING_ENABLED=true + LANGSMITH_API_KEY   -> LangSmith (LANGSMITH_PROJECT, LANGSMITH_ENDPOINT)
TRACING_ENABLED=true, no LangSmith key     -> OTLP to OTEL_EXPORTER_OTLP_ENDPOINT via LangChain OpenTelemetry instrumentation
```

The disconnected profile allows only the OTLP path to an in-cluster collector, or tracing off.

## Capture policy

| `TRACE_CAPTURE` | Captured | Never captured |
|---|---|---|
| `metadata` (default) | span structure and timing; model and provider names; token counts; tool **names**; error **types** and HTTP status codes; identifiers (`thread_id`, `run_id`, hashed principal id, agent version) | prompt text, completion text, tool arguments, tool results, error messages |
| `full` | everything in `metadata` plus prompts, completions, tool arguments, tool results, full error messages | |

Enabling `full` against a hosted destination sends user content off-network. The consuming
project must decide that explicitly (and publish a truthful privacy notice) before you set it. Do
not switch to `full` on your own to debug; ask, and prefer reproducing locally with
`graph-agents-cli run -v`, which prints the events to your terminal without exporting anything.

The same policy governs the chat API: `tool.call` events omit `args` for a caller who is not the
thread's owner under `metadata`.

## Hashed principal id

Traces, run records, and eval traces record `Principal.hashed_id()` (sha256 of the policy's
`Principal.id`, first 16 hex characters), never the raw id, under `metadata`. Audit and thread
ownership use the same identity, so a support engineer can correlate a trace with a conversation
without the trace revealing who the user is. Under `shared-bearer` every caller is `shared`.

## Run records

Besides traces, the app writes one run record per `/chat` call: run id, thread id, hashed
principal, model, token counts, latency, status; payload (prompt, completion, tool I/O) only under
`TRACE_CAPTURE=full`.

- `CHECKPOINTER=postgres`: durable rows in the agent-owned database (`runs` table, created at
  startup; no retention job in this release, so plan one).
- `CHECKPOINTER=memory`: in-process, served to the `/playground` page while the process lives,
  lost on restart. Local development needs no database.
- Under `langgraph-server`, the server's own Runs API is the durable record; the app's records
  follow the same capture policy.
- Evaluation never depends on run records; `eval generate` writes trace files.

## What is never captured by default

- Prompt and completion text, tool arguments and results, error messages (only under `full`).
- Raw principal ids, session cookies, session tokens, bearer keys (never, under any setting).
- External API payloads beyond what a tool returns into the trace (governed by `full`).
- Anything at all while `TRACING_ENABLED=false`.

## Procedure: enable tracing for an environment

1. Confirm the destination and capture level with the user (egress decision).
2. LangSmith: put `LANGSMITH_API_KEY` in `.env.<env>`, run `graph-agents-cli secrets apply --env
   <env>`; set `tracing.enabled=true`, `tracing.capture`, `tracing.langsmith.project` in
   `values-<env>.yaml`.
   OTLP: set `tracing.enabled=true` and `tracing.otlpEndpoint=http://<collector>:4318` in
   `values-<env>.yaml`; no key.
3. Deploy per the project's mode (`/graph-agents-cli-deploy`); in argocd mode the values change
   is a PR. After a Secret change alone, `deploy --restart --env <env>`.
4. Send one request with `graph-agents-cli run --url ... "hello"` and find the trace by
   `thread_id` / `run_id` from the `message.end` event.

Locally: set `TRACING_ENABLED=true` and either key or endpoint in `.env`; `playground` and `run`
pick it up.

## Troubleshooting

| Symptom | Fix |
|---|---|
| No traces appear | `TRACING_ENABLED` is not `true` (the key alone does nothing); check `GET /health` and the pod env |
| Traces in LangSmith but empty prompts | expected under `metadata`; `full` is an explicit decision |
| OTLP exporter connection refused | endpoint must be reachable from the pod; use the collector's Service DNS and port 4318 (HTTP) |
| Traces from `eval generate` mixed with production | use `LANGSMITH_PROJECT` per environment; eval traces are files unless tracing is on |
| Need to know who a trace belongs to | correlate `hashed_id` with the client application's session log; the raw id is never in the trace |

## Not covered by this skill

- Deploying the values or Secret changes: `/graph-agents-cli-deploy`.
- Writing nodes or tools that emit custom spans: `/graph-agents-cli-langgraph-code` (keep
  prompt text out of logs).
- The eval gate and results files: `/graph-agents-cli-eval`.
- Cluster-wide metrics, logging stacks, dashboards, alerting: platform tooling outside the CLI.
- Self-hosted LangSmith installation (point `LANGSMITH_ENDPOINT` at one that exists).

## Migration note

Compared with google-agents-cli: Cloud Trace, Cloud Logging, prompt-response logging to GCS and
BigQuery, and the BigQuery Agent Analytics plugin are gone, and tracing is no longer always on.
LangSmith or OTLP behind `TRACING_ENABLED` with the `TRACE_CAPTURE` policy replaces them.
