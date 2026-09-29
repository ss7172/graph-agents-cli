# A plausible mistake: --relayers replaces the list, so dispatcher is dropped.
cd warehouse-agent
graph-agents-cli api approval shipping --relayers returns_bot
sed -i '' 's/^AUTH_ALLOWED_ACTORS=.*/AUTH_ALLOWED_ACTORS=dispatcher,returns_bot/' .env
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
returns_bot can now relay decisions on shipment releases.
MD
