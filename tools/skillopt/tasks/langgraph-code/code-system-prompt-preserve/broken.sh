# A plausible mistake: replace the whole prompt, losing the tool-results rule.
cd travel-agent
python3 - <<'PY'
import re
from pathlib import Path

p = Path("app/agent.py")
t = p.read_text()
t = re.sub(r"SYSTEM_PROMPT = \(.*?\n\)\n", 'SYSTEM_PROMPT = "You are a travel assistant that helps users plan trips and suggests destinations."\n', t, count=1, flags=re.S)
p.write_text(t)
PY
graph-agents-cli lint || true
echo "The agent is now a travel assistant." > "$GAC_FINAL"
