# A plausible mistake: PUT instead of the PATCH the API defines (the policy refuses it at runtime).
cd ops-desk
cat > app/tools/ticket_status.py <<'PY'
"""Ticket status updates."""

import json
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from app.app_utils.api_client import get_client, require_user_mentioned

API_CALLS: list[dict[str, str]] = [
    {
        "api": "tickets",
        "method": "PATCH",
        "operation_id": "updateTicket",
        "path": "/tickets/{ticket_id}",
    },
]


@tool
async def set_ticket_status(ticket_id: str, status: str, runtime: ToolRuntime[Any]) -> str:
    """Set the status of ticket TICKET_ID to STATUS."""
    require_user_mentioned(ticket_id, runtime)
    client = get_client("tickets", context=getattr(runtime, "context", None))
    data = await client.put(
        "/tickets/{ticket_id}",
        operation_id="updateTicket",
        path_params={"ticket_id": ticket_id},
        json_body={"status": status},
    )
    return data if isinstance(data, str) else json.dumps(data)


TOOLS = [set_ticket_status]
PY
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
Added set_ticket_status.
MD
