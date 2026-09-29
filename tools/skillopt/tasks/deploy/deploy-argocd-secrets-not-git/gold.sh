cd fleet-agent
cat > "$GAC_FINAL" <<'MD'
I did not put the keys into the repository: in argocd mode Argo CD never manages the
app Secret, and secrets never go into git. The Secret's owner creates it from a
workstation with cluster access:
1. graph-agents-cli secrets apply --env staging   (reads .env.staging, allow-listed keys only)
2. graph-agents-cli secrets status --env staging
MD
