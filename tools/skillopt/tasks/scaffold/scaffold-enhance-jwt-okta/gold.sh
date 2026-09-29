cd tickets-agent
graph-agents-cli scaffold enhance . --auth-policy jwt -y
cat > "$GAC_FINAL" <<'MD'
Switched tickets-agent to the jwt policy with scaffold enhance. Set AUTH_JWT_JWKS_URL, AUTH_JWT_ISSUER and AUTH_JWT_AUDIENCE from your Okta tenant; locally, graph-agents-cli auth dev-token --sub <user> gives a test token.
MD
