# Copyright 2026 graph-agents-cli contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Example product-API tool: one explicit GET, registered only when a policy exists.

Shows the pattern every product tool follows: declare the call in
`PRODUCT_CALLS`, send it through `product_client` (which refuses anything
outside `product-policy.yaml` before sending), and forward the caller's
credential from the agent runtime context.
"""

from __future__ import annotations

import json
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from {{cookiecutter.agent_directory}}.app_utils.product_client import POLICY, get_product_client

PRODUCT_CALLS: list[dict[str, str]] = [
    {"method": "GET", "operation_id": "getItem", "path": "/items/{item_id}"},
]


@tool
async def product_lookup(item_id: str, runtime: ToolRuntime) -> str:
    """Look up an item in the product by its id and return it as JSON."""
    context: Any = getattr(runtime, "context", None)
    client = get_product_client(context) if context is not None else get_product_client()
    # The path stays the declared template; the client encodes `item_id` as one
    # segment and refuses `.`/`..`, so model input cannot reach another endpoint.
    # PolicyViolation propagates: agent.py turns it into a tool error the model reads.
    data = await client.request(
        "GET", operation_id="getItem", path="/items/{item_id}", path_params={"item_id": item_id}
    )
    return data if isinstance(data, str) else json.dumps(data)


# Registered only when the project declares a product policy.
TOOLS = [product_lookup] if POLICY.declared else []
