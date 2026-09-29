cd payments-desk
graph-agents-cli api approval payments --operations refundPayment --approvers requester --dry-run
graph-agents-cli api approval payments --operations refundPayment --approvers requester
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
api-policy.yaml now gates refundPayment with an approval by the requester: the run pauses before the refund is sent until the user approves it. No tool code changed; lint passes.
MD
