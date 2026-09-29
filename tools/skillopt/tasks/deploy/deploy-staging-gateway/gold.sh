cd shop
graph-agents-cli deploy --env staging --dry-run --tag abc1234 || true
python3 -c "import pathlib; p = pathlib.Path('deployment/helm/shop/values-staging.yaml'); t = p.read_text(); t = t.replace('hostname: \"\"', 'hostname: \"shop.staging.acme.dev\"', 1).replace('    name: \"\"', '    name: \"shared-gw\"', 1).replace('    namespace: \"\"', '    namespace: \"infra\"', 1); p.write_text(t)"
graph-agents-cli deploy --env staging --dry-run --tag abc1234
echo "values-staging.yaml: gateway.parentRef shared-gw/infra, hostname shop.staging.acme.dev; deploy --env staging --dry-run renders the HTTPRoute (nothing deployed)." > "$GAC_FINAL"
