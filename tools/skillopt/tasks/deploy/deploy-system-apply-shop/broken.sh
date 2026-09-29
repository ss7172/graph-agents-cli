# A plausible mistake: wire only the caller with peer add's defaults (/a2a/billing), and leave the
# called agent as it was.
cd shop/front-desk
graph-agents-cli peer add billing --description "Billing agent: invoices and refunds."
graph-agents-cli api check
cat > "$GAC_FINAL" <<'MD'
front-desk now calls billing through `graph-agents-cli peer add billing`.
MD
