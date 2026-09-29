# A plausible mistake: the guidance file of the agent running the command.
graph-agents-cli create pricing-agent --prototype --model-provider anthropic --agent-guidance-filename CLAUDE.md -y
cat > "$GAC_FINAL" <<'MD'
Created pricing-agent with CLAUDE.md.
MD
