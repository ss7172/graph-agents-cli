# A plausible mistake: the default endpoint (/a2a/billing) instead of the one the peer serves.
cd support-desk
graph-agents-cli peer add billing --description "Billing agent: invoices and refunds; a refund waits for the person's approval."
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
Added the billing agent with `graph-agents-cli peer add billing`; lint passes.
MD
