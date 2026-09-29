# A plausible mistake: the ingress in the shared values.yaml (staging keeps its gateway).
cd quote-service
python3 - <<'PY'
from pathlib import Path

p = Path('deployment/helm/quote-service/values.yaml')
t = p.read_text()
t = t.replace('ingress:\n  enabled: false\n  className: ""\n  hostname: ""', 'ingress:\n  enabled: true\n  className: "nginx"\n  hostname: "quotes.staging.acme.dev"', 1)
p.write_text(t)
PY
graph-agents-cli deploy --env staging --dry-run --tag abc1234 || true
cat > "$GAC_FINAL" <<'MD'
Enabled the nginx ingress for quotes.staging.acme.dev.
MD
