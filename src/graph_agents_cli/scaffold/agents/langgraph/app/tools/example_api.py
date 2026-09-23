{% if cookiecutter.example_api -%}
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

Generated from `api-policy.yaml`: the call is the first GET its first API
allows (an `allowed_operations` entry, else an operation of its OpenAPI spec),
so it passes `graph-agents-cli lint` and the policy test as generated. It shows
the pattern every tool that calls an external API follows: declare each call
in `API_CALLS` (checked by `graph-agents-cli lint`), send it through
`app_utils.api_client.get_client` (which refuses anything outside
`api-policy.yaml` before sending), and pass the run context so an
`auth: forward` API receives the caller's own credential. Replace the call with
the one your agent needs, or delete this module.
"""

from __future__ import annotations

import json
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from {{cookiecutter.agent_directory}}.app_utils.api_client import get_client

API_CALLS: list[dict[str, str]] = [
    {
        "api": "{{ cookiecutter.example_api.api }}",
        "method": "{{ cookiecutter.example_api.method }}",
{%- if cookiecutter.example_api.operation_id %}
        "operation_id": "{{ cookiecutter.example_api.operation_id }}",
{%- endif %}
        "path": "{{ cookiecutter.example_api.path }}",
    },
]


@tool
async def call_{{ cookiecutter.example_api.api }}_api(
{%- for name in cookiecutter.example_api.params %}
    {{ name }}: str,
{%- endfor %}
    runtime: ToolRuntime,
) -> str:
    """{{ cookiecutter.example_api.method }} {{ cookiecutter.example_api.path }} on the {{ cookiecutter.example_api.api }} API and return the response as JSON."""
    context: Any = getattr(runtime, "context", None)
    client = get_client("{{ cookiecutter.example_api.api }}", context=context)
    # The path stays the declared template; the client encodes each path
    # parameter as one segment and refuses `.`/`..`, so model input cannot reach
    # another endpoint. A refusal (ApiPolicyError) or a failed call
    # (ApiCallError) propagates: agent.py turns it into a tool error the model
    # can read.
    data = await client.get(
        "{{ cookiecutter.example_api.path }}",
{%- if cookiecutter.example_api.operation_id %}
        operation_id="{{ cookiecutter.example_api.operation_id }}",
{%- endif %}
{%- if cookiecutter.example_api.params %}
        path_params={
{%- for name in cookiecutter.example_api.params %}
            "{{ name }}": {{ name }},
{%- endfor %}
        },
{%- endif %}
    )
    return data if isinstance(data, str) else json.dumps(data)


TOOLS = [call_{{ cookiecutter.example_api.api }}_api]
{% endif -%}
