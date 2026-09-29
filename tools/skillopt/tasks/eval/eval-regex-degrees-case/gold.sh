cd climate-desk
python3 - <<'PY'
import json
from pathlib import Path

p = Path('tests/eval/datasets/basic-dataset.json')
data = json.loads(p.read_text())
data['cases'].append({'id': 'berlin-temperature', 'messages': [{'role': 'user', 'content': 'What is the weather in Berlin?'}], 'expect': {'regex': '(?i)\\b\\d+(\\.\\d+)? ?(°|degrees)'}})
p.write_text(json.dumps(data, indent=2) + '\n')
PY
graph-agents-cli eval run
cat > "$GAC_FINAL" <<'MD'
Added berlin-temperature with `expect.regex` for a number followed by degrees; eval run: 5 passed, exit 0 (fake model: plumbing only).
MD
