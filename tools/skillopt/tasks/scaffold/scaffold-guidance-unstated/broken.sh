# A plausible mistake: copy the generic example's --agent-guidance-filename CLAUDE.md.
graph-agents-cli create billing-agent --runtime fastapi --deployment-target kubernetes --checkpointer postgres --registry registry.acme.io/ml --cd skip --auth-policy shared-bearer --model-provider openai --agent-guidance-filename CLAUDE.md -y
cat > "$GAC_FINAL" <<'MD'
Created billing-agent.
MD
