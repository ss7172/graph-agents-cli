# A plausible mistake: fix the shared values.yaml, which every environment reads.
cd shop
python3 - <<'PY'
from pathlib import Path

p = Path("deployment/helm/shop/values.yaml")
p.write_text(p.read_text().replace("http://CHANGE-ME", "https://inventory.staging.acme.internal"))
PY
graph-agents-cli deploy --env staging --dry-run --tag abc1234 || true
echo "Set the inventory URL." > "$GAC_FINAL"
