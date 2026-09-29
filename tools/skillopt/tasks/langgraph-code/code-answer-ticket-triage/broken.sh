# A plausible mistake: asking for JSON in the system prompt instead of declaring a schema.
cd helpdesk-triage
python3 - <<'PY'
from pathlib import Path
p = Path("app/agent.py")
t = p.read_text()
marker = 'SYSTEM_PROMPT = (\n'
assert marker in t
t = t.replace(
    marker,
    marker + '    "Always answer with only a JSON object with category (billing, technical or "\n'
    '    "account), priority (low, medium or high) and summary (at most 200 characters).\\n\\n"\n',
    1,
)
p.write_text(t)
PY
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
The system prompt now tells the model to answer with a JSON object (category, priority,
summary).
MD
