cd market-agent
graph-agents-cli deploy --env prod --dry-run --tag abc1234 || true
python3 - <<'PY'
from pathlib import Path

p = Path('deployment/helm/market-agent/values-prod.yaml')
t = p.read_text()
old = 'env:\n  APP_ENV: prod\n'
assert old in t
p.write_text(t.replace(old, old + '  PAYMENTS_API_BASE_URL: https://payments.acme.com\n', 1))
PY
graph-agents-cli deploy --env prod --dry-run --tag abc1234
cat > "$GAC_FINAL" <<'MD'
Exit 3 was the CHANGE-ME placeholder for PAYMENTS_API_BASE_URL; set it in values-prod.yaml. The prod dry run passes.
MD
