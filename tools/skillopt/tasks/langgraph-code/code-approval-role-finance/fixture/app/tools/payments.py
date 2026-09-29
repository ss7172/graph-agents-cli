"""Payments: look up and refund payments."""

import json
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from app.app_utils.api_client import get_client, require_user_mentioned

API_CALLS: list[dict[str, str]] = [
    {
        "api": "payments",
        "method": "GET",
        "operation_id": "getPayment",
        "path": "/payments/{payment_id}",
    },
    {
        "api": "payments",
        "method": "POST",
        "operation_id": "refundPayment",
        "path": "/payments/{payment_id}/refund",
    },
]


@tool
async def get_payment(payment_id: str, runtime: ToolRuntime[Any]) -> str:
    """Return the payment record for PAYMENT_ID."""
    client = get_client("payments", context=getattr(runtime, "context", None))
    data = await client.get(
        "/payments/{payment_id}",
        operation_id="getPayment",
        path_params={"payment_id": payment_id},
    )
    return data if isinstance(data, str) else json.dumps(data)


@tool
async def refund_payment(payment_id: str, runtime: ToolRuntime[Any]) -> str:
    """Refund the payment PAYMENT_ID in full."""
    require_user_mentioned(payment_id, runtime)
    client = get_client("payments", context=getattr(runtime, "context", None))
    data = await client.post(
        "/payments/{payment_id}/refund",
        operation_id="refundPayment",
        path_params={"payment_id": payment_id},
    )
    return data if isinstance(data, str) else json.dumps(data)


TOOLS = [get_payment, refund_payment]
