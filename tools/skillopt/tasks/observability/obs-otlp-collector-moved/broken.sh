# A plausible mistake: an env entry in the shared values (the chart's tracing value wins).
cd atlas-agent
python3 - <<'PY'
from pathlib import Path

p = Path('deployment/helm/atlas-agent/values.yaml')
t = p.read_text()
p.write_text(t.replace('  PORT: "8000"\n', '  PORT: "8000"\n  OTEL_EXPORTER_OTLP_ENDPOINT: http://otel-collector.observability.svc:4318\n', 1))
PY
cat > "$GAC_FINAL" <<'MD'
Pointed OTEL_EXPORTER_OTLP_ENDPOINT at the new collector.
MD
