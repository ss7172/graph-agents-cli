cd travel-agent
python3 - <<'PY'
from pathlib import Path

p = Path("app/agent.py")
t = p.read_text()
old = '"You are a helpful assistant. Use the available tools when they can answer the "\n    "question; otherwise answer directly and concisely.\\n\\n"'
new = '"You are a travel assistant: you help users plan trips and suggest destinations. Use "\n    "the available tools when they can answer the question; otherwise answer directly and "\n    "concisely.\\n\\n"'
assert old in t, "prompt paragraph not found"
p.write_text(t.replace(old, new))
PY
graph-agents-cli lint
echo "Changed only the first paragraph of SYSTEM_PROMPT (travel assistant); lint passes." > "$GAC_FINAL"
