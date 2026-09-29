cd trace-agent
python3 - <<'PY'
from pathlib import Path

p = Path(".env")
want = {'TRACING_ENABLED': 'true', 'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://localhost:4318', 'TRACE_CAPTURE': 'metadata'}
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

echo "In .env: TRACING_ENABLED=true, OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318, TRACE_CAPTURE=metadata (no prompt or tool text). run/playground pick it up." > "$GAC_FINAL"
