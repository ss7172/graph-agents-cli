# A plausible mistake: assume the merge deploys the values change.
cd ledger-agent
python3 - <<'PY'
from pathlib import Path

p = Path('deployment/helm/ledger-agent/values-staging.yaml')
p.write_text(p.read_text().replace('replicaCount: 1\n', 'replicaCount: 3\n', 1))
PY
cat > "$GAC_FINAL" <<'MD'
Set replicaCount: 3 in values-staging.yaml; once it is merged to main, CI deploys it to staging.
MD
