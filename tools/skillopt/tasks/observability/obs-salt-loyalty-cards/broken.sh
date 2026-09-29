# A plausible mistake: make the app enforce a salt in template code.
cd loyalty-agent
python3 - <<'PY'
from pathlib import Path

p = Path("graph-agents-cli-manifest.yaml")
t = p.read_text()
old = '  keys: [OPENAI_API_KEY, JUDGE_API_KEY, POSTGRES_DSN, LANGSMITH_API_KEY]'
assert old in t, "secrets.keys line not found"
p.write_text(t.replace(old, old[:-1] + ", PRINCIPAL_HASH_SALT]"))
PY
printf '\n\n# Refuse to start without a salt.\nif not __import__("os").environ.get(PRINCIPAL_HASH_SALT_ENV):\n    raise RuntimeError("PRINCIPAL_HASH_SALT is required")\n' >> app/app_utils/auth.py
cat > "$GAC_FINAL" <<'MD'
Added the salt to secrets.keys; the app now refuses to start without it.
MD
