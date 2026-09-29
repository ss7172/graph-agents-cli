cd ledger-agent
graph-agents-cli api add ledger --base-url-env LEDGER_API_URL --auth exchange --audience ledger --resource https://ledger.internal.example --access read-only --dry-run
graph-agents-cli api add ledger --base-url-env LEDGER_API_URL --auth exchange --audience ledger --resource https://ledger.internal.example --access read-only
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
Added the ledger API, read-only, with `auth: exchange` for the audience ledger and the resource
https://ledger.internal.example: each call carries a token the issuer mints for the ledger in the
user's name. TOKEN_EXCHANGE_CLIENT_SECRET is in secrets.keys. Still to set: LEDGER_API_URL and the
token-exchange settings. Lint passes.
MD
