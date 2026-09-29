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
graph-agents-cli lint || true
cat > "$GAC_FINAL" <<'MD'
Added app/tools/reserve.py (reserve_item, API_CALLS declares POST reserveItem). `graph-agents-cli lint`
refuses the call: the inventory API allows only GET and HEAD. Widening it is your decision and a
reviewed change, so I did not run it. To allow the call:
`graph-agents-cli api access inventory custom --methods GET,HEAD,POST --dry-run`, then
`graph-agents-cli api allow inventory reserveItem --method POST --path /items/{item_id}/reserve`.
MD
