# A plausible mistake: a redeploy, which replaces no pod and never applies the Secret.
cd trends-agent
cat > "$GAC_FINAL" <<'MD'
Run graph-agents-cli deploy --env prod to pick up the new key.
MD
