cd hr-assistant
graph-agents-cli api add leave --base-url-env LEAVE_API_URL --auth exchange --audience leave-service --access read-only --description "Leave service: people's leave balances." --dry-run
graph-agents-cli api add leave --base-url-env LEAVE_API_URL --auth exchange --audience leave-service --access read-only --description "Leave service: people's leave balances."
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
Added the leave API with `auth: exchange` for the audience leave-service, read-only
(`graph-agents-cli api add leave --auth exchange --audience leave-service --access read-only`): each
call carries a token the issuer mints for the leave service in the user's name, never a shared key
or the user's own token. TOKEN_EXCHANGE_CLIENT_SECRET is now in secrets.keys. Still to set:
LEAVE_API_URL, TOKEN_EXCHANGE_URL and TOKEN_EXCHANGE_CLIENT_ID, the client secret, and at the issuer
the permission for this agent's client to exchange users' tokens for leave-service. Lint passes.
MD
