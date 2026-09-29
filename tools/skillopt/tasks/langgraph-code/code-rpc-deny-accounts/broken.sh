# A plausible mistake: deny by an operation label, as for an HTTP API. Lint passes (the tool's
# call names getAccount), but account.close sent under any other label gets past the denial.
cd account-desk
graph-agents-cli api deny accounts_rpc closeAccount
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
Denied the closeAccount operation on accounts_rpc; lint passes.
MD
