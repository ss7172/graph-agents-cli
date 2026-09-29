# A plausible mistake: the Gateway without the certificate.
cd portal-agent
python3 - <<'PY'
from pathlib import Path

p = Path('deployment/helm/portal-agent/values-prod.yaml')
t = p.read_text()
t = t.replace('hostname: ""', 'hostname: "agents.acme.com"', 1).replace('    name: ""', '    name: "public-gw"', 1).replace('    namespace: ""', '    namespace: "gateways"', 1)
p.write_text(t)
PY
graph-agents-cli deploy --env prod --dry-run --tag abc1234
cat > "$GAC_FINAL" <<'MD'
Attached prod to public-gw with agents.acme.com.
MD
