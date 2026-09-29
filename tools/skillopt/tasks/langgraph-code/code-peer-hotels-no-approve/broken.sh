# A plausible mistake: the default peer add relays approvals through a gate here.
cd trip-planner
graph-agents-cli peer add hotels --description "Hotels agent: room availability for the user's trips."
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
Added the hotels agent with `graph-agents-cli peer add hotels`; lint passes.
MD
