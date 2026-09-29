# A plausible mistake: a ServiceMonitor that does not send the token.
cd shop
python3 - <<'PY'
from pathlib import Path

p = Path("graph-agents-cli-manifest.yaml")
t = p.read_text()
old = "  keys: [OPENAI_API_KEY, JUDGE_API_KEY, POSTGRES_DSN, API_KEY, LANGSMITH_API_KEY]"
assert old in t
p.write_text(t.replace(old, old[:-1] + ", METRICS_TOKEN]"))
PY

cat >> deployment/helm/shop/values-prod.yaml <<'YAML'
metrics:
  serviceMonitor:
    enabled: true
YAML
echo "Enabled the ServiceMonitor." > "$GAC_FINAL"
