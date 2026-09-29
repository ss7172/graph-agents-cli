graph-agents-cli create support-bot --runtime langgraph-server --deployment-target kubernetes \
  --checkpointer postgres --registry harbor.acme.internal/agents --cd argocd --auth-policy jwt \
  --model-provider anthropic --agent-guidance-filename CLAUDE.md -y
echo "Created support-bot (langgraph-server, postgres, kubernetes, argocd, jwt, anthropic, CLAUDE.md)." > "$GAC_FINAL"
