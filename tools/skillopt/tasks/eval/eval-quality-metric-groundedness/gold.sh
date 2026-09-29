cd facts-agent
python3 - <<'PY'
from pathlib import Path

p = Path('tests/eval/eval_config.yaml')
t = p.read_text()
line = '  response_quality: { threshold: 4, min_pass_rate: 0.9 }\n'
assert line in t
p.write_text(t.replace(line, line + '  groundedness: { threshold: 3, min_pass_rate: 0.75 }\n'))
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
graph-agents-cli eval run
cat > "$GAC_FINAL" <<'MD'
groundedness is a quality metric (threshold 3, min_pass_rate 0.75) on the two weather cases; eval run exit 0 (fake judge: plumbing only).
MD
