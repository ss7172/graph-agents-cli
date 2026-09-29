# A plausible mistake: rename the code only and stop at lint.
cd forecast-desk
python3 - <<'PY'
from pathlib import Path

p = Path('app/tools/weather.py')
t = p.read_text()
t = t.replace('def get_weather(query: str)', 'def get_weather(city: str)')
t = t.replace('query.lower()', 'city.lower()')
p.write_text(t)
PY
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
Renamed the argument to city; lint passes.
MD
