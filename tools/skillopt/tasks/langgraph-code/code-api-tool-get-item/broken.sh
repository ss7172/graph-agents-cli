# A plausible mistake: call the API with httpx directly (no policy client, nothing declared).
cd stock-agent
cat > app/tools/inventory.py <<'PY'
"""Inventory lookups."""

import os

import httpx
from langchain_core.tools import tool

API_CALLS: list[dict[str, str]] = []


@tool
def get_item(item_id: str) -> str:
    """Return the inventory record for ITEM_ID as JSON."""
    base = os.environ["INVENTORY_API_BASE_URL"]
    token = os.environ["INVENTORY_API_TOKEN"]
    response = httpx.get(f"{base}/items/{item_id}", headers={"Authorization": f"Bearer {token}"})
    return response.text


TOOLS = [get_item]
PY
graph-agents-cli lint
echo "Added get_item." > "$GAC_FINAL"
