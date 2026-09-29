cd stock-agent
cat > app/tools/inventory.py <<'PY'
"""Inventory lookups."""

import json
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from app.app_utils.api_client import get_client

API_CALLS: list[dict[str, str]] = [
    {
        "api": "inventory",
        "method": "GET",
        "operation_id": "getItem",
        "path": "/items/{item_id}",
    },
]


@tool
async def get_item(item_id: str, runtime: ToolRuntime[Any]) -> str:
    """Return the inventory record for ITEM_ID as JSON."""
    client = get_client("inventory", context=getattr(runtime, "context", None))
    data = await client.get(
        "/items/{item_id}",
        operation_id="getItem",
        path_params={"item_id": item_id},
    )
    return data if isinstance(data, str) else json.dumps(data)


TOOLS = [get_item]
PY
graph-agents-cli lint
echo "Added app/tools/inventory.py: get_item via get_client('inventory') with API_CALLS declaring getItem; lint passes." > "$GAC_FINAL"
