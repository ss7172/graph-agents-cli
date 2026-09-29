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
    bearerToken:
      enabled: true
YAML
echo "METRICS_TOKEN added to secrets.keys; values-prod.yaml enables the ServiceMonitor with bearerToken. Put the token in .env.prod and run graph-agents-cli secrets apply --env prod." > "$GAC_FINAL"
