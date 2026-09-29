"""Wallet balances and history over the wallet service's JSON-RPC API."""

import json
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from app.app_utils.api_client import get_client

API_CALLS: list[dict[str, str]] = [
    {"api": "wallet_rpc", "method": "POST", "rpc_method": "wallet.balance", "path": "/rpc"},
    {"api": "wallet_rpc", "method": "POST", "rpc_method": "wallet.history", "path": "/rpc"},
]


async def _call(runtime: ToolRuntime[Any], method: str, params: dict[str, Any]) -> str:
    client = get_client("wallet_rpc", context=getattr(runtime, "context", None))
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    data = await client.post("/rpc", json=body)
    return data if isinstance(data, str) else json.dumps(data)


@tool
async def wallet_balance(wallet_id: str, runtime: ToolRuntime[Any]) -> str:
    """Return the balance of the wallet WALLET_ID."""
    return await _call(runtime, "wallet.balance", {"wallet": wallet_id})


@tool
async def wallet_history(wallet_id: str, limit: int, runtime: ToolRuntime[Any]) -> str:
    """Return the last LIMIT transactions of the wallet WALLET_ID."""
    return await _call(runtime, "wallet.history", {"wallet": wallet_id, "limit": limit})


TOOLS = [wallet_balance, wallet_history]
