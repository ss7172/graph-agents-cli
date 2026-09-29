# A plausible mistake: a quality metric without a per-case threshold (exit 3).
cd facts-agent
python3 - <<'PY'
from pathlib import Path

p = Path('tests/eval/eval_config.yaml')
t = p.read_text()
line = '  response_quality: { threshold: 4, min_pass_rate: 0.9 }\n'
p.write_text(t.replace(line, line + '  groundedness: { min_pass_rate: 0.75 }\n'))
PY
python3 - <<'PY'
import json
from pathlib import Path

p = Path('tests/eval/datasets/basic-dataset.json')
data = json.loads(p.read_text())
for case in data['cases']:
    if case['id'] in ('weather', 'weather-follow-up'):
        case.setdefault('judge', {})['groundedness'] = {'threshold': 3}
p.write_text(json.dumps(data, indent=2) + '\n')
PY
graph-agents-cli eval run || true
cat > "$GAC_FINAL" <<'MD'
Configured groundedness.
MD
