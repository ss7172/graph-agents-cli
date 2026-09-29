cd payouts-agent
python3 - <<'PY'
from pathlib import Path

p = Path('deployment/helm/payouts-agent/values-prod.yaml')
t = p.read_text()
assert 'env:\n  APP_ENV: prod\n' in t
p.write_text(t.replace('env:\n  APP_ENV: prod\n', 'env:\n  APP_ENV: prod\n  LOG_LEVEL: WARNING\n', 1))
PY
cat > "$GAC_FINAL" <<'MD'
values-prod.yaml now sets env.LOG_LEVEL: WARNING (other environments unchanged). It takes effect with the next `graph-agents-cli deploy --env prod`: the ConfigMap change rolls the pods.
MD
