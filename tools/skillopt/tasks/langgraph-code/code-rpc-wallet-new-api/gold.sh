cd wallet-helper
graph-agents-cli api add wallet_rpc --protocol jsonrpc --base-url-env WALLET_RPC_URL --auth bearer --token-env WALLET_RPC_TOKEN --access custom --methods POST --dry-run
graph-agents-cli api add wallet_rpc --protocol jsonrpc --base-url-env WALLET_RPC_URL --auth bearer --token-env WALLET_RPC_TOKEN --access custom --methods POST
graph-agents-cli api allow wallet_rpc --rpc-method wallet.balance --method POST --path /rpc
graph-agents-cli api allow wallet_rpc --rpc-method wallet.history --method POST --path /rpc
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
Declared wallet_rpc as a JSON-RPC API (`--protocol jsonrpc`, WALLET_RPC_URL, a bearer token from
WALLET_RPC_TOKEN, which is now in secrets.keys) and allowed only the two methods the tools send,
wallet.balance and wallet.history at POST /rpc, by rpc_method: the client reads each request's
method from its body, so no other method passes. The tools are unchanged; lint passes. Set
WALLET_RPC_URL and WALLET_RPC_TOKEN in .env.
MD
