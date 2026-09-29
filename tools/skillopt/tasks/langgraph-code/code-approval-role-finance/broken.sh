# A plausible mistake: the requester may approve too.
cd refund-desk
graph-agents-cli api approval payments --operations refundPayment --approvers requester,role:finance
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
Refunds need approval by the requester or finance.
MD
