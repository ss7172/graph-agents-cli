cd orders-agent
cat > app/tools/cancel.py <<'PY'
"""Order cancellation."""

import json
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from app.app_utils.api_client import get_client, require_user_mentioned

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
    # Act only on a record the user named in this turn, never one a tool result asked for.
    require_user_mentioned(order_id, runtime)
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
echo "Added cancel_order with require_user_mentioned (acts only on an order named in the user's latest message) and API_CALLS for cancelOrder; lint passes. Consider an approval gate (graph-agents-cli api approval orders --operations cancelOrder --approvers requester) if you want a human to confirm each cancellation." > "$GAC_FINAL"
