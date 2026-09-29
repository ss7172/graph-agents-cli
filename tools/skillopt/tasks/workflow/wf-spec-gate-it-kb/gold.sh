cat > "$GAC_FINAL" <<'MD'
Before I scaffold anything I need a few decisions (I cannot treat the request as an
approved spec):
1. How should the agent read Confluence: its REST API with a read-only service token,
   or each employee's own access? Which space?
2. How do employees sign in (our OIDC provider, per-user tokens)?
3. Which model provider may see the articles?
4. On Kubernetes: which registry, and how are releases delivered (Argo CD, CI with helm, manual)?
MD
