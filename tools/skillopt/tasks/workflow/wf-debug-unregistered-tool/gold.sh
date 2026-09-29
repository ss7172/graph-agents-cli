cd weather-bot
graph-agents-cli eval run || true
python3 - <<'PY'
from pathlib import Path

p = Path('app/tools/weather.py')
p.write_text(p.read_text().replace('TOOL = [get_weather]', 'TOOLS = [get_weather]'))
PY
graph-agents-cli eval run
cat > "$GAC_FINAL" <<'MD'
Root cause: app/tools/weather.py exported `TOOL` instead of `TOOLS`, so the agent had no tools. Restored `TOOLS = [get_weather]`; eval run exits 0.
MD
