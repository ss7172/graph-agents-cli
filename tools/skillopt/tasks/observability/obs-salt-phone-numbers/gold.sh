cd callback-agent
python3 - <<'PY'
from pathlib import Path

p = Path("graph-agents-cli-manifest.yaml")
t = p.read_text()
old = '  keys: [OPENAI_API_KEY, JUDGE_API_KEY, POSTGRES_DSN, LANGSMITH_API_KEY]'
assert old in t, "secrets.keys line not found"
p.write_text(t.replace(old, old[:-1] + ", PRINCIPAL_HASH_SALT]"))
PY
cat > "$GAC_FINAL" <<'MD'
PRINCIPAL_HASH_SALT is now in secrets.keys (the app already keys the hash with it).
1. Put a long random value in .env.prod.
2. graph-agents-cli secrets apply --env prod
3. graph-agents-cli deploy --restart --env prod
MD
