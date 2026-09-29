# A plausible mistake: the guidance file of the agent running the command.
graph-agents-cli create survey-bot --prototype --model-provider openai --agent-guidance-filename CLAUDE.md -y
cat > "$GAC_FINAL" <<'MD'
Created survey-bot.
MD
