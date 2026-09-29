# A plausible mistake: forward the user's own token, restricted by audience.
cd hr-assistant
graph-agents-cli api add leave --base-url-env LEAVE_API_URL --auth forward --audience leave-service --access read-only
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
Added the leave API; it forwards the user's token when it is for leave-service.
MD
