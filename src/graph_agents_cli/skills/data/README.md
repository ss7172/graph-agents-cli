# graph-agents-cli skills

Development skills for building [LangGraph](https://langchain-ai.github.io/langgraph/) agents with
graph-agents-cli and deploying them to self-hosted Kubernetes. Install into any coding agent with
`graph-agents-cli setup` or [`npx skills`](https://github.com/vercel-labs/skills).

The copy under `src/graph_agents_cli/skills/data/` is byte-identical to this directory and ships
in the wheel so `setup` works without network.

## Skills

| Skill | Description |
|-------|-------------|
| `graph-agents-cli-workflow` | Development lifecycle (understand, scaffold, build, evaluate, deploy, observe), process deference, code preservation, model rule, approval before deploy, loop breaker |
| `graph-agents-cli-langgraph-code` | LangGraph and LangChain patterns the template uses: create_agent and StateGraph, tools with API_CALLS, checkpointers, streaming, interrupts, subgraphs, init_chat_model, the auth policy adapter, the API client |
| `graph-agents-cli-scaffold` | `create`, `scaffold enhance`, `scaffold upgrade`, every flag, the combination table, upgrade guarantees |
| `graph-agents-cli-eval` | The enforceable eval gate, dataset schema, expect checks, judge and quality metrics, exit codes, `eval submit` |
| `graph-agents-cli-deploy` | Deployment modes, environments, secrets and rotation, Argo CD PR flow, GitHub settings, `infra check`, local-load clusters, disconnected profile |
| `graph-agents-cli-observability` | `TRACING_ENABLED` opt-in, `TRACE_CAPTURE`, LangSmith versus OTLP, hashed principal id, run records |
