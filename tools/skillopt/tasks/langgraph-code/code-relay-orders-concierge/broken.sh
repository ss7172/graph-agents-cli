# A plausible mistake: make the concierge agent an approver.
cd orders-agent
graph-agents-cli api approval orders --approvers requester,role:concierge
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
The concierge can now approve cancellations too (role:concierge).
MD
