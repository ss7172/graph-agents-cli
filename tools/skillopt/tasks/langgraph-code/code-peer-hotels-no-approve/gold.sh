cd trip-planner
graph-agents-cli peer add hotels --approvals deny --description "Hotels agent: room availability for the user's trips." --dry-run
graph-agents-cli peer add hotels --approvals deny --description "Hotels agent: room availability for the user's trips."
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
Added the hotels agent as a peer with `graph-agents-cli peer add hotels --approvals deny`: the API
hotels_agent (protocol a2a, /a2a/hotels, HOTELS_AGENT_URL, a token exchanged for the user's,
audience hotels) may receive questions (SendMessage) and status reads (GetTask), and every message
that would approve something at the hotels agent is denied, so people approve bookings there
themselves. tools/a2a_peers.py was generated; lint passes. Still to set: HOTELS_AGENT_URL, the
token-exchange settings (TOKEN_EXCHANGE_URL, TOKEN_EXCHANGE_CLIENT_ID, the client secret), and on
the hotels agent AUTH_ALLOWED_ACTORS with this agent's client id.
MD
