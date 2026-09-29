cd courier-agent
cat > "$GAC_FINAL" <<'MD'
Nothing in the repository changes, and CI never holds the key. From a workstation with
access to the prod cluster:
1. graph-agents-cli secrets apply --env prod
2. graph-agents-cli deploy --restart --env prod   (pods read the Secret only when they start)
3. graph-agents-cli secrets status --env prod
MD
