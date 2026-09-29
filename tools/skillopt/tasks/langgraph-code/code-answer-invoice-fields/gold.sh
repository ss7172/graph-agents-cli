cd invoice-reader
cat > app/response_schema.json <<'JSON'
{
  "type": "object",
  "description": "The fields of one invoice.",
  "properties": {
    "invoice_id": {"type": "string"},
    "amount": {"type": "number", "minimum": 0},
    "currency": {"type": "string", "enum": ["EUR", "USD", "GBP"]},
    "due_date": {"anyOf": [{"type": "string"}, {"type": "null"}]}
  },
  "required": ["invoice_id", "amount", "currency", "due_date"],
  "additionalProperties": false
}
JSON
for f in .env .env.example; do
  sed -i.bak 's/^# RESPONSE_FORMAT_STRATEGY=auto$/RESPONSE_FORMAT_STRATEGY=tool/' "$f" && rm -f "$f.bak"
  /usr/bin/grep -q '^RESPONSE_FORMAT_STRATEGY=tool$' "$f" || printf '\nRESPONSE_FORMAT_STRATEGY=tool\n' >> "$f"
done
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
Declared the answer in app/response_schema.json: invoice_id (string), amount (number, minimum
0), currency (EUR, USD, GBP) and due_date (string or null), all required. Because the model
server has no native structured output, RESPONSE_FORMAT_STRATEGY=tool is set in .env and
.env.example (set it in the chart values too when you deploy): the model answers through the
final_answer tool and every answer is checked. The calling agent reads the JSON text of the A2A
reply (a data part holds the object). lint passes.
MD
