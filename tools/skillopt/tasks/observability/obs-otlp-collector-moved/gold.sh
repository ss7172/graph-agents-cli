cd atlas-agent
python3 - <<'PY'
from pathlib import Path

p = Path('deployment/helm/atlas-agent/values-staging.yaml')
t = p.read_text()
assert 'otlpEndpoint: http://otel-old.monitoring.svc:4318' in t
p.write_text(t.replace('otlpEndpoint: http://otel-old.monitoring.svc:4318', 'otlpEndpoint: http://otel-collector.observability.svc:4318'))
PY
cat > "$GAC_FINAL" <<'MD'
values-staging.yaml: tracing.otlpEndpoint is now http://otel-collector.observability.svc:4318. It takes effect with the next `graph-agents-cli deploy --env staging`.
MD
