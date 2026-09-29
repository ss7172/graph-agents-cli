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
graph-agents-cli lint || true
cat > "$GAC_FINAL" <<'MD'
Added app/tools/items.py (list_items, API_CALLS declares GET listItems). `graph-agents-cli lint`
refuses the call: the inventory API allows only getItem. Allowing a new operation is your
reviewed decision, so I did not run it. To allow it:
`graph-agents-cli api allow inventory listItems --method GET --path /items --dry-run`, then without --dry-run.
MD
