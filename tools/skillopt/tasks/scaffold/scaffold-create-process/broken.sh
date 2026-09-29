# A plausible mistake: a note in the guidance file instead of --process.
graph-agents-cli create claims-agent --prototype -y
printf '\nFollow docs/delivery-process.md for all work.\n' >> claims-agent/AGENTS.md
echo "Created claims-agent and noted the process in AGENTS.md." > "$GAC_FINAL"
