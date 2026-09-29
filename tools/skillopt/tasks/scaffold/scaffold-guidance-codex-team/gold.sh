graph-agents-cli create returns-agent --runtime fastapi --deployment-target kubernetes --checkpointer postgres --registry ghcr.io/acme-returns --cd helm-push --auth-policy shared-bearer --model-provider openai -y
cat > "$GAC_FINAL" <<'MD'
Created returns-agent (helm-push); guidance in AGENTS.md, which Codex reads.
MD
