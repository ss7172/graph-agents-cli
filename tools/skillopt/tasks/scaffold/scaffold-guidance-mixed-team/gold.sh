graph-agents-cli create pricing-agent --prototype --model-provider anthropic -y
cat > "$GAC_FINAL" <<'MD'
Created the prototype pricing-agent (Anthropic, default model). The agent guidance is in AGENTS.md, which Codex, Cursor and Claude Code all read.
MD
