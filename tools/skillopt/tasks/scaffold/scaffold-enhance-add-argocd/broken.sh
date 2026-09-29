# A plausible mistake: edit the manifest instead of running enhance.
cd shop-agent
python3 - <<'PY'
from pathlib import Path

p = Path("graph-agents-cli-manifest.yaml")
t = p.read_text()
for old, new in (("deployment_target: 'none'", "deployment_target: 'kubernetes'"),
                 ("checkpointer: 'memory'", "checkpointer: 'postgres'"),
                 ("cd: 'skip'", "cd: 'argocd'"),
                 ("registry: ''", "registry: 'ghcr.io/acme'")):
    t = t.replace(old, new)
p.write_text(t)
PY
echo "Switched shop-agent to kubernetes + argocd." > "$GAC_FINAL"
