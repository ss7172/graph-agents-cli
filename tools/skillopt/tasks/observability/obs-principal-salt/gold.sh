cd hr-agent
python3 - <<'PY'
from pathlib import Path

p = Path("graph-agents-cli-manifest.yaml")
t = p.read_text()
old = "  keys: [OPENAI_API_KEY, JUDGE_API_KEY, POSTGRES_DSN, API_KEY, LANGSMITH_API_KEY]"
assert old in t
p.write_text(t.replace(old, old[:-1] + ", PRINCIPAL_HASH_SALT]"))
PY

echo "Added PRINCIPAL_HASH_SALT to secrets.keys: set a long random value in each .env.<env>, run graph-agents-cli secrets apply --env <env> and deploy --restart. Hashes change when the salt does." > "$GAC_FINAL"
