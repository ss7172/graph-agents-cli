cd dispatch-agent
printf 'tracing:\n  enabled: true\n  capture: metadata\n  otlpEndpoint: http://otel-collector.monitoring.svc:4318\n' >> deployment/helm/dispatch-agent/values-prod.yaml
cat > "$GAC_FINAL" <<'MD'
values-prod.yaml: tracing enabled, capture metadata, otlpEndpoint http://otel-collector.monitoring.svc:4318. Keep LANGSMITH_API_KEY out of the prod Secret (with it, traces go to LangSmith). Deploy prod to apply.
MD
