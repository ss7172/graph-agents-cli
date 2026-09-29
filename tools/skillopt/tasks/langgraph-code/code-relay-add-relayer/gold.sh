cd warehouse-agent
graph-agents-cli api show shipping
graph-agents-cli api approval shipping --relayers dispatcher,returns_bot --dry-run
graph-agents-cli api approval shipping --relayers dispatcher,returns_bot
sed -i '' 's/^AUTH_ALLOWED_ACTORS=.*/AUTH_ALLOWED_ACTORS=dispatcher,returns_bot/' .env
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
The releaseShipment gate now lists both relayers: `relayers: [dispatcher, returns_bot]`
(`graph-agents-cli api approval shipping --relayers dispatcher,returns_bot`; the option replaces
the list, so dispatcher is named again). The requester still decides, with the same 30-minute
timeout. returns_bot is added to AUTH_ALLOWED_ACTORS in .env beside dispatcher; set it wherever the
agent runs too. The loosening goes through the reviewed pull request. Lint passes.
MD
