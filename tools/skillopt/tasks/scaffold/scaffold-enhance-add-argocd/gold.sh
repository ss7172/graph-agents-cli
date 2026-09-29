cd shop-agent
graph-agents-cli scaffold enhance . --deployment-target kubernetes --checkpointer postgres --registry ghcr.io/acme --cd argocd -y
graph-agents-cli lint
echo "Enhanced shop-agent: kubernetes + postgres + argocd, registry ghcr.io/acme; agent code and eval data unchanged; lint passes." > "$GAC_FINAL"
