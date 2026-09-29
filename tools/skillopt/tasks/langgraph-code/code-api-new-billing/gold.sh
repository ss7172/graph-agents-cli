cd billing-helper
graph-agents-cli api add billing --base-url-env BILLING_API_BASE_URL --auth bearer --token-env BILLING_API_TOKEN --access read-only
graph-agents-cli api allow billing getInvoice --method GET --path '/invoices/{invoice_id}'
cat > app/tools/billing.py <<'PY'
"""Invoice lookups."""

import json
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from app.app_utils.api_client import get_client

API_CALLS: list[dict[str, str]] = [
    {
        "api": "billing",
        "method": "GET",
        "operation_id": "getInvoice",
        "path": "/invoices/{invoice_id}",
    },
]


@tool
async def get_invoice(invoice_id: str, runtime: ToolRuntime[Any]) -> str:
    """Return the invoice INVOICE_ID."""
    client = get_client("billing", context=getattr(runtime, "context", None))
    data = await client.get(
        "/invoices/{invoice_id}",
        operation_id="getInvoice",
        path_params={"invoice_id": invoice_id},
    )
    return data if isinstance(data, str) else json.dumps(data)


TOOLS = [get_invoice]
PY
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
Declared the billing API read-only (api add --access read-only, api allow getInvoice) and added app/tools/billing.py (get_invoice); lint passes. Set BILLING_API_BASE_URL and BILLING_API_TOKEN in .env.
MD
