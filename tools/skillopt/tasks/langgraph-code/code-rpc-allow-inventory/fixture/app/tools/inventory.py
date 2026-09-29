"""Inventory lookups over the inventory JSON-RPC API."""

import json
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from app.app_utils.api_client import get_client

API_CALLS: list[dict[str, str]] = [
    {"api": "inventory_rpc", "method": "POST", "rpc_method": "item.get", "path": "/rpc"},
    {"api": "inventory_rpc", "method": "POST", "rpc_method": "item.search", "path": "/rpc"},
]


async def _call(runtime: ToolRuntime[Any], method: str, params: dict[str, Any]) -> str:
    client = get_client("inventory_rpc", context=getattr(runtime, "context", None))
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    data = await client.post("/rpc", json=body)
    return data if isinstance(data, str) else json.dumps(data)


@tool
async def get_item(sku: str, runtime: ToolRuntime[Any]) -> str:
    """Return the inventory record of the item SKU."""
    return await _call(runtime, "item.get", {"sku": sku})


@tool
async def search_items(query: str, runtime: ToolRuntime[Any]) -> str:
    """Search the inventory for items matching QUERY."""
    return await _call(runtime, "item.search", {"query": query})


TOOLS = [get_item, search_items]
