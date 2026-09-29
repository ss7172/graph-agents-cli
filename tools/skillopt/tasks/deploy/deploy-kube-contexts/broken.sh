# A plausible mistake: a chart value, which deploy never reads for the context.
cd route-agent
printf 'kubeContext: stg-eu1\n' >> deployment/helm/route-agent/values-staging.yaml
printf 'kubeContext: prod-eu1\n' >> deployment/helm/route-agent/values-prod.yaml
cat > "$GAC_FINAL" <<'MD'
Pinned the contexts in the values files.
MD
