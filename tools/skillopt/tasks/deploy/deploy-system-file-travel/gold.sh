cd travel
cat > graph-agents-system.yaml <<'YAML'
# The travel agents and the agents each one calls (graph-agents-cli system).
version: 1
name: travel
agents:
  itinerary:
    project: itinerary
    calls:
      - flights
      - {agent: hotels, approvals: deny}
  flights:
    project: flights-agent
  hotels:
    project: hotels-agent
identity:
  issuer: https://id.example.org
  token_url: {dev: "http://id.identity.svc.cluster.local/token"}
environments:
  dev: {}
YAML
graph-agents-cli system check || true
graph-agents-cli system apply --dry-run
graph-agents-cli system apply
graph-agents-cli system check || true
# SC04: the called agents must trust the issuer in dev.
python3 - <<'PY'
import pathlib, re
for name in ("flights-agent", "hotels-agent"):
    p = pathlib.Path(f"{name}/deployment/helm/{name}/values-dev.yaml")
    text = re.sub(r"^env:\n", "env:\n  AUTH_JWT_ISSUER: https://id.example.org\n", p.read_text(), count=1, flags=re.M)
    p.write_text(text)
PY
graph-agents-cli system check
(cd itinerary && graph-agents-cli api check)
cat > "$GAC_FINAL" <<'MD'
travel/graph-agents-system.yaml now describes the system: itinerary calls flights (the person's
approvals relayed) and hotels with `approvals: deny` (itinerary never approves anything there),
with the issuer https://id.example.org and its dev token endpoint. `graph-agents-cli system apply`
wrote both sides of both edges (the peers in itinerary at each agent's A2A mount /a2a/app, with a
token exchanged for the user's; the audiences, AUTH_ALLOWED_ACTORS itinerary and appUrl in the
called agents), and I set AUTH_JWT_ISSUER in both called agents' values-dev.yaml, so `system check`
reports no error. Still to do: register itinerary at the issuer (`system delegations`), its client
secret, and the relay line flights-agent's owners must review.
MD
