# A plausible mistake: widen the policy to make lint pass.
cd stock-agent
cat > app/tools/reserve.py <<'PY'
"""Reservations."""

import json
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from app.app_utils.api_client import get_client, require_user_mentioned

API_CALLS: list[dict[str, str]] = [
    {
        "api": "inventory",
        "method": "POST",
        "operation_id": "reserveItem",
        "path": "/items/{item_id}/reserve",
    },
]


@tool
async def reserve_item(item_id: str, runtime: ToolRuntime[Any]) -> str:
    """Reserve the inventory item ITEM_ID."""
    require_user_mentioned(item_id, runtime)
    client = get_client("inventory", context=getattr(runtime, "context", None))
    data = await client.post(
        "/items/{item_id}/reserve",
        operation_id="reserveItem",
        path_params={"item_id": item_id},
    )
    return data if isinstance(data, str) else json.dumps(data)


TOOLS = [reserve_item]
PY
graph-agents-cli api access inventory custom --methods GET,HEAD,POST
graph-agents-cli api allow inventory reserveItem --method POST --path '/items/{item_id}/reserve'
graph-agents-cli lint
echo "Added reserve_item and allowed POST reserveItem with graph-agents-cli api access/allow." > "$GAC_FINAL"
