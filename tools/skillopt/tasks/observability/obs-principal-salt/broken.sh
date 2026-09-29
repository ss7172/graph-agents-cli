# A plausible mistake: the salt as a chart value.
cd hr-agent
python3 - <<'PY'
from pathlib import Path

p = Path("deployment/helm/hr-agent/values.yaml")
p.write_text(p.read_text().replace('  PORT: "8000"\n', '  PORT: "8000"\n  PRINCIPAL_HASH_SALT: "change-me-salt"\n', 1))
PY
echo "Set PRINCIPAL_HASH_SALT." > "$GAC_FINAL"
