# A plausible mistake: the endpoint in the shared values (every environment).
cd catalog-api
python3 - <<'PY'
from pathlib import Path

p = Path('deployment/helm/catalog-api/values.yaml')
t = p.read_text()
t = t.replace('  PORT: "8000"\n', '  PORT: "8000"\n  LANGSMITH_ENDPOINT: https://langsmith.acme.internal/api\n', 1)
t = t.replace('tracing:\n  enabled: false', 'tracing:\n  enabled: true', 1).replace('project: "catalog-api"', 'project: "catalog-staging"', 1)
p.write_text(t)
PY
cat > "$GAC_FINAL" <<'MD'
Tracing enabled to the self-hosted LangSmith.
MD
