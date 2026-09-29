# A plausible mistake: the tool is named after the file, not `get_weather`.
cd forecast-agent
python3 - <<'PY'
import json
from pathlib import Path

path = Path("tests/eval/datasets/basic-dataset.json")
data = json.loads(path.read_text())
data["cases"].append({"id": "tokyo-weather", "messages": [{"role": "user", "content": "What is the weather in Tokyo?"}], "expect": {"contains": ["sunny"], "tool_calls": [{"name": "weather", "args_subset": {"query": "Tokyo"}}]}})
path.write_text(json.dumps(data, indent=2) + "\n")
PY

graph-agents-cli eval run || true
echo "Added tokyo-weather." > "$GAC_FINAL"
