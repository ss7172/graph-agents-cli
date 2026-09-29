cd shop
python3 - <<'PY'
from pathlib import Path

p = Path("graph-agents-cli-manifest.yaml")
t = p.read_text()
old = "  keys: [OPENAI_API_KEY, JUDGE_API_KEY, POSTGRES_DSN, API_KEY, LANGSMITH_API_KEY]"
assert old in t
p.write_text(t.replace(old, old[:-1] + ", WEATHER_API_TOKEN]"))
PY
printf 'OPENAI_API_KEY=x\nAPI_KEY=y\nWEATHER_API_TOKEN=z\n' > "$TMPDIR/env.dev"
graph-agents-cli secrets apply --env dev --env-file "$TMPDIR/env.dev" --dry-run
echo "Added WEATHER_API_TOKEN to secrets.keys; put it in each .env.<env> and run graph-agents-cli secrets apply --env <env>, then deploy --restart." > "$GAC_FINAL"
