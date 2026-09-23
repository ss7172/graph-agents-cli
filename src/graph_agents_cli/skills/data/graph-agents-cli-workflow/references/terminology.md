# Terminology: user terms to CLI values

Users describe the same thing in many ways. Map their words to graph-agents-cli values before
choosing flags or editing configuration.

## Runtime and framework

| User says | CLI value |
|---|---|
| "LangGraph", "a graph", "StateGraph", "ReAct agent", "create_agent" | `--agent langgraph` (the only bundled template); the graph lives in `app/agent.py` |
| "plain FastAPI", "just a container", "no license", "simplest thing" | `--runtime fastapi` (default) |
| "LangGraph Platform", "LangGraph Server", "Agent Server", "langgraph-api", "Assistants/Threads/Runs API", "Studio-compatible server" | `--runtime langgraph-server` (needs Postgres and Redis; the deployed `langgraph-api` image's licensing is unverified, so it is outside the disconnected profile; the local `langgraph dev` server needs no LangSmith key) |
| "LangGraph Studio", "visual graph debugger" | `graph-agents-cli playground --graph` (`langgraph dev`, bypasses auth) |
| "chat page", "try it in the browser" | `graph-agents-cli playground` (`/playground`, only when `APP_ENV=dev`) |
| "A2A", "agent-to-agent", "agent card" | built in: `/a2a/<agent_directory>/.well-known/agent-card.json`; `run --mode a2a` |

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

## Auth and outbound API access

| User says | CLI value |
|---|---|
| "API key", "bearer token", "shared secret", "internal tool" | `--auth-policy shared-bearer`; `API_KEY` in the Secret; clients send `Authorization: Bearer` |
| "OIDC", "SSO", "JWT", "access token", "per-user identity", "who owns the conversation" | `--auth-policy jwt` (per-user principals from a verified token: `AUTH_JWT_ISSUER`, `AUTH_JWT_AUDIENCE`, JWKS or public key) |
| "our app's session cookie", "existing roles", "custom header" | `--auth-policy custom` (stub fails closed until implemented in `app/policies/custom.py`); clients use `--header` or `--cookie` |
| "which endpoints may the agent call", "read-only", "GET only", "allow-list" | `api-policy.yaml` (`apis: <name>:` with `allowed_methods`, `allowed_operations`, `denied_operations`), seeded by `create --api-policy <file>` |
| "call our backend API", "backend client", "call the service as the user" | `get_client("<api>")` from `app/app_utils/api_client.py`, the only HTTP path to external APIs (`auth: none`, `bearer`, or `forward` for the caller's own credential); tools declare `API_CALLS` |

## Deployment

| User says | CLI value |
|---|---|
| "Kubernetes", "k8s", "our cluster", "on-prem", "self-hosted", "OpenShift", "RKE2", "kubeadm" | `--deployment-target kubernetes` (Helm chart in `deployment/helm/<name>/`) |
| "kind", "k3s", "k3d", "minikube", "Docker Desktop", "local cluster" | environment `dev` with local-load (image loaded into the node, no registry) |
| "prototype", "just locally", "no deployment yet" | `--prototype` (target `none`, `cd: skip`) |
| "GitOps", "Argo", "pull-based", "PR to deploy" | `--cd argocd` |
| "runner in our network", "push-based", "CI deploys" | `--cd helm-push` (self-hosted GitHub runner with a kubeconfig secret) |
| "CI only", "I'll deploy by hand" | `--cd skip` (direct modes) |
| "registry", "GHCR", "Harbor", "where do images go" | `--registry <url/org>` (default `ghcr.io/<org>`) |
| "ingress", "route", "hostname", "TLS" | chart values: `gateway` (Gateway API `HTTPRoute`, default) or `ingress`; `tls.existingSecret` or `tls.certManager` |
| "dev / staging / prod", "namespaces" | environments `dev`, `staging`, `prod`; namespace `<name>-<env>`; `values-<env>.yaml`; Secret `<name>-app` |
| "secrets", "rotate the key" | `secrets apply --env <env>` then `deploy --restart --env <env>` |
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
