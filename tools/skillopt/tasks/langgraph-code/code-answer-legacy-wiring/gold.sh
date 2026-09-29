cd claims-intake
python3 - <<'PY'
from pathlib import Path
p = Path("app/agent.py")
t = p.read_text()
old_import = "from app.app_utils.model import get_model\n"
assert old_import in t
t = t.replace(
    old_import,
    old_import
    + "from app.app_utils.structured import (\n    StructuredAnswer,\n    response_format,\n)\n",
    1,
)
old_mw = "    return [SurfaceApiErrors(), AnswerInvalidToolCalls(), UntrustedToolResults()]\n"
assert old_mw in t
t = t.replace(
    old_mw,
    "    return [\n        SurfaceApiErrors(),\n        AnswerInvalidToolCalls(),\n"
    "        UntrustedToolResults(),\n        StructuredAnswer(),\n    ]\n",
    1,
)
old_graph = "graph: CompiledStateGraph = create_agent(\n    model=get_model(),\n    tools=get_tools(),\n"
assert old_graph in t
t = t.replace(
    old_graph,
    "model = get_model()\ntools = get_tools()\n"
    "graph: CompiledStateGraph = create_agent(\n    model=model,\n    tools=tools,\n",
    1,
)
old_ctx = "    context_schema=AgentContext,\n"
assert t.count(old_ctx) == 1
t = t.replace(old_ctx, old_ctx + "    response_format=response_format(model, tools),\n", 1)
p.write_text(t)
PY
cat > app/response_schema.json <<'JSON'
{
  "type": "object",
  "description": "An insurance claim, read by the claims system.",
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
scaffold upgrade added app_utils/structured.py but never rewrites agent.py, so I wired it by
hand: create_agent now gets response_format=response_format(model, tools) and middleware()
ends with StructuredAnswer(). Then I declared the answer in app/response_schema.json
(claim_type auto/home/travel, urgent boolean, notes string, all required). lint: "Response
schema: app/response_schema.json OK".
MD
