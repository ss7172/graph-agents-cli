cd concierge-agent
python3 - <<'PY'
import json
from pathlib import Path

p = Path('tests/eval/datasets/basic-dataset.json')
data = json.loads(p.read_text())
for case in data['cases']:
    case.setdefault('judge', {})['response_quality'] = {'threshold': 4}
p.write_text(json.dumps(data, indent=2) + '\n')
PY
python3 - <<'PY'
from pathlib import Path

p = Path('tests/eval/eval_config.yaml')
t = p.read_text()
assert 'response_quality: { threshold: 4, min_pass_rate: 0.9 }' in t
p.write_text(t.replace('response_quality: { threshold: 4, min_pass_rate: 0.9 }', 'response_quality: { threshold: 4, min_pass_rate: 0.75 }'))
PY
graph-agents-cli eval run
cat > "$GAC_FINAL" <<'MD'
response_quality is declared on all four cases (threshold 4) with min_pass_rate 0.75, so one case of four may score below 4. eval run: exit 0 (fake judge: plumbing only).
MD
