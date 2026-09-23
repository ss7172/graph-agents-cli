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

"""Example API tool: one explicit GET through the policy-enforcing client.

Rendered when the project declares an `api-policy.yaml`; it calls the first
API the seed policy declares. It shows the pattern every tool that calls an
external API follows: declare each call in `API_CALLS` (checked by
`graph-agents-cli lint`), send it through `app_utils.api_client.get_client`
(which refuses anything outside `api-policy.yaml` before sending), and pass the
run context so an `auth: forward` API receives the caller's own credential.
Replace the operation with a real one of your API, or delete this module.
"""

from __future__ import annotations

import json
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from {{cookiecutter.agent_directory}}.app_utils.api_client import get_client

API_NAME = "{{ cookiecutter.apis[0].name if cookiecutter.apis else 'example' }}"

API_CALLS: list[dict[str, str]] = [
    {
        "api": "{{ cookiecutter.apis[0].name if cookiecutter.apis else 'example' }}",
        "method": "GET",
        "operation_id": "getItem",
        "path": "/items/{item_id}",
    },
]


@tool
async def lookup_item(item_id: str, runtime: ToolRuntime) -> str:
    """Look up an item by its id and return it as JSON."""
    context: Any = getattr(runtime, "context", None)
    client = get_client(API_NAME, context=context)
    # The path stays the declared template; the client encodes `item_id` as one
    # segment and refuses `.`/`..`, so model input cannot reach another endpoint.
    # A refusal (ApiPolicyError) or a failed call (ApiCallError) propagates:
    # agent.py turns it into a tool error the model can read.
    data = await client.get(
        "/items/{item_id}", operation_id="getItem", path_params={"item_id": item_id}
    )
    return data if isinstance(data, str) else json.dumps(data)


TOOLS = [lookup_item]
