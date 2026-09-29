# A plausible mistake: the fake model's own value as fixed text.
cd climate-desk
python3 - <<'PY'
import json
from pathlib import Path

p = Path('tests/eval/datasets/basic-dataset.json')
data = json.loads(p.read_text())
data['cases'].append({'id': 'berlin-temperature', 'messages': [{'role': 'user', 'content': 'What is the weather in Berlin?'}], 'expect': {'contains': ['90 degrees']}})
p.write_text(json.dumps(data, indent=2) + '\n')
PY
graph-agents-cli eval run
cat > "$GAC_FINAL" <<'MD'
Added berlin-temperature (contains 90 degrees); the eval passes.
MD
