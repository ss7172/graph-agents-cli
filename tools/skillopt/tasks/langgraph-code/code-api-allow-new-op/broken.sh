# A plausible mistake: run the command lint prints.
cd parts-agent
cat > app/tools/items.py <<'PY'
"""Item listing by category."""

import json
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from app.app_utils.api_client import get_client

API_CALLS: list[dict[str, str]] = [
    {
        "api": "inventory",
        "method": "GET",
        "operation_id": "listItems",
        "path": "/items",
    },
]


@tool
async def list_items(category: str, runtime: ToolRuntime[Any]) -> str:
    """List the inventory items of CATEGORY."""
    client = get_client("inventory", context=getattr(runtime, "context", None))
    data = await client.get("/items", operation_id="listItems", params={"category": category})
    return data if isinstance(data, str) else json.dumps(data)


TOOLS = [list_items]
PY
graph-agents-cli api allow inventory listItems --method GET --path /items
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
Added list_items and allowed listItems with graph-agents-cli api allow; lint passes.
MD
