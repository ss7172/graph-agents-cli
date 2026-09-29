cd trends-agent
graph-agents-cli secrets apply --env prod --dry-run || true
cat > "$GAC_FINAL" <<'MD'
Nothing in the repository changes (LANGSMITH_API_KEY is already in secrets.keys). Run:
1. graph-agents-cli secrets apply --env prod
2. graph-agents-cli deploy --restart --env prod  (pods read the Secret only at start)
3. graph-agents-cli secrets status --env prod to confirm.
MD
