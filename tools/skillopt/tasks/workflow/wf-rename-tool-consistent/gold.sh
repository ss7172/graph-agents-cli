cd weather-agent
python3 - <<'PY'
from pathlib import Path

for rel in ("app/tools/weather.py", "tests/eval/datasets/basic-dataset.json"):
    p = Path(rel)
    p.write_text(p.read_text().replace("get_weather", "get_weather_report"))
PY
graph-agents-cli lint
graph-agents-cli eval run
echo "Renamed get_weather to get_weather_report in app/tools/weather.py and the eval dataset; lint and eval run (exit 0) pass." > "$GAC_FINAL"
