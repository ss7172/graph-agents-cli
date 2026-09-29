cd catalog-api
python3 - <<'PY'
from pathlib import Path

p = Path('deployment/helm/catalog-api/values-staging.yaml')
t = p.read_text()
assert 'env:\n  APP_ENV: staging\n' in t
t = t.replace('env:\n  APP_ENV: staging\n', 'env:\n  APP_ENV: staging\n  LANGSMITH_ENDPOINT: https://langsmith.acme.internal/api\n', 1)
t += 'tracing:\n  enabled: true\n  capture: metadata\n  langsmith:\n    project: catalog-staging\n'
p.write_text(t)
PY
cat > "$GAC_FINAL" <<'MD'
values-staging.yaml: tracing on (metadata), LangSmith project catalog-staging, env.LANGSMITH_ENDPOINT https://langsmith.acme.internal/api. Put LANGSMITH_API_KEY in .env.staging, run graph-agents-cli secrets apply --env staging, then deploy staging.
MD
