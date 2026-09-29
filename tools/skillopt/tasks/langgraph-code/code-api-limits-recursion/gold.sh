cd stock-watch
graph-agents-cli api limits inventory --max-calls-per-run 30
printf 'RECURSION_LIMIT=62\n' >> .env
cat > "$GAC_FINAL" <<'MD'
api-policy.yaml limits inventory to 30 calls per run; RECURSION_LIMIT=62 in .env (2 x 30 + 2), since the default 50 steps fit only 24 sequential tool calls.
MD
