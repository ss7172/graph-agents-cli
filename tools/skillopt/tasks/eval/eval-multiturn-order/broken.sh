# A plausible mistake: without scope all_turns only the final turn's calls are checked.
cd trip-agent
python3 - <<'PY'
import json
from pathlib import Path

path = Path("tests/eval/datasets/basic-dataset.json")
data = json.loads(path.read_text())
data["cases"].append({"id": "rome-then-oslo", "messages": [{"role": "user", "content": "What is the weather in Rome?"}, {"role": "user", "content": "What is the weather in Oslo?"}], "expect": {"tool_calls": [{"name": "get_weather", "args_subset": {"query": "Rome"}}, {"name": "get_weather", "args_subset": {"query": "Oslo"}}], "ordered": True}})
path.write_text(json.dumps(data, indent=2) + "\n")
PY

graph-agents-cli eval run || true
echo "Added rome-then-oslo." > "$GAC_FINAL"
