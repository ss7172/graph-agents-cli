cat > "$GAC_FINAL" <<'MD'
In argocd mode `deploy` never runs helm, never touches the cluster and never merges. It writes
`image.tag: 4f2a9c1` into deployment/helm/shop-gitops/values-prod.yaml on a branch
`deploy/prod/<short sha>` built from origin/main and opens a pull request. Then a code owner of
values-prod.yaml must review and merge it (no self-approval; pr_checks must pass): that merge is the
single production gate. Argo CD reconciles from main; the prod Application has no automated sync, so
an operator syncs it in Argo CD after the merge.
MD
