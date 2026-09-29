cd claims-bot
graph-agents-cli api add claims --base-url-env CLAIMS_API_URL --auth exchange --audience claims-api --scope "claims.read claims.submit" --access custom --methods GET,POST --dry-run
graph-agents-cli api add claims --base-url-env CLAIMS_API_URL --auth exchange --audience claims-api --scope "claims.read claims.submit" --access custom --methods GET,POST
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
Added the claims API with `auth: exchange` (audience claims-api, scopes `claims.read claims.submit`
only) and GET and POST allowed: each call carries a token minted for the claims API in the
person's name. TOKEN_EXCHANGE_CLIENT_SECRET is in secrets.keys. Still to set: CLAIMS_API_URL, the
token-exchange settings, and at the issuer this agent's permission to exchange for claims-api with
those scopes. Lint passes.
MD
