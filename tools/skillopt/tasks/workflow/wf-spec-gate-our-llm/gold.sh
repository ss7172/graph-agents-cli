cat > .graph-agents-cli-spec.md <<'MD'
# Wiki guide agent: draft spec (NOT approved)

- Purpose: answer new hires' questions from the internal wiki.
- Model: "our LLM" -- open: which provider or on-network endpoint and model?
- Wiki access: open (API or export, credentials, read-only).
MD
cat > "$GAC_FINAL" <<'MD'
Nothing is scaffolded yet; I need your answers first:
1. Which LLM is "ours": a hosted provider (OpenAI, Anthropic, Gemini), or an
   on-network OpenAI-compatible server (vLLM, Ollama)? If on-network, its base URL
   and model name?
2. How should the agent read the wiki (which API or export, read-only, which
   credential)?
3. Who will ask it questions, and how do they sign in?
4. Prototype first, or Kubernetes?
I drafted .graph-agents-cli-spec.md for you to approve.
MD
