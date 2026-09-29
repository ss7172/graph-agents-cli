# A plausible mistake: list both agents by name, so hotels gets the default (approvals relayed).
cd travel
cat > graph-agents-system.yaml <<'YAML'
version: 1
name: travel
agents:
  itinerary:
    project: itinerary
    calls: [flights, hotels]
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
graph-agents-cli system apply
python3 - <<'PY'
import pathlib, re
for name in ("flights-agent", "hotels-agent"):
    p = pathlib.Path(f"{name}/deployment/helm/{name}/values-dev.yaml")
    text = re.sub(r"^env:\n", "env:\n  AUTH_JWT_ISSUER: https://id.example.org\n", p.read_text(), count=1, flags=re.M)
    p.write_text(text)
PY
graph-agents-cli system check
cat > "$GAC_FINAL" <<'MD'
Wrote graph-agents-system.yaml (itinerary calls flights and hotels) and applied it.
MD
