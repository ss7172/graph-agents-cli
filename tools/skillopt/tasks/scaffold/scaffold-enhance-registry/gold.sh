cd inventory-agent
graph-agents-cli build --dry-run || true
graph-agents-cli scaffold enhance --registry registry.example.com/platform -y
graph-agents-cli build --dry-run
echo "Set the registry with scaffold enhance --registry (manifest, values.yaml, .github/agent.env); build --dry-run passes." > "$GAC_FINAL"
