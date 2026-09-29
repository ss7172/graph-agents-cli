# A plausible mistake: build straight away.
graph-agents-cli create merge-digest --prototype -y
cat > "$GAC_FINAL" <<'MD'
Created merge-digest; next I will add GitHub and Slack tools.
MD
