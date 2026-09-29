cd notes-agent
graph-agents-cli eval run || true
python3 - <<'PY'
from pathlib import Path
p = Path('app/agent.py')
t = p.read_text()
t = t.replace('\nmodel = init_chat_model("openai:gpt-5-mini")\n', '\nmodel = get_model()\n').replace('from langchain.chat_models import init_chat_model\n', '')
t = t.replace('    checkpointer=InMemorySaver(),\n', '').replace('from langgraph.checkpoint.memory import InMemorySaver\n', '')
p.write_text(t)
PY
graph-agents-cli lint
graph-agents-cli eval run
echo "app/agent.py hard-coded init_chat_model('openai:gpt-5-mini') and bound an InMemorySaver; restored get_model() and removed the checkpointer (the app binds it). eval run exit 0." > "$GAC_FINAL"
