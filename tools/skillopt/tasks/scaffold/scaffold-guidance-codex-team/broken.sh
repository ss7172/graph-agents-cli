# A plausible mistake: the guidance file of the agent running the command.
graph-agents-cli create returns-agent --runtime fastapi --deployment-target kubernetes --checkpointer postgres --registry ghcr.io/acme-returns --cd helm-push --auth-policy shared-bearer --model-provider openai --agent-guidance-filename CLAUDE.md -y
cat > "$GAC_FINAL" <<'MD'
Created returns-agent.
MD
