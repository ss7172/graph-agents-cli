# LangSmith destination

Used when `TRACING_ENABLED=true` and `LANGSMITH_API_KEY` is set.

## Variables

| Variable | Where | Notes |
|---|---|---|
| `TRACING_ENABLED=true` | `.env` / chart `tracing.enabled` | required; the key alone does nothing |
| `LANGSMITH_API_KEY` | Secret (`secrets.keys` includes it by default) | |
| `LANGSMITH_PROJECT` | `.env` / chart `tracing.langsmith.project` | defaults to the project name; use one per environment (`my-agent-staging`) |
| `LANGSMITH_ENDPOINT` | `.env` / chart | override for a self-hosted LangSmith instance |
| `TRACE_CAPTURE` | `.env` / chart `tracing.capture` | `metadata` (default) or `full` |

`app/app_utils/telemetry.py` sets the LangSmith tracing variables for the LangChain tracer only
when enabled; it does not rely on `LANGSMITH_TRACING` being set by the operator, and it removes
prompt and tool content from runs under `metadata` before they are sent.

## What a run looks like

- One LangSmith run per `/chat` call, named after the graph, tagged with `agent_version`,
  `runtime`, `checkpointer`, and `thread_id`, with `metadata.principal` = hashed id.
- Child runs per node, model call, and tool. Under `metadata` the model call shows token counts
  and model name, inputs and outputs are redacted; tool runs show the tool name only.
- Under `full` inputs and outputs are present.

## Self-hosted LangSmith

Point `LANGSMITH_ENDPOINT` at the instance's API URL. The instance itself (enterprise licence,
Postgres, Redis, ClickHouse) is outside this CLI. On a disconnected network a self-hosted
instance is the only LangSmith option; hosted LangSmith is excluded from the disconnected profile.

## `eval submit`

Uploads a dataset and a results file as a LangSmith dataset plus experiment, so eval history can
be browsed there. Requires `LANGSMITH_API_KEY`; optional; never part of the gate; disabled in the
disconnected profile. It does not need `TRACING_ENABLED`.

```bash
graph-agents-cli eval submit --results artifacts/grade_results/results_<ts>.json
```

## Finding a trace

`graph-agents-cli run --url ... "hello" -v` prints `message.start` with `thread_id` and `run_id`;
filter the LangSmith project by `thread_id` tag or `run_id` metadata.
