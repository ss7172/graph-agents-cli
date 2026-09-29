cd route-agent
python3 - <<'PY'
from pathlib import Path

p = Path('graph-agents-cli-manifest.yaml')
t = p.read_text()
for env, ctx in (('staging', 'stg-eu1'), ('prod', 'prod-eu1')):
    old = f'  {env}: {{ context: "", namespace: route-agent-{env} }}'
    assert old in t, old
    t = t.replace(old, f'  {env}: {{ context: "{ctx}", namespace: route-agent-{env} }}')
p.write_text(t)
PY
cat > "$GAC_FINAL" <<'MD'
Recorded environments.staging.context stg-eu1 and environments.prod.context prod-eu1 in the manifest.
MD
