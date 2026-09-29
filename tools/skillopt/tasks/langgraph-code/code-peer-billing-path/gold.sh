cd support-desk
graph-agents-cli peer add billing --path /a2a/invoices --description "Billing agent: invoices and refunds; a refund waits for the person's approval." --dry-run
graph-agents-cli peer add billing --path /a2a/invoices --description "Billing agent: invoices and refunds; a refund waits for the person's approval."
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
Added the billing agent with `graph-agents-cli peer add billing --path /a2a/invoices`: the API
billing_agent (protocol a2a, endpoint /a2a/invoices, BILLING_AGENT_URL, a token exchanged for the
user's), with the approve gate for the requester, so a refund at billing is approved by the
person through this agent and relayed, never decided by the agent itself. tools/a2a_peers.py was
generated; lint passes. Still to set: BILLING_AGENT_URL, the token-exchange settings, and on the
billing agent AUTH_ALLOWED_ACTORS plus the `graph-agents-cli api approval <its gated API>
--decide-with relayed --relayers <this agent's client id>` line its owners must review.
MD
