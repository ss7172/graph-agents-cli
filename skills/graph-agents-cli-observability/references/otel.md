# OpenTelemetry (OTLP) destination

Used when `TRACING_ENABLED=true` and no `LANGSMITH_API_KEY` is set. The only tracing path in the
disconnected profile.

## Variables

| Variable | Where | Notes |
|---|---|---|
| `TRACING_ENABLED=true` | `.env` / chart `tracing.enabled` | required |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | chart `tracing.otlpEndpoint` / `.env` | e.g. `http://otel-collector.observability.svc:4318` (OTLP/HTTP) |
| `TRACE_CAPTURE` | `.env` / chart `tracing.capture` | `metadata` (default) or `full` |

## Instrumentation

`app/app_utils/telemetry.py` uses the OpenInference LangChain instrumentor
(`openinference-instrumentation-langchain`) with `opentelemetry-exporter-otlp-proto-http`. It is
initialised only when tracing is enabled and no LangSmith key is present; otherwise the modules
are not imported. Standard `OTEL_*` variables (`OTEL_RESOURCE_ATTRIBUTES`,
`OTEL_EXPORTER_OTLP_HEADERS`) are honoured by the SDK. The resource's `service.name` is
`OTEL_SERVICE_NAME` when set, else `LANGSMITH_PROJECT`, else the name of the working directory
(`app` in the fastapi image, the project name in the server image); the chart does not set it, so add `OTEL_SERVICE_NAME: <name>` to the
chart's `env` (or `values-<env>.yaml`) to name the service in your backend.

## Spans and attributes

- Root span per `/chat` call; child spans per graph node, model call, and tool.
- Attributes under `metadata`: model and provider names, token counts, tool names, error types,
  HTTP status codes, `thread_id`, `run_id`, `principal.hashed_id`, `agent_version`.
- Under `full`: additionally `input.value` / `output.value` on model and tool spans (prompts,
  completions, tool arguments and results) and full exception messages. Under `metadata` the
  exporter drops `exception.message` / `exception.stacktrace` from span events and the ERROR
  status description (where OpenInference puts `repr(exc)` plus the traceback); the LangSmith
  client likewise reduces a run's `error` to the exception class.

The capture policy is applied by a span processor before export, so a collector never sees
content that the policy excludes.

## Collector

Any OTLP-capable collector: the OpenTelemetry Collector, Jaeger, Tempo, or a vendor agent. A
minimal in-cluster collector receives OTLP/HTTP on 4318 and exports to the backend the platform
already runs. Installing and operating the collector is outside the CLI; `infra check` does not
verify it (the endpoint is a plain URL the pod must reach).

## Local use

Run a collector locally (for example the OpenTelemetry Collector container or Jaeger all-in-one),
set `TRACING_ENABLED=true` and `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` in `.env`, and
use `graph-agents-cli playground` or `run`. Nothing leaves the machine.

## Troubleshooting

| Symptom | Fix |
|---|---|
| No spans | `TRACING_ENABLED` not `true`, or a LangSmith key is set (LangSmith wins) |
| Connection refused | wrong Service DNS or port; OTLP/HTTP is 4318, OTLP/gRPC is 4317 (this exporter uses HTTP) |
| Spans without content | expected under `metadata` |
| Import errors at startup | the OTel packages are in the template's `pyproject.toml`; run `graph-agents-cli install` |
