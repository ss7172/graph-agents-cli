# A plausible mistake: an env entry in the chart values instead of the Secret allowlist.
cd shop
python3 - <<'PY'
from pathlib import Path

p = Path("deployment/helm/shop/values.yaml")
t = p.read_text()
p.write_text(t.replace('  PORT: "8000"\n', '  PORT: "8000"\n  WEATHER_API_TOKEN: ""\n', 1))
PY
echo "Added WEATHER_API_TOKEN to the chart env." > "$GAC_FINAL"
