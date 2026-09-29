cd helpdesk-agent
graph-agents-cli eval run
cat > "$GAC_FINAL" <<'MD'
`graph-agents-cli eval run`: 4 passed, 0 failed, 0 error, 0 missing; exit code 0 (gate met).
But the agent and the judge ran on the deterministic fake model, so this proves the eval plumbing
only; it is not a quality signal. Run the eval on the real provider before deciding to ship.
MD
