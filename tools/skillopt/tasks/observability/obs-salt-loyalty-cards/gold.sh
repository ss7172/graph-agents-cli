cd loyalty-agent
python3 - <<'PY'
from pathlib import Path

p = Path("graph-agents-cli-manifest.yaml")
t = p.read_text()
old = '  keys: [OPENAI_API_KEY, JUDGE_API_KEY, POSTGRES_DSN, LANGSMITH_API_KEY]'
assert old in t, "secrets.keys line not found"
p.write_text(t.replace(old, old[:-1] + ", PRINCIPAL_HASH_SALT]"))
PY
cat > "$GAC_FINAL" <<'MD'
The app already HMAC-keys the hashed principal id when PRINCIPAL_HASH_SALT is set, so
this is configuration only. I added PRINCIPAL_HASH_SALT to secrets.keys. Your steps:
1. Put a long random value (python -c "import secrets; print(secrets.token_hex(32))")
   in .env.staging and .env.prod.
2. graph-agents-cli secrets apply --env staging, then --env prod.
3. graph-agents-cli deploy --restart --env staging, then --env prod.
Every hash changes once the salt is set.
MD
