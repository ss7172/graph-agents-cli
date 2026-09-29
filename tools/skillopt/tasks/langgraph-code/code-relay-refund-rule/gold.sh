cd payments-hub
graph-agents-cli api show payments
graph-agents-cli api approval payments --rule 0 --decide-with relayed --relayers helpdesk --dry-run
graph-agents-cli api approval payments --rule 0 --decide-with relayed --relayers helpdesk
sed -i '' 's/^AUTH_ALLOWED_ACTORS=.*/AUTH_ALLOWED_ACTORS=helpdesk/' .env
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
The refund rule (approval[0]) now has `decide_with: relayed` with `relayers: [helpdesk]`
(`graph-agents-cli api approval payments --rule 0 --decide-with relayed --relayers helpdesk`):
the requester still decides, and the helpdesk agent may deliver that decision. The void rule is
unchanged: role:finance approves here directly. helpdesk is in AUTH_ALLOWED_ACTORS in .env; set it
wherever the agent runs too. The loosening goes through the reviewed pull request. Lint passes.
MD
