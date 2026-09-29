# A plausible mistake: copy the generic example's --agent-guidance-filename CLAUDE.md.
graph-agents-cli create kb-search --runtime langgraph-server --deployment-target kubernetes --checkpointer postgres --registry registry.corp.example/search --cd argocd --auth-policy jwt --model-provider anthropic --agent-guidance-filename CLAUDE.md -y
cat > "$GAC_FINAL" <<'MD'
Created kb-search.
MD
