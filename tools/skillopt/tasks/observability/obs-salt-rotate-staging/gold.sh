cd crm-agent
python3 - <<'PY'
from pathlib import Path

p = Path('.env.staging')
want = {'PRINCIPAL_HASH_SALT': '6b1f0d9e3a7c5e2b8d4f1a6c9e3b7d05'}
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
cat > "$GAC_FINAL" <<'MD'
New random PRINCIPAL_HASH_SALT written to .env.staging. Roll it out:
1. graph-agents-cli secrets apply --env staging
2. graph-agents-cli deploy --restart --env staging
Every hashed principal id changes from then on.
MD
