# A plausible mistake: only the schema, as for a new project; agent.py keeps 0.2's wiring.
cd claims-intake
cat > app/response_schema.json <<'JSON'
{
  "type": "object",
  "properties": {
    "claim_type": {"type": "string", "enum": ["auto", "home", "travel"]},
    "urgent": {"type": "boolean"},
    "notes": {"type": "string"}
  },
  "required": ["claim_type", "urgent", "notes"],
  "additionalProperties": false
}
JSON
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
Added app/response_schema.json with claim_type, urgent and notes.
MD
