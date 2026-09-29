graph-agents-cli create billing-agent --runtime fastapi --deployment-target kubernetes --checkpointer postgres --registry registry.acme.io/ml --cd skip --auth-policy shared-bearer --model-provider openai -y
cat > "$GAC_FINAL" <<'MD'
Created billing-agent (fastapi, kubernetes, postgres, registry.acme.io/ml, no CD, shared bearer, OpenAI); guidance in AGENTS.md.
MD
