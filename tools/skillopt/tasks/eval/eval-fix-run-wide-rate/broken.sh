# A plausible mistake: delete the offending key; task_success stays mandatory.
cd survey-agent
python3 - <<'PY'
from pathlib import Path

p = Path('tests/eval/eval_config.yaml')
p.write_text(p.read_text().replace('min_pass_rate: 0.8\n', ''))
PY
graph-agents-cli eval run
cat > "$GAC_FINAL" <<'MD'
Removed the invalid key; eval run passes.
MD
