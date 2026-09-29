# A plausible mistake: the API limit alone (runs hit the step limit first).
cd stock-watch
graph-agents-cli api limits inventory --max-calls-per-run 30
cat > "$GAC_FINAL" <<'MD'
Limited inventory calls to 30 per run.
MD
