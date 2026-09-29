cd quote-service
graph-agents-cli deploy --env staging --dry-run --tag abc1234 || true
python3 - <<'PY'
from pathlib import Path

p = Path('deployment/helm/quote-service/values-staging.yaml')
t = p.read_text()
old = 'gateway:\n  enabled: true\n  hostname: ""\n  parentRef:\n    name: ""\n    namespace: ""\n'
assert old in t
p.write_text(t.replace(old, 'gateway:\n  enabled: false\ningress:\n  enabled: true\n  className: nginx\n  hostname: quotes.staging.acme.dev\n'))
PY
graph-agents-cli deploy --env staging --dry-run --tag abc1234
cat > "$GAC_FINAL" <<'MD'
values-staging.yaml: gateway off, ingress on (class nginx, host quotes.staging.acme.dev). `graph-agents-cli deploy --env staging --dry-run` renders the Ingress; nothing was deployed.
MD
