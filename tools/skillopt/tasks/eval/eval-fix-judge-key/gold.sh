cd digest-agent
graph-agents-cli eval run || true
python3 - <<'PY'
from pathlib import Path

p = Path('tests/eval/eval_config.yaml')
p.write_text(p.read_text().replace('  temperature: 0\n', '', 1))
PY
graph-agents-cli eval run
cat > "$GAC_FINAL" <<'MD'
judge: accepts only provider, model and max_tool_result_chars; removed temperature. eval run exits 0.
MD
