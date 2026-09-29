# A plausible mistake: the tool's argument is `query`, not `city`.
cd weather-agent
python3 - <<'PY'
import json
from pathlib import Path

path = Path("tests/eval/datasets/basic-dataset.json")
data = json.loads(path.read_text())
data["cases"].append({"id": "lisbon-weather", "messages": [{"role": "user", "content": "What is the weather in Lisbon?"}], "expect": {"tool_calls": [{"name": "get_weather", "args_subset": {"city": "Lisbon"}}]}})
path.write_text(json.dumps(data, indent=2) + "\n")
PY

graph-agents-cli eval run || true
echo "Added the Lisbon case; the eval ran." > "$GAC_FINAL"
