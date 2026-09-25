---
description: Logs, Prometheus metrics, run records, probes and opt-in tracing for graph-agents-cli agents.
---

# Observability

See what a running agent does: structured logs, Prometheus metrics, run records, health probes, and opt-in tracing to LangSmith or an OTLP collector.

<!--
WRITER BRIEF (lane: guides_ops). Replace this comment and the paragraph above with the finished
page; keep the H1 and the front-matter description (update it if the scope changes).
Rules and shared components: website/COVERAGE.md. Code is the source of truth: when the
README and the code disagree, follow the code and say so in your report.
Paths: CLI = src/graph_agents_cli/   TPL = CLI + scaffold/agents/langgraph/
       K8S = CLI + scaffold/deployment_targets/kubernetes/python/   CHART = K8S + deployment/helm/{{cookiecutter.project_name}}/
       BASE = CLI + scaffold/base_templates/
Upstream pages: git show HEAD:<path> in a google/agents-cli checkout (tracked files only; your task names its location).

Must cover:
- Logging: JSON lines outside APP_ENV=dev (LOG_FORMAT, LOG_LEVEL), the ids on every record, X-Request-ID, what is never logged (credentials, messages, tool arguments; query strings dropped; HTTP client libraries at WARNING; outbound calls logged by API, method, operation id and path template), warnings as JSON, error_id tracebacks, the LOG_LEVEL=DEBUG caveat, langgraph-server differences (LOG_JSON; its access lines).
- Metrics: /metrics (METRICS_ENABLED, METRICS_TOKEN), every metric name, the approval counters; chart scraping (metrics.scrapeAnnotations, metrics.serviceMonitor, bearerToken and the <release>-metrics Secret); suggested alerts (agent_runs_total{status!="ok"}, /ready).
- Probes: /health (process only) and /ready (database within 2 s) and how the chart uses them.
- Run records and their statuses; RETENTION_DAYS.
- Tracing: TRACING_ENABLED (true, yes or 1 only), LangSmith with LANGSMITH_API_KEY else OTLP/HTTP to OTEL_EXPORTER_OTLP_ENDPOINT; TRACE_CAPTURE metadata versus full; client metadata stays in the run record; hashed principal ids and PRINCIPAL_HASH_SALT. Content tabs LangSmith / OTLP.
- The load test in tests/load_test/.

Sources:
- README: "### Endpoints" (rows /health, /ready, /metrics; bullets "Logging", "Tracing", "Run records", "Retention"); "### Chart" (metrics bullet); "## Production checklist" (METRICS_TOKEN, alerts, PRINCIPAL_HASH_SALT, TRACING_ENABLED/TRACE_CAPTURE items).
- CHANGELOG 0.2.0: Breaking "Run records and statuses"; Changed "Logs".
- KNOWN_ISSUES: KI-002, KI-016, KI-019, KI-054, KI-055, KI-058, KI-062.
- Skills: graph-agents-cli-observability/SKILL.md, references/langsmith.md, references/otel.md.
- Code: TPL/app/app_utils/{telemetry,metrics,middleware,db,threads}.py, TPL/.env.example, TPL/tests/load_test/README.md; CHART templates/servicemonitor.yaml, values.yaml (metrics.*).
- Upstream model: docs/src/guide/observability/index.md.

Verify (MODEL_PROVIDER=fake, GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1, scratch dirs only):
- Start the app locally (playground --port <your range> --no-open) and curl /health, /ready and /metrics on that port only; capture a JSON log line with LOG_FORMAT=json.

Link to at least: deploy.md, security.md, ../reference/environment.md, ../reference/http-api.md
-->
