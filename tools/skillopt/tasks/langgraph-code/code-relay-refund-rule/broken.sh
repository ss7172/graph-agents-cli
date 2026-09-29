# A plausible mistake: fold both calls into one relayed rule.
cd payments-hub
graph-agents-cli api approval payments --remove
graph-agents-cli api approval payments --operations refundPayment,voidPayment --approvers requester --decide-with relayed --relayers helpdesk
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
Refunds and voids are now approved by the requester, relayed by helpdesk.
MD
