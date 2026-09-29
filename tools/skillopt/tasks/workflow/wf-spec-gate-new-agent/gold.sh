cat > .graph-agents-cli-spec.md <<'MD'
# Incident agent: draft spec (not approved)

- Purpose: answer questions about incidents. Open: which incident system and which API operations
  (list, get)? Read-only?
- Callers: who uses it, and how do they authenticate (shared bearer, per-user JWT, existing session)?
- Model provider: which provider may receive incident data?
- Deployment: prototype first, or Kubernetes (runtime, CD mode, registry)?
MD
echo "Before scaffolding I need your answers: which incident system/API operations (read-only?), who calls the agent and how they authenticate, which model provider may see incident data, and prototype or Kubernetes? I drafted .graph-agents-cli-spec.md; nothing is scaffolded until you approve it." > "$GAC_FINAL"
