# Terminology: user terms to CLI values

Users describe the same thing in many ways. Map their words to graph-agents-cli values before
choosing flags or editing configuration.

## Runtime and framework

| User says | CLI value |
|---|---|
| "LangGraph", "a graph", "StateGraph", "ReAct agent", "create_agent" | `--agent langgraph` (the only bundled template); the graph lives in `app/agent.py` |
| "plain FastAPI", "just a container", "no license", "simplest thing" | `--runtime fastapi` (default) |
| "LangGraph Platform", "LangGraph Server", "Agent Server", "langgraph-api", "Assistants/Threads/Runs API", "Studio-compatible server" | `--runtime langgraph-server` (needs Postgres and Redis; the deployed `langgraph-api` image checks for a LangGraph licence at startup, so it is outside the disconnected profile; the local `langgraph dev` server needs no licence) |
| "LangGraph Studio", "visual graph debugger" | `graph-agents-cli playground --graph` (`langgraph dev`, bypasses auth) |
| "chat page", "try it in the browser" | `graph-agents-cli playground` (`/playground`, only when `APP_ENV=dev`) |
| "A2A", "agent-to-agent", "agent card" | built in: `/a2a/<agent_directory>/.well-known/agent-card.json`; `run --mode a2a`; tasks are private to their principal and kept in memory per replica (`A2A_TASK_TTL_S`) |
| "health check", "liveness", "readiness", "probe" | `GET /health` (liveness), `GET /ready` (the database answers) |
| "metrics", "Prometheus", "monitoring" | `GET /metrics` (`METRICS_ENABLED`, optional `METRICS_TOKEN`); chart `metrics.serviceMonitor` / `metrics.scrapeAnnotations` |
| "timeout", "runaway agent", "loop" | `RUN_TIMEOUT_S`, `MODEL_TIMEOUT_S`, `MODEL_MAX_RETRIES`, `RECURSION_LIMIT` |
| "concurrent requests on one conversation" | one run per thread: 409 `{"code": "thread_busy"}` |
| "rate limiting", "throttling" | not built in: configure it at the Gateway or ingress |

## Models

