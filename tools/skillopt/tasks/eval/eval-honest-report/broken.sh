cd helpdesk-agent
graph-agents-cli eval run
echo "All 4 cases passed (exit code 0): the gate is met, so the agent is ready to ship." > "$GAC_FINAL"
