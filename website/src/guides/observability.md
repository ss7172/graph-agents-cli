---
description: Structured logs, Prometheus metrics, health probes, run records and opt-in tracing to LangSmith or an OTLP collector for graph-agents-cli agents.
---

# Observability

<p class="gac-lede">A generated agent writes JSON logs, serves Prometheus metrics and two health
probes, and records every run. Tracing is off until you turn it on, and even then prompts and
tool data stay out of traces unless you ask for them.</p>

| Signal | Where | On by default |
|---|---|---|
| [Logs](#logs) | stdout, one JSON object per line outside `APP_ENV=dev` | yes |
| [Metrics](#metrics) | `GET /metrics`, Prometheus text | yes (`METRICS_ENABLED`) |
| [Probes](#health-probes) | `GET /health`, `GET /ready` | yes |
| [Run records](#run-records-and-retention) | the app database | yes |
| [Traces](#tracing) | LangSmith or an OTLP/HTTP collector | no (`TRACING_ENABLED`) |

## Logs

Outside `APP_ENV=dev` the app logs one JSON object per line (`LOG_FORMAT=json|text`, default
`text` under `dev`) at `LOG_LEVEL` (default `INFO`). Every record carries the request id, and a
record written during a run also carries the run id, the thread id and the hashed principal id.
These lines come from a local run with `LOG_FORMAT=json` and a caller-supplied request id:

```json
{"ts": "2026-09-25T02:48:32.119+00:00", "level": "INFO", "logger": "uvicorn.access", "message": "127.0.0.1:50136 - \"POST /chat HTTP/1.1\" 200", "request_id": "docs-example-req-1", "principal_hash": "a4d26868017c0ccf"}
{"ts": "2026-09-25T02:48:32.130+00:00", "level": "INFO", "logger": "app.app_utils.chat", "message": "run finished", "request_id": "docs-example-req-1", "run_id": "9adb1647-c17f-43ca-a9bb-90f170af14c8", "thread_id": "a273c0de-479e-4ddc-8ec6-de7fbb4f9589", "principal_hash": "a4d26868017c0ccf", "status": "ok", "latency_ms": 10}
```

Every response carries `X-Request-ID`. A valid one from the caller (1 to 128 characters of
letters, digits and `._:-`) is echoed and used in the logs; otherwise the app generates one.

### What is never logged

The app logs no credentials, messages or tool arguments:

- Access lines keep the path and drop the query string, so a token a client put in a URL is
  never logged.
- The HTTP client libraries (`httpx`, `httpcore`, and `httpx2` / `httpcore2` of the model
  SDKs) log at `WARNING` only, because their `INFO` lines carry full outbound URLs.
- The API client logs each outbound call by API, method, operation id and path template, never
  the query, the concrete path or the body:
  `api call done: <api> <METHOD> <operation|template> -> <status> (<ms> ms)`.
- Python warnings become log records too (JSON under `fastapi`), with the values pydantic
  warnings echo redacted.

When something fails:

- An unexpected error answers 500 with a reference, and its exception and traceback are logged
  under that `error_id`.
- A failed tool call logs one `WARNING` with its error id, the tool name and the error type.
- An unreachable database logs one `WARNING` line, without a traceback.

!!! warning "`LOG_LEVEL=DEBUG` is for your machine"
    `DEBUG` also turns on third-party debug output, which can include message content. Keep it
    for local debugging.

### Under `langgraph-server`

LangGraph Server formats its own lines, with its own `LOG_JSON` and `LOG_LEVEL`; `LOG_FORMAT`
and the per-record ids above apply to `fastapi` only. The app applies the same rules to the
server's logs: its access lines (`langgraph_api.server`) lose their `query_string` field, the
HTTP client libraries log at `WARNING` only, and warnings are captured with their values
redacted.

## Metrics

`GET /metrics` serves Prometheus text while `METRICS_ENABLED` is true (the default). Metrics
are per process, so scrape every replica. Labels never carry ids, principals or client input.

| Metric | What it counts |
|---|---|
| `http_requests_total` | Counter of every HTTP request by `method`, `route` and `status`. `route` is the route template (`/threads/{thread_id}/messages`), or `unmatched` for a 404. |
| `http_request_duration_seconds` | Histogram of request duration by `method` and `route`; a streamed `/chat` counts until its last event. |
| `agent_runs_total` | Counter of finished runs by `status`: `ok`, `awaiting_approval`, `step_limit`, `error`, `timeout`, `cancelled`, `interrupted`. |
| `agent_active_runs` | Gauge of runs in progress. |
| `agent_run_duration_seconds` | Histogram of run duration. |
| `agent_tokens_total` | Counter of model tokens by `kind` (`input`, `output`). |
| `agent_database_up` | Gauge: 1 while the database answered at last contact, 0 while it is known to be down (always 1 without a database). |
| `agent_approvals_total` | Counter of human approvals of gated API calls by `event`: `requested`, `approved`, `rejected`, `expired` (see [Human approval](approvals.md)). |
| `agent_token_exchanges_total` | Counter of the tokens asked for [`auth: exchange`](api-policy.md#auth-exchange-act-for-the-user-at-another-agent) APIs by `api` (its name in api-policy.yaml) and `outcome`: `issued` (the issuer minted one), `cached` (a kept one, or one another call was exchanging), `refused` (the issuer refused, or refused within `TOKEN_EXCHANGE_FAILURE_TTL_S`), `no_actor` (the issued token names no actor and the API does not set `exchange.allow_actorless`: refused by this agent, and remembered for `TOKEN_EXCHANGE_FAILURE_TTL_S`), `unavailable` (a timeout, connection error, 5xx or unusable answer), `circuit_open` (failed at once while the issuer's breaker is open). |
| `agent_token_exchange_duration_seconds` | Histogram of the exchanges sent to the issuer, by `api`. |

After one `/chat` request on the fake model, the agent series look like this:

```text
agent_runs_total{status="ok"} 1.0
agent_approvals_total{event="requested"} 0.0
agent_active_runs 0.0
agent_database_up 1.0
agent_run_duration_seconds_count 1.0
agent_tokens_total{kind="input"} 12.0
agent_tokens_total{kind="output"} 11.0
```

### Protect `/metrics` with a token

`/metrics` sits outside the auth policy, like the probes. The route never publishes it, but
anything that can reach the pods can read it. With `METRICS_TOKEN` set, it answers only
`Authorization: Bearer <METRICS_TOKEN>` and gives everyone else 401:

```bash
python -c "import secrets; print(secrets.token_hex(32))"   # generate a token
curl -H "Authorization: Bearer $METRICS_TOKEN" http://127.0.0.1:8000/metrics
```

`METRICS_TOKEN` is a secret: add it to `secrets.keys` and put it in the env file (see
[Secrets](secrets.md)).

### Scrape it from the chart

Both scraping options are off by default:

=== "ServiceMonitor (Prometheus Operator)"

    ```yaml title="values-prod.yaml"
    metrics:
      serviceMonitor:
        enabled: true
        interval: 30s
        labels:
          release: kube-prometheus-stack   # what your Prometheus selects on
        bearerToken:
          enabled: true                    # needed when METRICS_TOKEN is set
    ```

    With `bearerToken.enabled`, the ServiceMonitor reads the token from the Secret
    `<release>-metrics`, which holds `METRICS_TOKEN` alone. `secrets apply` and a direct
    `deploy` write it whenever the app Secret holds `METRICS_TOKEN`. Grant Prometheus read access
    to that Secret only, never to the app Secret. `bearerToken.secretName` and `.key` name a
    Secret of your own.

=== "Pod annotations"

    ```yaml title="values-prod.yaml"
    metrics:
      scrapeAnnotations: true
    ```

    This adds `prometheus.io/scrape`, `prometheus.io/path` and `prometheus.io/port` to the pods,
    for a Prometheus that discovers pods by annotation. Annotations cannot carry a token: with
    `METRICS_TOKEN` set, give that Prometheus's scrape job the token itself.

The chart refuses to render scraping while `env.METRICS_ENABLED` turns `/metrics` off. With a
[NetworkPolicy](deploy.md#networkpolicy), admit your Prometheus's namespace.

### Alerts worth having

| Alert on | Why |
|---|---|
| `/ready` failing, or `agent_database_up == 0` | the database is unreachable; pods leave the Service |
| a rising rate of `agent_runs_total` with status `error`, `timeout` or `interrupted` | runs are failing, timing out or losing their lease |
| `agent_runs_total{status="step_limit"}` growing | runs hit `RECURSION_LIMIT`; a tool may loop |
| `http_requests_total{status=~"5.."}` | server errors and database outages (503) |

```text title="PromQL"
sum by (status) (rate(agent_runs_total{status=~"error|timeout|interrupted"}[5m])) > 0
min(agent_database_up) < 1
```

`awaiting_approval` is a normal outcome when you gate calls, so leave it out of failure alerts.

## Health probes

| Route | Answers | The chart uses it for |
|---|---|---|
| `GET /health` | 200 `{"status": "ok", "runtime", "checkpointer"}` while the process answers | startup and liveness probes |
| `GET /ready` | 200 `{"status": "ready"}` when the database (and run store) is set up and answers within 2 s, else 503 `{"status": "not_ready"}` | readiness probe |

```console
$ curl -s http://127.0.0.1:8000/health
{"status":"ok","runtime":"fastapi","checkpointer":"memory"}
$ curl -s http://127.0.0.1:8000/ready
{"status":"ready"}
```

Neither needs credentials, and the chart's route never publishes them. Because readiness and
liveness are separate, a pod that loses its database leaves the Service endpoints instead of
being restarted, and comes back seconds after Postgres does.

## Run records and retention

Every run is recorded in the app database when it starts (`running`) and updated when it ends:

| Status | Meaning |
|---|---|
| `ok` | the run finished |
| `awaiting_approval` | the run paused before a gated API call and waits for a decision (see [Human approval](approvals.md)) |
| `step_limit` | the run reached `RECURSION_LIMIT` and ended with a reply saying so |
| `error` | the run failed |
| `timeout` | the run exceeded `RUN_TIMEOUT_S` (default 300) |
| `cancelled` | the client disconnected |
| `interrupted` | the run lost its lease on the thread, its process died, or a pod's shutdown drain cut it (see [Limitations](#limitations)) |

A record left `running` by a dead process is marked `interrupted` (error type `ProcessLost`)
about a minute after its lease expires, and counted in `agent_runs_total{status="interrupted"}`.
The client's `/chat` metadata is kept in the run record only: never in checkpoints, and in
traces only under `TRACE_CAPTURE=full`.

`RETENTION_DAYS=N` deletes threads idle for more than N days, with their checkpoints and run
records, in an hourly best-effort pass on every replica. Idleness is re-checked under the
thread's lock. The default, 0, keeps everything.

## Tracing

Tracing is off unless `TRACING_ENABLED` is `true`, `yes` or `1` (any case); any other value
leaves it off, and the app logs `Tracing disabled (TRACING_ENABLED != true).` at startup. When
it is on, traces go to LangSmith if `LANGSMITH_API_KEY` is set, else over OTLP/HTTP to
`OTEL_EXPORTER_OTLP_ENDPOINT`.

=== "LangSmith"

    ```bash title=".env"
    TRACING_ENABLED=true
    LANGSMITH_API_KEY=lsv2_...           # a secret: keep it in secrets.keys (it is by default)
    LANGSMITH_PROJECT=my-agent-staging   # default: the project name
    # LANGSMITH_ENDPOINT=https://...     # a self-hosted LangSmith
    ```

    One LangSmith run per `/chat` call, with child runs per node, model call and tool. A key
    alone does nothing: `TRACING_ENABLED` must be on too.

=== "OpenTelemetry (OTLP)"

    ```bash title=".env"
    TRACING_ENABLED=true
    OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector.observability.svc:4318
    # OTEL_SERVICE_NAME=my-agent         # default: LANGSMITH_PROJECT, which the chart sets
    ```

    Spans go over OTLP/HTTP (port 4318, not gRPC's 4317) with OpenInference's LangChain
    instrumentation: a root span per `/chat` call and child spans per node, model call and
    tool. Standard `OTEL_*` variables such as `OTEL_EXPORTER_OTLP_HEADERS` are honoured. Any
    OTLP collector works: the OpenTelemetry Collector, Jaeger, Tempo or a vendor agent.

In a cluster, set the chart values instead of the variables:

```yaml title="values-prod.yaml"
tracing:
  enabled: true
  capture: metadata                                   # metadata | full
  otlpEndpoint: http://otel-collector.observability.svc:4318
  langsmith:
    project: my-agent-prod
```

### What a trace holds

| `TRACE_CAPTURE` | Exported |
|---|---|
| `metadata` (default) | structure, timing, model and tool names, token counts, error types and ids (thread, run, hashed principal), but no prompt or completion text, tool arguments or results, or error messages |
| `full` | everything above, plus prompts, completions, tool inputs and outputs, error messages and the client's `/chat` metadata |

The policy is applied before export, so a collector never sees what it excludes. Any other
value of `TRACE_CAPTURE` stops the app at startup.

### Across agents and services

A request that crosses agents keeps one request id and, under OTLP, one trace. Every call a
tool makes through the policy client (`get_client`) to another agent
([`protocol: a2a`](api-policy.md#other-agents-and-json-rpc-apis-protocol)), or to an
[`auth: forward` or `auth: exchange`](api-policy.md#auth-modes) API, carries:

- `X-Request-ID`: this request's id. An agent built from this template takes a caller's id as
  its own, so its log records carry the same `request_id` as the caller's.
- Under OTLP tracing, the W3C trace context (`traceparent`, and `tracestate` when there is
  one) of the span of the tool that makes the call, so the callee's spans nest under it.

Only those APIs receive them: another agent, whatever its `auth`, and an API that acts for
the calling user, as another agent does when it is reached with the caller's own credential or
a token exchanged for it; both are part of the same request. Any other `auth: bearer` or `auth:
none` API is a third party: it never learns this request's id or trace.

On the receiving side, a request that carries a `traceparent` continues that trace: its root
span is a child of the caller's span, so an agent that asks another agent over A2A and the
runs it causes there show as one trace in Jaeger, Tempo or any other OTLP backend.

- A header the tool sets itself wins over the propagated one. A tool may still send its own
  `X-Request-ID` to any API.
- These headers differ for every request, so an [approval](approvals.md) does not bind them:
  the approved call is sent with the headers of the request that resumes it.
- Only W3C Trace Context is propagated, never baggage. Under LangSmith tracing only the
  request id is passed on, so each agent's run is its own LangSmith trace.
- `PROPAGATE_TRACE_HEADERS=false` turns both directions off: set it on an agent whose peers
  or `auth: forward` or `auth: exchange` APIs are outside your trust boundary, or whose
  callers should not choose its trace ids.
- `auth: forward` and `auth: exchange` are refused under `langgraph-server`, so under that
  runtime only its peers (`protocol: a2a` with `auth: bearer`) receive these headers.

Token exchange logs one line per exchange sent (`token exchange for orders_agent (audience
orders): issued (38 ms)`, or `refused (invalid_target)`, or `unavailable (timed out)`) and one
warning when the issuer's circuit breaker opens. Neither the logs nor the metrics carry any
token or the user's hash: only the issuer's RFC 6749 `error` code and the HTTP status.

### Hashed principal ids

Logs, traces and run records carry a hashed principal id, never the raw one. It is a plain
SHA-256 prefix unless `PRINCIPAL_HASH_SALT` is set, and then HMAC-SHA256 with that salt. Set the
salt when principal ids are guessable, such as email addresses: anyone who can read the logs
could otherwise confirm an id by hashing it
([KI-002](../reference/known-issues.md#ki-002-principal-hashes-are-unsalted-unless-principal_hash_salt-is-set)).
Add it to `secrets.keys` so it reaches the pods, and keep it stable: a new salt changes every
hash.

## Load test

Every project has a Locust load test for `POST /chat` in `tests/load_test/`:

```bash
export GRAPH_AGENTS_CLI_API_KEY=<the API_KEY of the target>   # jwt: a token
export LOAD_TEST_PROMPT="<a request that exercises your tools>"   # optional
uv run --with locust locust -f tests/load_test/load_test.py -H http://127.0.0.1:8000 \
    -u 10 -r 2 -t 30s --headless
```

Point `-H` at a deployed environment's URL (with that environment's key) to test it, and add
`--csv=<prefix>` to keep the latency and failure tables.

## Limitations

| Limitation | What to do |
|---|---|
| A run cut by the shutdown drain is recorded as `interrupted`, and the client's stream just ends ([KI-016](../reference/known-issues.md#ki-016-a-run-cut-by-the-shutdown-drain-is-recorded-as-interrupted-with-no-error-event)). | Raise `shutdown.drainSeconds` and `terminationGracePeriodSeconds` above your longest runs; treat a stream without `message.end` as failed. |
| A typo in `TRACING_ENABLED` silently leaves tracing off ([KI-058](../reference/known-issues.md#ki-058-tracing_enabled-accepts-any-value)). | Look for the startup line `Tracing disabled`. |
| Under `langgraph-server` the retention sweep of orphaned run records never gets past its first pages ([KI-019](../reference/known-issues.md#ki-019-langgraph-server-the-orphaned-run-record-sweep-never-gets-past-its-first-pages)). | At more than about 10,000 live threads, clean up by hand. |
| Log noise: a `langgraph-server` warning on every `/chat` run, server 500s for auth failures clients see as 503, and A2A push-notification requests logged at `ERROR` ([KI-054](../reference/known-issues.md#ki-054-langgraph-server-logs-a-warning-on-every-chat-run), [KI-055](../reference/known-issues.md#ki-055-langgraph-server-the-server-logs-500-for-auth-failures-that-clients-see-as-503), [KI-062](../reference/known-issues.md#ki-062-the-a2a-sdk-logs-push-notification-config-requests-at-error)). | Filter those messages; alert on the app's metrics and `/ready`. |

## Next steps

<div class="grid cards" markdown>

-   :material-kubernetes:{ .lg } **[Deploy to Kubernetes](deploy.md)**

    The chart's probes, metrics values and NetworkPolicy.

-   :material-shield-lock-outline:{ .lg } **[Security & production](security.md)**

    Data egress, the metrics token and the salt in the production checklist.

-   :material-tune-variant:{ .lg } **[Environment variables](../reference/environment.md)**

    Every logging, metrics, retention and tracing setting with its default.

-   :material-api:{ .lg } **[HTTP API](../reference/http-api.md)**

    `/health`, `/ready`, `/metrics` and the SSE events a run streams.

</div>
