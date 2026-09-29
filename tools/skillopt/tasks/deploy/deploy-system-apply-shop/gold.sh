cd shop
graph-agents-cli system check || true
graph-agents-cli system apply --dry-run
graph-agents-cli system apply
graph-agents-cli system check || true
# SC04: the called agent must trust the file's issuer in dev.
python3 - <<'PY'
import pathlib, re
p = pathlib.Path("billing-agent/deployment/helm/billing-agent/values-dev.yaml")
text = p.read_text()
text = re.sub(r"^env:\n", "env:\n  AUTH_JWT_ISSUER: https://issuer.example.com\n", text, count=1, flags=re.M)
p.write_text(text)
PY
graph-agents-cli system check
(cd front-desk && graph-agents-cli api check)
cat > "$GAC_FINAL" <<'MD'
Wired both sides of frontdesk -> billing from graph-agents-system.yaml with `graph-agents-cli
system apply`: front-desk got the billing_agent peer at billing-agent's A2A endpoint /a2a/app (a
token exchanged for the user's, audience billing), BILLING_AGENT_URL, TOKEN_EXCHANGE_URL and its
client id for dev, and tools/a2a_peers.py; billing-agent got AUTH_JWT_AUDIENCE billing,
AUTH_ALLOWED_ACTORS frontdesk and its appUrl for dev. I also set AUTH_JWT_ISSUER to
https://issuer.example.com in billing-agent's values-dev.yaml (system check SC04). `system check`
now reports no error. Still to do: register frontdesk at the issuer (`graph-agents-cli system
delegations`), put TOKEN_EXCHANGE_CLIENT_SECRET in front-desk's secrets, and review the
`api approval ... --decide-with relayed --relayers frontdesk` line billing-agent's owners must run
before a relay reaches its gates.
MD
