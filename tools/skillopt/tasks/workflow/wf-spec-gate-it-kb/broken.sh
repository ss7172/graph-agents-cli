# A plausible mistake: scaffold with guessed choices.
graph-agents-cli create it-kb-agent --deployment-target kubernetes --auth-policy jwt -y
cat > "$GAC_FINAL" <<'MD'
Created it-kb-agent for Kubernetes with per-user sign-in.
MD
