# A plausible mistake: the default guidance file for a Gemini-only team.
graph-agents-cli create catalog-agent --prototype --model-provider openai -y
cat > "$GAC_FINAL" <<'MD'
Created catalog-agent.
MD
