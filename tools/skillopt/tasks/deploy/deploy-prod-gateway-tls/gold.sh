cd portal-agent
python3 - <<'PY'
from pathlib import Path

p = Path('deployment/helm/portal-agent/values-prod.yaml')
t = p.read_text()
t = t.replace('hostname: ""', 'hostname: "agents.acme.com"', 1).replace('    name: ""', '    name: "public-gw"', 1).replace('    namespace: ""', '    namespace: "gateways"', 1)
t += 'tls:\n  certManager:\n    enabled: true\n    issuerRef:\n      name: letsencrypt-prod\n      kind: ClusterIssuer\n'
p.write_text(t)
PY
graph-agents-cli deploy --env prod --dry-run --tag abc1234
cat > "$GAC_FINAL" <<'MD'
values-prod.yaml: Gateway public-gw/gateways, host agents.acme.com, cert-manager TLS from letsencrypt-prod. The prod dry run renders the HTTPRoute and the Certificate.
MD
