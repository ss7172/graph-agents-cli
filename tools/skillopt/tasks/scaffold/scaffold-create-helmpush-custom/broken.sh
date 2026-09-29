# A plausible mistake: jwt for callers who already carry another credential.
graph-agents-cli create orders-agent --runtime fastapi --deployment-target kubernetes --checkpointer postgres \
  --registry ghcr.io/acme-shop --cd helm-push --auth-policy jwt --model-provider openai -y
echo "Created orders-agent." > "$GAC_FINAL"
