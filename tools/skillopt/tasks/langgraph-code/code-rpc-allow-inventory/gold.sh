cd stock-bot
graph-agents-cli api allow inventory_rpc --rpc-method item.get --method POST --path /rpc --dry-run
graph-agents-cli api allow inventory_rpc --rpc-method item.get --method POST --path /rpc
graph-agents-cli api allow inventory_rpc --rpc-method item.search --method POST --path /rpc
graph-agents-cli api deny inventory_rpc --rpc-method item.delete
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
The inventory_rpc API now allows only item.get and item.search at POST /rpc (allowed_operations by
rpc_method, read from each request's body), and denies item.delete by its JSON-RPC method alone,
whatever the path or label. The tools are unchanged; lint passes.
MD
