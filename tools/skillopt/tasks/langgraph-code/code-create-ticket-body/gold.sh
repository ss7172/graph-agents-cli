cd service-desk
cat > app/tools/tickets.py <<'PY'
"""Ticket creation."""

import json
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from app.app_utils.api_client import get_client

API_CALLS: list[dict[str, str]] = [
    {
        "api": "tickets",
        "method": "POST",
        "operation_id": "createTicket",
        "path": "/tickets",
    },
]


@tool
async def create_ticket(title: str, description: str, runtime: ToolRuntime[Any]) -> str:
    """Open a ticket with TITLE and DESCRIPTION."""
    client = get_client("tickets", context=getattr(runtime, "context", None))
    data = await client.post(
        "/tickets",
        operation_id="createTicket",
        json_body={"title": title, "description": description},
    )
    return data if isinstance(data, str) else json.dumps(data)


TOOLS = [create_ticket]
PY
graph-agents-cli lint
cat > "$GAC_FINAL" <<'MD'
Added app/tools/tickets.py (create_ticket, POST /tickets with json_body); lint passes.
MD
