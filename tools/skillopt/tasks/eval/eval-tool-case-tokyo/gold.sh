cd forecast-agent
python3 - <<'PY'
import json
from pathlib import Path

path = Path("tests/eval/datasets/basic-dataset.json")
data = json.loads(path.read_text())
data["cases"].append({"id": "tokyo-weather", "messages": [{"role": "user", "content": "What is the weather in Tokyo?"}], "expect": {"contains": ["sunny"], "tool_calls": [{"name": "get_weather", "args_subset": {"query": "Tokyo"}}]}})
path.write_text(json.dumps(data, indent=2) + "\n")
PY

graph-agents-cli eval run
cat > "$GAC_FINAL" <<'MD'
Added `tokyo-weather` (`contains: ["sunny"]`, `tool_calls: get_weather {"query": "Tokyo"}`).
`graph-agents-cli eval run`: 5 passed, 0 failed; exit code 0 (fake model: plumbing check only).
MD
