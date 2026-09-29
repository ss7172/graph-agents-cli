# A plausible mistake: a schema whose due_date cannot be null, and the default strategy left on.
cd invoice-reader
cat > app/response_schema.json <<'JSON'
{
  "type": "object",
  "properties": {
    "invoice_id": {"type": "string"},
    "amount": {"type": "number", "minimum": 0},
    "currency": {"type": "string", "enum": ["EUR", "USD", "GBP"]},
    "due_date": {"type": "string"}
  },
  "required": ["invoice_id", "amount", "currency", "due_date"],
  "additionalProperties": false
}
JSON
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
Added app/response_schema.json with the four invoice fields.
MD
