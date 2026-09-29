cd roster-agent
python3 - <<'PY'
from pathlib import Path

p = Path('.env')
want = {'PRINCIPAL_HASH_SALT': '9f3c1e7a5b2d4f608e1a7c3b5d9f2e4a'}
drop = ()
lines, seen = [], set()
for line in (p.read_text().splitlines() if p.exists() else []):
    key = line.split("=", 1)[0].strip()
    if key in drop:
        continue
    if key in want:
        line = f"{key}={want[key]}"
        seen.add(key)
    lines.append(line)
lines += [f"{k}={v}" for k, v in want.items() if k not in seen]
p.write_text("\n".join(lines) + "\n")
PY
python3 - <<'PY'
from pathlib import Path

p = Path("graph-agents-cli-manifest.yaml")
t = p.read_text()
old = '  keys: [OPENAI_API_KEY, JUDGE_API_KEY, POSTGRES_DSN, LANGSMITH_API_KEY]'
assert old in t, "secrets.keys line not found"
p.write_text(t.replace(old, old[:-1] + ", PRINCIPAL_HASH_SALT]"))
PY
cat > "$GAC_FINAL" <<'MD'
Configuration only: PRINCIPAL_HASH_SALT (random, 32 hex characters) is in .env, which is git-ignored; the app HMAC-keys the hashed principal id with it. Restart your local server. I also added it to secrets.keys, so deployed environments get it from .env.<env> with secrets apply. Hashes differ from older traces from now on.
MD
