cd survey-agent
graph-agents-cli eval run || true
python3 - <<'PY'
from pathlib import Path

p = Path('tests/eval/eval_config.yaml')
t = p.read_text()
t = t.replace('\n# Let task_success fail on up to 20 percent of its cases.\nmin_pass_rate: 0.8\n', '\n')
line = '  response_quality: { threshold: 4, min_pass_rate: 0.9 }\n'
assert line in t
t = t.replace(line, line + '  task_success: { threshold: 4, min_pass_rate: 0.8 }\n')
p.write_text(t)
PY
graph-agents-cli eval run
cat > "$GAC_FINAL" <<'MD'
Exit 3 came from a top-level `min_pass_rate`, which the config does not have. I made task_success a quality metric (threshold 4, min_pass_rate 0.8) under quality_metrics; eval run exits 0 (fake model: plumbing only).
MD
