cd helpdesk-triage
cat > app/response_schema.json <<'JSON'
{
  "type": "object",
  "description": "A support ticket, filed by the ticketing system.",
  "properties": {
    "category": {"type": "string", "enum": ["billing", "technical", "account"]},
    "priority": {"type": "string", "enum": ["low", "medium", "high"]},
    "summary": {"type": "string", "maxLength": 200}
  },
  "required": ["category", "priority", "summary"],
  "additionalProperties": false
}
JSON
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
Declared the answer's shape in app/response_schema.json: category (billing, technical,
account), priority (low, medium, high) and a summary of at most 200 characters, all required.
agent.py already builds the agent with response_format() and StructuredAnswer(), so every
answer is now that JSON object (checked, and delivered as structured_response on /chat and as
an A2A data part). lint: "Response schema: app/response_schema.json OK".
MD
