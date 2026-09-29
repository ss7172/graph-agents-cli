graph-agents-cli create quote-agent --prototype --model-provider gemini --agent-guidance-filename CLAUDE.md -y
cat > "$GAC_FINAL" <<'MD'
Created the prototype quote-agent (Gemini); guidance in CLAUDE.md for your Claude Code team.
MD
