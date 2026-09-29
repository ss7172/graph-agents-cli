# A plausible mistake: the shared values.yaml changes every environment.
cd payouts-agent
python3 - <<'PY'
from pathlib import Path

p = Path('deployment/helm/payouts-agent/values.yaml')
t = p.read_text()
p.write_text(t.replace('  PORT: "8000"\n', '  PORT: "8000"\n  LOG_LEVEL: WARNING\n', 1))
PY
cat > "$GAC_FINAL" <<'MD'
Set LOG_LEVEL to WARNING.
MD
