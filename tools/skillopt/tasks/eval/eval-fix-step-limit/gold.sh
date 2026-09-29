cd ops-agent
graph-agents-cli eval run || true
python3 - <<'PY'
from pathlib import Path

p = Path(".env")
p.write_text("".join(line for line in p.read_text().splitlines(keepends=True) if not line.startswith("RECURSION_LIMIT=")))
PY
graph-agents-cli eval run
echo "Exit 2 = incomplete run: weather cases ended with step_limit because .env set RECURSION_LIMIT=2. Removed it (default 50); eval run exits 0." > "$GAC_FINAL"
