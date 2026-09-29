# A plausible mistake: guess the provider and scaffold.
graph-agents-cli create wiki-guide --prototype --model-provider openai-compatible -y
cat > "$GAC_FINAL" <<'MD'
Created wiki-guide on an OpenAI-compatible provider; set OPENAI_BASE_URL to your LLM.
MD
