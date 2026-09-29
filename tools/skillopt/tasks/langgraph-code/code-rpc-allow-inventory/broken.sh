# A plausible mistake: allow the endpoint by path, as for an HTTP API.
cd stock-bot
graph-agents-cli api allow inventory_rpc --method POST --path /rpc
graph-agents-cli api deny inventory_rpc --rpc-method item.delete
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
Allowed POST /rpc and denied item.delete.
MD
