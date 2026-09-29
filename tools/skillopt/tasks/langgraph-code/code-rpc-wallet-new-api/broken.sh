# A plausible mistake: allow the endpoint by path, as for an HTTP API; every method passes there.
cd wallet-helper
graph-agents-cli api add wallet_rpc --protocol jsonrpc --base-url-env WALLET_RPC_URL --auth bearer --token-env WALLET_RPC_TOKEN --access custom --methods POST
graph-agents-cli api allow wallet_rpc --method POST --path /rpc
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
Declared wallet_rpc (JSON-RPC) and allowed POST /rpc; lint passes.
MD
