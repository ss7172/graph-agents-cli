cd shop
graph-agents-cli deploy --env dev --dry-run > deploy-plan.txt 2>&1
echo "deploy-plan.txt holds the output of graph-agents-cli deploy --env dev --dry-run: the commands a real deploy would run and the rendered manifests. Nothing was changed." > "$GAC_FINAL"
