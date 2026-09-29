cd shop
cat >> deployment/helm/shop/values-staging.yaml <<'YAML'
tracing:
  enabled: true
  capture: metadata
  langsmith:
    project: shop-staging
YAML
echo "values-staging.yaml: tracing enabled, capture metadata, LangSmith project shop-staging. Put LANGSMITH_API_KEY in .env.staging and run graph-agents-cli secrets apply --env staging, then deploy." > "$GAC_FINAL"
