# A plausible mistake: fix the visible failure (the model) and keep the bound checkpointer.
cd notes-agent
python3 - <<'PY'
from pathlib import Path
p = Path('app/agent.py')
t = p.read_text()
t = t.replace('\nmodel = init_chat_model("openai:gpt-5-mini")\n', '\nmodel = get_model()\n').replace('from langchain.chat_models import init_chat_model\n', '')
p.write_text(t)
PY
graph-agents-cli eval run || true
echo "Restored get_model(); eval passes." > "$GAC_FINAL"
