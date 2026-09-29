# A plausible mistake: ignore the team's single agent.
graph-agents-cli create quote-agent --prototype --model-provider gemini -y
cat > "$GAC_FINAL" <<'MD'
Created quote-agent.
MD
