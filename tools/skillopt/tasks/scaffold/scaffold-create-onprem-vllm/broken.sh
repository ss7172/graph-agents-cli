# A plausible mistake: the hosted OpenAI provider with the vLLM model name.
graph-agents-cli create netops-agent --model-provider openai --model qwen2.5:14b --runtime fastapi --deployment-target kubernetes --registry harbor.corp.internal/agents --cd skip -y
cat > "$GAC_FINAL" <<'MD'
Created netops-agent with qwen2.5:14b.
MD
