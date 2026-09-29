cd shop
cat incident/pod-events.txt
cat > "$GAC_FINAL" <<'MD'
The pods are in CreateContainerConfigError: `secret "shop-app" not found`. Staging requires the app
Secret (secretOptional: false) and it was never applied. Put the allow-listed keys in .env.staging, then:

1. `graph-agents-cli secrets apply --env staging`
2. `graph-agents-cli secrets status --env staging` (exit 0 when every required key is there)
3. the pods retry on their own; otherwise `graph-agents-cli deploy --restart --env staging`
MD
