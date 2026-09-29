graph-agents-cli create kb-search --runtime langgraph-server --deployment-target kubernetes --checkpointer postgres --registry registry.corp.example/search --cd argocd --auth-policy jwt --model-provider anthropic -y
cat > "$GAC_FINAL" <<'MD'
Created kb-search (langgraph-server, postgres, kubernetes, argocd, jwt, Anthropic); guidance in AGENTS.md.
MD
