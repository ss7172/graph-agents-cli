cd refund-desk
graph-agents-cli api approval payments --operations refundPayment --approvers role:finance
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
refundPayment now waits for an approver holding role finance (not the requester); lint passes.
MD
