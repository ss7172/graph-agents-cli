# A plausible mistake: no scope, so the issuer's default (often broader) applies.
cd claims-bot
graph-agents-cli api add claims --base-url-env CLAIMS_API_URL --auth exchange --audience claims-api --access custom --methods GET,POST
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
Added the claims API with auth: exchange for claims-api.
MD
