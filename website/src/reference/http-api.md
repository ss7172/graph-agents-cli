---
description: The HTTP API of a graph-agents-cli agent: /chat and its events, threads, approvals, probes, metrics and A2A.
---

# HTTP API

The HTTP surface of a generated agent service: `/chat` and its server-sent events, threads, approvals, health, readiness, metrics and A2A, with status codes and limits.

<!--
WRITER BRIEF (lane: reference). Replace this comment and the paragraph above with the finished
page; keep the H1 and the front-matter description (update it if the scope changes).
Rules and shared components: website/COVERAGE.md. Code is the source of truth: when the
README and the code disagree, follow the code and say so in your report.
Paths: CLI = src/graph_agents_cli/   TPL = CLI + scaffold/agents/langgraph/
       K8S = CLI + scaffold/deployment_targets/kubernetes/python/   CHART = K8S + deployment/helm/{{cookiecutter.project_name}}/
       BASE = CLI + scaffold/base_templates/
Upstream pages: git show HEAD:<path> in a google/agents-cli checkout (tracked files only; your task names its location).

Must cover:
- The endpoints table (route, auth action, behaviour) for every route, dev-only routes included.
- /chat: request body, Accept: text/event-stream, every event type and its fields (message.start, message.delta, tool.call, tool.result, message.end with usage, latency and status, error), every status (ok, step_limit, awaiting_approval, ...), a real example stream captured with curl -N on the fake model.
- Errors: the error event {code, message, error_id, run_id} and its codes; the 500 body with a reference; 503 for an unreachable database; how a failed tool call reaches clients outside dev.
- Guardrails, one row or short section each: one run per thread (409 thread_busy, leases), limits (413, the 422 rules), thread id format, timeouts, the step limit, history repair, tool arguments that are not valid JSON.
- Threads: GET /threads (scope, limit, offset), messages, DELETE statuses.
- Approvals: GET /threads/{id}/approvals, GET /approvals, POST decision (403, 404, 409, 410), the approval object's fields.
- Health, readiness and metrics (brief; details in observability.md).
- A2A: card path, JSON-RPC (A2A 1.0, 0.3 clients on the same URL), text reply and lastChunk, errors -32602 and -32001, A2A_DESCRIPTION, AGENT_VERSION, the card's public URL (APP_URL), private tasks, approvals over A2A (input-required and the data part).
- langgraph-server differences (DELETE is the server's route; native routes and what they skip).
- Limitations: the A2A task store is per replica; the run lease; the relevant KI entries.

Sources:
- README: "### Endpoints" (table and Behaviour bullets, except Logging, Tracing, Database and Retention: see observability.md and deploy.md); "### Human approval of calls" bullets "What happens", "A2A"; "## Known limitations" bullets "A2A task store", "Run lock across replicas", "`langgraph-server` specifics".
- CHANGELOG 0.2.0: Breaking "Chat API changes", "A run that reaches the step limit ends with a reply ...", "Run records and statuses", "`GET /threads` lists the caller's own threads ...", "One message cap for every surface", "Failed tool calls reach clients as an error id"; Added "Endpoints", "Runtime guardrails", "Run leases", "A2A".
- KNOWN_ISSUES: KI-001, KI-003, KI-018, KI-019, KI-020, KI-023 to KI-026, KI-034, KI-039, KI-060, KI-061, KI-088.
- Skills: graph-agents-cli-langgraph-code/references/template-contract.md.
- Code: TPL/app/fast_api_app.py (routes), TPL/app/app_utils/{chat,threads,approvals,a2a,run_locks,limits,middleware,metrics}.py; the contract under test in TPL/tests/integration/test_api_surface.py.

Verify (MODEL_PROVIDER=fake, GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1, scratch dirs only):
- Start playground --port <your range> --no-open in a scratch project (fake model) and curl the routes on that port only.

Link to at least: ../guides/develop.md, ../guides/approvals.md, ../guides/authentication.md, ../guides/observability.md, environment.md
-->
