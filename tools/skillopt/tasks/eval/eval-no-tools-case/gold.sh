cd greeter-agent
python3 - <<'PY'
import json
from pathlib import Path

p = Path('tests/eval/datasets/basic-dataset.json')
data = json.loads(p.read_text())
data['cases'].append({'id': 'thanks-no-tools', 'messages': [{'role': 'user', 'content': "Thanks, that's all for today!"}], 'expect': {'no_tool_calls': True}})
p.write_text(json.dumps(data, indent=2) + '\n')
PY
graph-agents-cli eval run
cat > "$GAC_FINAL" <<'MD'
Added thanks-no-tools with `expect.no_tool_calls: true`; eval run exit 0 (fake model: plumbing only).
MD
