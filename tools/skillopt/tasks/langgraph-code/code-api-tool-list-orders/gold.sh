cd shop-support
cat > app/tools/orders.py <<'PY'
"""Order listing."""

import json
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from app.app_utils.api_client import get_client

API_CALLS: list[dict[str, str]] = [
    {
        "api": "orders",
        "method": "GET",
        "operation_id": "listOrders",
        "path": "/orders",
    },
]


@tool
async def list_orders(status: str, runtime: ToolRuntime[Any]) -> str:
    """List the orders that have the given STATUS (for example open or shipped)."""
    client = get_client("orders", context=getattr(runtime, "context", None))
    data = await client.get("/orders", operation_id="listOrders", params={"status": status})
    return data if isinstance(data, str) else json.dumps(data)


TOOLS = [list_orders]
PY
graph-agents-cli lint
echo "Added app/tools/orders.py (list_orders; API_CALLS declares listOrders); lint passes." > "$GAC_FINAL"
