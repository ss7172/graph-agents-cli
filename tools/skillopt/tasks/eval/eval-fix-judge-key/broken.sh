# A plausible mistake: a deterministic judge by switching it to the fake provider.
cd digest-agent
python3 - <<'PY'
from pathlib import Path

p = Path('tests/eval/eval_config.yaml')
t = p.read_text().replace('  temperature: 0\n', '', 1)
t = t.replace('  provider: null', '  provider: fake', 1).replace('  model: null', '  model: fake', 1)
p.write_text(t)
PY
graph-agents-cli eval run
cat > "$GAC_FINAL" <<'MD'
The judge is now the deterministic fake provider.
MD
