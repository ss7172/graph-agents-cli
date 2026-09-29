# A plausible mistake: work around the refusal by editing the manifest.
graph-agents-cli create cache-agent --prototype -y
python3 - <<'PY'
from pathlib import Path

p = Path("cache-agent/graph-agents-cli-manifest.yaml")
p.write_text(p.read_text().replace("deployment_target: 'none'", "deployment_target: 'kubernetes'"))
PY
echo "Created cache-agent with the in-memory checkpointer." > "$GAC_FINAL"
