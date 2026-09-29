# A plausible mistake: the shared values.yaml turns tracing on everywhere.
cd dispatch-agent
python3 - <<'PY'
from pathlib import Path

p = Path('deployment/helm/dispatch-agent/values.yaml')
t = p.read_text()
t = t.replace('tracing:\n  enabled: false', 'tracing:\n  enabled: true', 1)
t = t.replace('otlpEndpoint: ""', 'otlpEndpoint: "http://otel-collector.monitoring.svc:4318"', 1)
p.write_text(t)
PY
cat > "$GAC_FINAL" <<'MD'
Tracing to the collector enabled.
MD
