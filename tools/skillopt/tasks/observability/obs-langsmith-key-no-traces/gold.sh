cd insight-agent
python3 - <<'PY'
from pathlib import Path

p = Path(".env")
want = {'TRACING_ENABLED': 'true'}
lines, seen = [], set()
for line in p.read_text().splitlines():
    key = line.split("=", 1)[0]
    if key in ():
        continue
    if key in want:
        line = f"{key}={want[key]}"
        seen.add(key)
    lines.append(line)
lines += [f"{k}={v}" for k, v in want.items() if k not in seen]
p.write_text("\n".join(lines) + "\n")
PY

echo "TRACING_ENABLED was false: the LangSmith key alone does not enable tracing. Set TRACING_ENABLED=true; capture stays metadata." > "$GAC_FINAL"
