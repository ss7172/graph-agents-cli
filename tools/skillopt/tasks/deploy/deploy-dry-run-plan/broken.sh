# A plausible mistake: render the chart with helm directly (no commands, not what deploy would do).
cd shop
helm template shop deployment/helm/shop -f deployment/helm/shop/values.yaml -f deployment/helm/shop/values-dev.yaml --set image.tag=dev > deploy-plan.txt 2>&1
echo "deploy-plan.txt holds the rendered manifests." > "$GAC_FINAL"
