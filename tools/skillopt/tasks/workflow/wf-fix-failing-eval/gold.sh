cd weather-desk
graph-agents-cli eval run || true
python3 - <<'PY'
from pathlib import Path

p = Path("app/tools/weather.py")
p.write_text(p.read_text().replace("degrees and suny.", "degrees and sunny."))
PY
graph-agents-cli eval run
echo "Root cause: app/tools/weather.py returned 'suny' (typo), failing the weather case's contains: sunny. Fixed the string; eval run exits 0." > "$GAC_FINAL"
