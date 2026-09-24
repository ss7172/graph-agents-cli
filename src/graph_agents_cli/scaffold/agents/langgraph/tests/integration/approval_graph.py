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

"""The graph `tests/integration/test_approvals_server.py` serves with `langgraph dev`.

The agent as `agent.py` builds it (prompt, middleware, context schema), with
a test tool that cancels an order through `api_client`: a call the test's
`api-policy.yaml` gates. The JSON body comes from the file named by
`TEST_APPROVAL_BODY_FILE`, so a test can change the request between the pause
and the decision. Two more tools place and amend orders, for a policy whose
approval rules ask other approvers for other calls. Not collected by pytest
(no `test_` prefix).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from langchain.agents import create_agent
from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from {{cookiecutter.agent_directory}} import agent
from {{cookiecutter.agent_directory}}.app_utils.api_client import get_client
from {{cookiecutter.agent_directory}}.app_utils.limits import recursion_limit
from {{cookiecutter.agent_directory}}.app_utils.model import get_model


def _principal(context: Any) -> str:
    if isinstance(context, dict):
        return str(context.get("principal_id") or "")
    return str(getattr(context, "principal_id", "") or "")


@tool
async def cancel_order(order_id: str, runtime: ToolRuntime[Any]) -> str:
    """Cancel an order by its id."""
    context = getattr(runtime, "context", None)
    body = json.loads(Path(os.environ["TEST_APPROVAL_BODY_FILE"]).read_text(encoding="utf-8"))
    client = get_client("shop", context=context)
    data = await client.post(
        "/orders/{order_id}/cancel",
        operation_id="cancelOrder",
        path_params={"order_id": order_id},
        json_body=body,
    )
    return json.dumps({"upstream": data, "acting_as": _principal(context)})


@tool
async def place_order(item: str, runtime: ToolRuntime[Any]) -> str:
    """Place a new purchase of an item."""
    context = getattr(runtime, "context", None)
    client = get_client("shop", context=context)
    data = await client.post("/orders", operation_id="createOrder", json_body={"item": item})
    return json.dumps({"upstream": data, "acting_as": _principal(context)})


@tool
async def amend_order(order_id: str, runtime: ToolRuntime[Any]) -> str:
    """Amend an existing purchase with a gift note."""
    context = getattr(runtime, "context", None)
    client = get_client("shop", context=context)
    data = await client.patch(
        "/orders/{order_id}",
        operation_id="updateOrder",
        path_params={"order_id": order_id},
        json_body={"note": "gift"},
    )
    return json.dumps({"upstream": data, "acting_as": _principal(context)})


graph = create_agent(
    model=get_model(),
    # The fake model calls the first tool a message names: "Cancel ..." cancels,
    # "Place ..." places and "Amend ..." amends.
    tools=[cancel_order, place_order, amend_order],
    system_prompt=agent.SYSTEM_PROMPT,
    middleware=agent.middleware(),
    context_schema=agent.AgentContext,
    name="approval-test",
).with_config({"recursion_limit": recursion_limit()})
