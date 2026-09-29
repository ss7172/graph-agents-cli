"""Account lookups over the accounts JSON-RPC API."""

import json
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from app.app_utils.api_client import get_client

API_CALLS: list[dict[str, str]] = [
    {
        "api": "accounts_rpc",
        "method": "POST",
        "operation_id": "getAccount",
        "rpc_method": "account.get",
        "path": "/jsonrpc",
    },
]


@tool
async def get_account(account_id: str, runtime: ToolRuntime[Any]) -> str:
    """Return the account ACCOUNT_ID."""
    client = get_client("accounts_rpc", context=getattr(runtime, "context", None))
    body = {"jsonrpc": "2.0", "id": 1, "method": "account.get", "params": {"id": account_id}}
    data = await client.post("/jsonrpc", json=body, operation_id="getAccount")
    return data if isinstance(data, str) else json.dumps(data)


TOOLS = [get_account]
