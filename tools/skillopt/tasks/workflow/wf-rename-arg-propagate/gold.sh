cd forecast-desk
python3 - <<'PY'
from pathlib import Path

p = Path('app/tools/weather.py')
t = p.read_text()
t = t.replace('def get_weather(query: str)', 'def get_weather(city: str)')
t = t.replace('query.lower()', 'city.lower()')
p.write_text(t)
d = Path('tests/eval/datasets/basic-dataset.json')
d.write_text(d.read_text().replace('"args_subset": {"query"', '"args_subset": {"city"'))
PY
graph-agents-cli lint
graph-agents-cli eval run
cat > "$GAC_FINAL" <<'MD'
Renamed get_weather's argument to `city` in app/tools/weather.py and in the eval cases' args_subset; lint passes and eval run exits 0 (fake model: plumbing only).
MD
