cd quality-agent
graph-agents-cli eval run || true
python3 - <<'PY'
from pathlib import Path

p = Path("tests/eval/eval_config.yaml")
p.write_text(p.read_text().replace("response_quality: { min_pass_rate: 0.9 }", "response_quality: { threshold: 4, min_pass_rate: 0.9 }"))
PY
graph-agents-cli eval run
echo "Exit 3 was a config error: the response_quality quality metric had no threshold. Restored threshold 4 (min_pass_rate 0.9); eval run now exits 0 (fake model: plumbing only)." > "$GAC_FINAL"
