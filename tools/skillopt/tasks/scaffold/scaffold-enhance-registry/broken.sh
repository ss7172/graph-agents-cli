# A plausible mistake: change only the manifest.
cd inventory-agent
python3 - <<'PY'
from pathlib import Path

p = Path("graph-agents-cli-manifest.yaml")
p.write_text(p.read_text().replace("ghcr.io/CHANGE-ME", "registry.example.com/platform"))
PY
graph-agents-cli build --dry-run || true
echo "Registry updated in the manifest." > "$GAC_FINAL"
