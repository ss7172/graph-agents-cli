graph-agents-cli create catalog-agent --prototype --model-provider openai --agent-guidance-filename GEMINI.md -y
cat > "$GAC_FINAL" <<'MD'
Created the prototype catalog-agent; guidance in GEMINI.md for Gemini CLI.
MD
