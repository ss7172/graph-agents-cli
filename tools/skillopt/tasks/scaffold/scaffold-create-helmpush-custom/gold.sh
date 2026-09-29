graph-agents-cli create orders-agent --runtime fastapi --deployment-target kubernetes --checkpointer postgres \
  --registry ghcr.io/acme-shop --cd helm-push --auth-policy custom --model-provider openai -y
echo "Created orders-agent (helm-push, custom auth policy stub to implement in app/policies/custom.py)." > "$GAC_FINAL"
