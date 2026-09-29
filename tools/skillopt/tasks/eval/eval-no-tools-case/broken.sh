# A plausible mistake: an empty tool_calls list, which asserts nothing.
cd greeter-agent
python3 - <<'PY'
import json
from pathlib import Path

p = Path('tests/eval/datasets/basic-dataset.json')
data = json.loads(p.read_text())
data['cases'].append({'id': 'thanks-no-tools', 'messages': [{'role': 'user', 'content': "Thanks, that's all for today!"}], 'expect': {'tool_calls': []}})
p.write_text(json.dumps(data, indent=2) + '\n')
PY
graph-agents-cli eval run
cat > "$GAC_FINAL" <<'MD'
Added thanks-no-tools.
MD
