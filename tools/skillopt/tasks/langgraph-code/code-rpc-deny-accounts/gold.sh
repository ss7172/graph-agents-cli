cd account-desk
graph-agents-cli api deny accounts_rpc --rpc-method account.close --dry-run
graph-agents-cli api deny accounts_rpc --rpc-method account.close
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
The accounts_rpc API now denies the JSON-RPC method account.close by its method alone (read from
each request's body), whatever the path or label. Nothing else changed; lint passes.
MD
