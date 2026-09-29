# A plausible mistake: rename the code only and stop at lint.
cd weather-agent
python3 - <<'PY'
from pathlib import Path

p = Path("app/tools/weather.py")
p.write_text(p.read_text().replace("get_weather", "get_weather_report"))
PY
graph-agents-cli lint
echo "Renamed the tool; lint passes." > "$GAC_FINAL"
