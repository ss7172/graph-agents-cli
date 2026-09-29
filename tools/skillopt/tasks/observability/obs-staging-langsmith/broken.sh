# A plausible mistake: the shared values.yaml turns tracing on in every environment.
cd shop
python3 - <<'PY'
from pathlib import Path

p = Path("deployment/helm/shop/values.yaml")
t = p.read_text()
t = t.replace("tracing:\n  enabled: false", "tracing:\n  enabled: true", 1).replace('project: "shop"', 'project: "shop-staging"', 1)
p.write_text(t)
PY
echo "Tracing enabled." > "$GAC_FINAL"
