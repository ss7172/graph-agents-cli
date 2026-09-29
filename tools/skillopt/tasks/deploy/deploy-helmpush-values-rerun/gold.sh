cd ledger-agent
python3 - <<'PY'
from pathlib import Path

p = Path('deployment/helm/ledger-agent/values-staging.yaml')
t = p.read_text()
assert 'replicaCount: 1\n' in t
p.write_text(t.replace('replicaCount: 1\n', 'replicaCount: 3\n', 1))
PY
cat > "$GAC_FINAL" <<'MD'
values-staging.yaml now has replicaCount: 3. In helm-push mode the staging workflow
deploys from the self-hosted runner, but a push that only changes values-*.yaml does
not start it: after the merge, run the `staging` workflow by hand from main
(workflow_dispatch). It runs graph-agents-cli deploy --env staging on the runner.
MD
