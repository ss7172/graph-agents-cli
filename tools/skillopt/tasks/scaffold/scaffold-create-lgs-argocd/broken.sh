# A plausible mistake: the guidance file stays AGENTS.md although the team uses Claude Code.
graph-agents-cli create support-bot --runtime langgraph-server --deployment-target kubernetes \
  --checkpointer postgres --registry harbor.acme.internal/agents --cd argocd --auth-policy jwt \
  --model-provider anthropic -y
echo "Created support-bot." > "$GAC_FINAL"
