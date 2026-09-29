# A plausible mistake: a write tool that acts on whatever id the model passes.
cd orders-agent
cat > app/tools/cancel.py <<'PY'
"""Order cancellation."""

import json
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from app.app_utils.api_client import get_client

API_CALLS: list[dict[str, str]] = [
    {
        "api": "orders",
        "method": "POST",
        "operation_id": "cancelOrder",
        "path": "/orders/{order_id}/cancel",
    },
]


@tool
async def cancel_order(order_id: str, runtime: ToolRuntime[Any]) -> str:
    """Cancel the order ORDER_ID."""
    client = get_client("orders", context=getattr(runtime, "context", None))
    data = await client.post(
        "/orders/{order_id}/cancel",
        operation_id="cancelOrder",
        path_params={"order_id": order_id},
    )
    return data if isinstance(data, str) else json.dumps(data)


TOOLS = [cancel_order]
PY
graph-agents-cli lint
echo "Added cancel_order." > "$GAC_FINAL"
