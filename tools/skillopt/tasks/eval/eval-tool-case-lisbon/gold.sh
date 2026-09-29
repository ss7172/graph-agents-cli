cd weather-agent
python3 - <<'PY'
import json
from pathlib import Path

path = Path("tests/eval/datasets/basic-dataset.json")
data = json.loads(path.read_text())
data["cases"].append({"id": "lisbon-weather", "messages": [{"role": "user", "content": "What is the weather in Lisbon?"}], "expect": {"tool_calls": [{"name": "get_weather", "args_subset": {"query": "Lisbon"}}]}})
path.write_text(json.dumps(data, indent=2) + "\n")
PY

graph-agents-cli eval run
cat > "$GAC_FINAL" <<'MD'
Added `lisbon-weather` with a `tool_calls` check (`get_weather`, args_subset `{"query": "Lisbon"}`).
`graph-agents-cli eval run`: 5 passed, 0 failed, 0 error, 0 missing; exit code 0. The agent and the
judge ran on the fake model, so this proves the eval plumbing only, not the agent's quality.
MD
