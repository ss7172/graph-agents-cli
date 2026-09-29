cd shop
graph-agents-cli deploy --env staging --dry-run --tag abc1234 || true
python3 - <<'PY'
from pathlib import Path

p = Path("deployment/helm/shop/values-staging.yaml")
t = p.read_text()
old = "env:\n  APP_ENV: staging\n"
assert old in t
p.write_text(t.replace(old, old + "  INVENTORY_API_BASE_URL: https://inventory.staging.acme.internal\n", 1))
PY
graph-agents-cli deploy --env staging --dry-run --tag abc1234
echo "Exit 3 was the CHANGE-ME placeholder for INVENTORY_API_BASE_URL; set it in values-staging.yaml. The staging dry run passes." > "$GAC_FINAL"
