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
  version: "0.2.0"
  requires:
    bins:
      - graph-agents-cli
    install: "uv tool install git+https://github.com/ss7172/graph-agents-cli@v0.2.0"
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
| `metadata` (default) | span structure and timing; model and provider names; token counts; tool **names**; error **types** and HTTP status codes; identifiers (`thread_id`, `run_id`, hashed principal id, agent version) | prompt text, completion text, tool arguments, tool results, error messages, the client's `/chat` metadata |
| `full` | everything in `metadata` plus prompts, completions, tool arguments, tool results, full error messages, and the client's `/chat` metadata (as `client_metadata`) | |

Clients can never overwrite trace ids through `/chat` metadata, and that metadata is never
written into checkpoints (it is kept in the run record).

Enabling `full` against a hosted destination sends user content off-network. The consuming
project must decide that explicitly (and publish a truthful privacy notice) before you set it. Do
not switch to `full` on your own to debug; ask, and prefer reproducing locally with
`graph-agents-cli run -v`, which prints the events to your terminal without exporting anything.

The same policy governs the chat API: `tool.call` events omit `args` for a caller who is not the
thread's owner under `metadata`.

## Hashed principal id

Traces, logs, run records, and eval traces record `Principal.hashed_id()` (the first 16 hex
characters of sha256 of the policy's `Principal.id`, or of HMAC-SHA256 keyed with
`PRINCIPAL_HASH_SALT` when that secret is set), never the raw id. Audit and thread ownership use
the same identity, so a support engineer can correlate a trace with a conversation without the
trace revealing who the user is. Set `PRINCIPAL_HASH_SALT` (and add it to `secrets.keys`) when ids
are guessable, such as email addresses: without a salt anyone holding a trace can confirm a guess
by hashing it. Changing the salt changes every hash. Under `shared-bearer` every caller is
`shared`. Under `langgraph-server`, runs started through the server's native API carry the raw id
in checkpoint metadata (the server injects it); `/chat` runs carry only the hash.

## Run records

Besides traces, the app writes one run record per `/chat` call: run id, thread id, hashed
principal, model, token counts, latency, status, error type, the client's `/chat` metadata;
payload (prompt, completion, tool I/O) only under `TRACE_CAPTURE=full`. The record is written
when the run starts (`running`) and updated when it ends: `ok`, `step_limit` (the run reached
`RECURSION_LIMIT` and ended with a reply saying so), `error`, `timeout`, `cancelled` (the client
left) or `interrupted` (the run lost its lease on the thread, or its process died: a crash, an
OOM kill, a lost node, a rollout that ran out of grace). Records a dead process left `running`
are marked `interrupted` (`error_type` `ProcessLost`) by any replica within about a minute of
the run's 30 s lease expiring, so crashes can be counted and audited.

- `CHECKPOINTER=postgres`: durable rows in the agent-owned database (`runs` table, created
  under an advisory lock).
- `CHECKPOINTER=memory`: in-process, served to the `/playground` page while the process lives,
  lost on restart. Local development needs no database.
- Under `langgraph-server`, the app keeps its records in an `agent_runs` table in the server's
  Postgres (`DATABASE_URI`), beside the server's own Runs API.
- Retention: `RETENTION_DAYS=N` deletes threads idle for more than N days with their checkpoints
  and run records, in an hourly best-effort pass on every replica (0, the default, keeps
  everything). `DELETE /threads/{thread_id}` deletes one thread and its records on request.
- Evaluation never depends on run records; `eval generate` writes trace files.

## Logs, metrics and health

- **Logs** are JSON lines by default outside `APP_ENV=dev` (`LOG_FORMAT=json|text`, `LOG_LEVEL`),
  each with the request id (`X-Request-ID`, echoed to the client), run id, thread id and hashed
  principal. Client-facing errors carry an `error_id`; the exception is logged under that id. The
  app does not log credentials, messages or tool arguments.
- **Metrics:** `GET /metrics` serves Prometheus text (`METRICS_ENABLED`, default true):
  `http_requests_total` and `http_request_duration_seconds` (by method, route, status),
  `agent_runs_total` (by status; `interrupted` also counts the dead processes' runs a replica
  closed, once across replicas), `agent_active_runs`, `agent_run_duration_seconds`,
  `agent_tokens_total`, `agent_database_up` (0 while the database is known to be unreachable).
  It is unauthenticated unless `METRICS_TOKEN` is set (then the scraper
  sends `Authorization: Bearer <token>`), and the chart never publishes it on the Gateway or
  Ingress. Scrape it with `metrics.serviceMonitor.enabled` (Prometheus Operator; add
  `metrics.serviceMonitor.bearerToken.enabled` when `METRICS_TOKEN` is set, so it sends the
  token from the app Secret) or `metrics.scrapeAnnotations` (annotations carry no token: put
  it in that Prometheus's scrape job). Under `langgraph dev` the server's own `/metrics` answers instead;
  the server image disables it so the app's is served.
- **Health:** `GET /health` is liveness (the process answers); `GET /ready` is readiness (the
  database is set up and answers within 2 s, else 503). A pod started during a database outage
  stays up and unready, and is ready again seconds after the database is. During an outage
  requests get 503 within a few seconds and each logs one WARNING line (`Database unavailable
  (error_id=...)`), without a traceback. Useful alerts: `/ready` failing,
  `agent_database_up == 0`, a rising `agent_runs_total{status!="ok"}` (notably `interrupted`
  and `step_limit`), `agent_active_runs` near capacity.

## What is never captured by default

- Prompt and completion text, tool arguments and results, error messages (only under `full`).
- Raw principal ids (except the langgraph-server native-API case above), session cookies,
  session tokens, bearer keys and `attributes["credentials"]` (never, under any setting).
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
- Cluster-wide metrics collection, logging stacks, dashboards, alerting rules: platform tooling
  outside the CLI (the app exposes `/metrics` and JSON logs for them).
- Self-hosted LangSmith installation (point `LANGSMITH_ENDPOINT` at one that exists).

## Migration note

Compared with google-agents-cli: Cloud Trace, Cloud Logging, prompt-response logging to GCS and
BigQuery, and the BigQuery Agent Analytics plugin are gone, and tracing is no longer always on.
LangSmith or OTLP behind `TRACING_ENABLED` with the `TRACE_CAPTURE` policy replaces them.
