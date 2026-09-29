# A plausible mistake: the resource indicator given as the audience.
cd ledger-agent
graph-agents-cli api add ledger --base-url-env LEDGER_API_URL --auth exchange --audience https://ledger.internal.example --access read-only
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
Added the ledger API with auth: exchange.
MD
