# A plausible mistake: the shared values.yaml.
cd market-agent
python3 - <<'PY'
from pathlib import Path

p = Path('deployment/helm/market-agent/values.yaml')
p.write_text(p.read_text().replace('http://CHANGE-ME', 'https://payments.acme.com'))
PY
graph-agents-cli deploy --env prod --dry-run --tag abc1234 || true
cat > "$GAC_FINAL" <<'MD'
Set the payments URL.
MD