| User says | CLI value |
|---|---|
| "OpenAI", "GPT" | `--model-provider openai`, key `OPENAI_API_KEY` |
| "Anthropic", "Claude" | `--model-provider anthropic`, key `ANTHROPIC_API_KEY` |
| "Gemini", "AI Studio key" | `--model-provider gemini`, key `GOOGLE_API_KEY` (the AI Studio API key path only) |
| "Ollama", "vLLM", "TGI", "OpenRouter", "local model", "self-hosted model", "open-source model", "OpenAI-compatible" | `--model-provider openai-compatible`, `OPENAI_BASE_URL`, key `MODEL_API_KEY` |
| "judge model", "grader", "LLM-as-judge" | `JUDGE_MODEL_PROVIDER`, `JUDGE_MODEL_NAME`, `JUDGE_BASE_URL`, `JUDGE_API_KEY` (default to the agent's) |
| "fake model", "no key in CI" | provider `fake` (tests only; never offered by `create`) |

## Persistence

| User says | CLI value |
|---|---|
| "memory", "remember the conversation", "threads", "checkpoints", "session" | the checkpointer; continuity is by `thread_id` on `/chat` |
| "Postgres", "durable", "survives restarts", "multiple replicas" | `--checkpointer postgres`; `CHECKPOINTER=postgres` + `POSTGRES_DSN` (fastapi) or `DATABASE_URI` + `REDIS_URI` (langgraph-server) |
| "in-memory", "no database locally" | `CHECKPOINTER=memory` (the `.env.example` default; run records are in-process) |
| "run history", "usage records" | run records (follow the checkpointer; payload only under `TRACE_CAPTURE=full`) |
| "list my conversations", "delete a conversation" | `GET /threads`, `DELETE /threads/{thread_id}` (owner only) |
| "data retention", "purge old conversations" | `RETENTION_DAYS` (0 keeps everything; an hourly best-effort purge of idle threads) |

## Auth and outbound API access

| User says | CLI value |
|---|---|
| "API key", "bearer token", "shared secret", "internal tool" | `--auth-policy shared-bearer`; `API_KEY` in the Secret; clients send `Authorization: Bearer` |
| "OIDC", "SSO", "JWT", "access token", "per-user identity", "who owns the conversation" | `--auth-policy jwt` (per-user principals from a verified token: `AUTH_JWT_ISSUER`, `AUTH_JWT_AUDIENCE`, JWKS or public key) |
| "our app's session cookie", "existing roles", "custom header", "gateway identity headers" | `--auth-policy custom` (stub fails closed until implemented in `app/policies/custom.py`); clients use `--header` or `--cookie` |
| "support staff may read conversations", "admins" | `AUTH_READ_ACROSS_ROLES` (read others' threads), `AUTH_ADMIN_ROLES` (manage assistants, crons and the store under `langgraph-server`) |
| "which endpoints may the agent call", "read-only", "read-write", "only these operations", "allow-list", "deny" | `api-policy.yaml` (`apis: <name>:` with `allowed_methods`, `allowed_operations`, `denied_operations`, optional `limits`), changed with `graph-agents-cli api add --access read-only\|read-write\|custom`, `api access`, `api allow`, `api deny`, `api revoke`; seeded by `create --api-policy <file>`. The access level is the user's choice per API; there is no default |
| "the agent may call it at most N times", "rate limit the API", "don't hammer the backend" | `limits: {max_calls_per_run, rate_per_minute}` on the API (`graph-agents-cli api limits`); per run and per replica |
| "ask a human before the agent writes" | an approval gate on API calls is planned, not available (the `approval` key is refused). Meanwhile the write tool asks and a later turn confirms (the two-step pattern of `/graph-agents-cli-langgraph-code` section 2a), with `require_user_mentioned` on the record id; a LangGraph `interrupt` is not wired to `/chat` and stalls the stream |
| "don't let customer text steer the agent", "prompt injection", "act only on the user's own records" | tool results are fenced as untrusted data (`UntrustedToolResults`) and the default prompt forbids following them; write tools call `require_user_mentioned` and `require_owner`, and write APIs use `auth: forward` so the upstream authorizes the user (`/graph-agents-cli-langgraph-code` section 2a) |
| "call our backend API", "backend client", "call the service as the user" | `get_client("<api>")` from `app/app_utils/api_client.py`, the only HTTP path to external APIs (`auth: none`, `bearer`, or `forward` for the caller's own credential); tools declare `API_CALLS` |

## Deployment

| User says | CLI value |
|---|---|
| "Kubernetes", "k8s", "our cluster", "on-prem", "self-hosted", "OpenShift", "RKE2", "kubeadm" | `--deployment-target kubernetes` (Helm chart in `deployment/helm/<name>/`) |
| "kind", "k3s", "k3d", "minikube", "Docker Desktop", "local cluster" | environment `dev` with local-load (image loaded into the node, no registry) |
| "prototype", "just locally", "no deployment yet" | `--prototype` (target `none`, `cd: skip`) |
| "GitOps", "Argo", "pull-based", "PR to deploy" | `--cd argocd` |
| "runner in our network", "push-based", "CI deploys" | `--cd helm-push` (self-hosted GitHub runner; the `DEPLOY_KUBECONFIG` secret of the `staging` / `production` environment) |
| "CI only", "I'll deploy by hand" | `--cd skip` (direct modes) |
| "registry", "GHCR", "Harbor", "where do images go" | `--registry <url/org>` (default `ghcr.io/<org>`) |
| "ingress", "route", "hostname", "TLS" | chart values: `gateway` (Gateway API `HTTPRoute`, default) or `ingress`; `tls.existingSecret` or `tls.certManager` |
| "dev / staging / prod", "namespaces" | environments `dev`, `staging`, `prod`; namespace `<name>-<env>`; `values-<env>.yaml`; Secret `<name>-app` |
| "secrets", "rotate the key" | `secrets apply --env <env>` (from `.env.<env>`; `--rotate-api-key` for `API_KEY`) then `deploy --restart --env <env>` |
| "which cluster", "kube context" | `environments.<env>.context` in the manifest, or `--context`; outside dev the current context needs a confirmation or `--yes` |
| "who approves production" | the merge of the PR touching `values-prod.yaml` (code owners, no self-approval, `pr_checks` green); the GitHub `production` environment gates promotion from CI |
| "air-gapped", "disconnected", "no internet", "offline" | the disconnected profile (`infra check --profile disconnected`, `login --profile disconnected`) |

## Evaluation and observability

| User says | CLI value |
|---|---|
| "test cases", "golden set", "eval dataset" | `tests/eval/datasets/*.json` |
| "must contain", "must call tool X", "no tool calls", "under N ms" | `expect.contains`, `expect.tool_calls`, `expect.no_tool_calls`, `expect.max_latency_ms` |
| "quality score", "rubric", "graded by a model" | `judge.<metric>` with `threshold`; mandatory unless listed under `quality_metrics:` |
| "allow 90 % to pass" | `quality_metrics.<metric>.min_pass_rate: 0.9` (judge metrics only) |
| "upload to LangSmith", "experiment" | `eval submit` |
| "tracing", "traces", "spans" | `TRACING_ENABLED=true` (off by default) |
| "log the prompts", "full payloads" | `TRACE_CAPTURE=full` (default `metadata`) |
| "OpenTelemetry", "OTLP", "our collector", "Jaeger", "Tempo" | `OTEL_EXPORTER_OTLP_ENDPOINT` (used when no `LANGSMITH_API_KEY`) |

## Renamed in graph-agents-cli 0.2.0

| 0.1.0 name | 0.2.0 name |
|---|---|
| `--auth-policy product-session`, `ProductSessionPolicy` | `--auth-policy custom`, `CustomPolicy` in `app/policies/custom.py` (the old name is read as `custom` with a warning) |
| `product-policy.yaml`, `--product-policy`, manifest `product_api:` | `api-policy.yaml` (`apis: {<name>: ...}`), `--api-policy`, `api_policy: {policy_file: api-policy.yaml}` |
| `PRODUCT_CALLS`, `product_client` | `API_CALLS` (with `"api"`), `api_client.get_client("<api>")` |
| `CLI_VERSION_PIN` in `.github/agent.env` | `GRAPH_AGENTS_CLI_SPEC` (a full install spec) |
| default guidance file `GEMINI.md` | `AGENTS.md` |
| repository secret `KUBECONFIG` (helm-push) | environment secret `DEPLOY_KUBECONFIG` |

## Migration note

Old names from google-agents-cli and their replacements, for users who still use them:

| Old term | Now |
|---|---|
| ADK, Agent Development Kit, `root_agent` | LangGraph; `graph` in `app/agent.py` |
| Agent Engine, Agent Runtime, Cloud Run, GKE, `--deployment-target agent_runtime\|cloud_run\|gke` | `--deployment-target kubernetes` |
| Vertex AI, gcloud ADC, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_GENAI_USE_VERTEXAI` | provider keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `MODEL_API_KEY`) plus a kubeconfig |
| Agent Platform Sessions, Cloud SQL sessions, `--session-type` | `--checkpointer memory\|postgres` |
| Cloud Build, `--cicd-runner google_cloud_build\|github_actions` | GitHub Actions always; `--cd argocd\|helm-push\|skip` |
| Terraform, `infra single-project`, `infra cicd` | none; `infra check` is read-only |
| Secret Manager | Kubernetes Secret `<release>-app` via `secrets apply` |
| Cloud Trace, Cloud Logging, BigQuery Agent Analytics, `--bq-analytics` | LangSmith or OTLP behind `TRACING_ENABLED` |
| Agent Platform eval service, `eval optimize`, `eval dataset synthesize`, `eval results` | in-process deterministic checks and judge; those commands were removed |
| `publish gemini-enterprise`, Agent Registry, Agent Gateway | removed; `deploy` exposes the agent in-cluster |
| `agents-cli-manifest.yaml`, `AGENTS_CLI_*` env vars, `~/.agents-cli` | `graph-agents-cli-manifest.yaml`, `GRAPH_AGENTS_CLI_*`, `~/.graph-agents-cli` |
