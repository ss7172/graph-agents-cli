# Copyright 2026 Google LLC
# Modifications Copyright 2026 graph-agents-cli contributors
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

"""The agent: `graph` is a compiled LangGraph ReAct agent with NO checkpointer.

The model comes from `MODEL_PROVIDER` / `MODEL_NAME` through
`app_utils.model.get_model()`, so no provider is named here. The tools
come from `tools/`. Persistence is bound elsewhere: `fast_api_app.py`
attaches the checkpointer under the fastapi runtime and LangGraph Server owns
it under langgraph-server. Replacing `create_agent` with an explicit
`StateGraph` is a one-file change: keep exporting `graph`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.graph.state import CompiledStateGraph

from {{cookiecutter.agent_directory}}.app_utils.api_client import ApiCallError, ApiPolicyError
from {{cookiecutter.agent_directory}}.app_utils.model import get_model
from {{cookiecutter.agent_directory}}.tools import get_tools

load_dotenv()

SYSTEM_PROMPT = (
    "You are a helpful assistant. Use the available tools when they can answer the "
    "question; otherwise answer directly and concisely."
)


@dataclass
class AgentContext:
    """Per-run context set by the server: who is calling, for tools to act on their behalf."""

    principal_id: str = "anonymous"
    roles: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)


def _tool_error(request: Any, exc: Exception) -> ToolMessage:
    call = request.tool_call
    return ToolMessage(
        content=f"{type(exc).__name__}: {exc}",
        tool_call_id=call["id"],
        name=call["name"],
        status="error",
    )


class SurfaceApiErrors(AgentMiddleware):
    """Turn an API-policy refusal or a failed API call into a tool error the model can read.

    Without this the exception would abort the run; with it the refusal reaches
    the model as a ToolMessage with `status="error"` and the caller sees
    `tool.result` with `is_error: true`.
    """

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        try:
            return handler(request)
        except (ApiPolicyError, ApiCallError) as exc:
            return _tool_error(request, exc)

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        try:
            return await handler(request)
        except (ApiPolicyError, ApiCallError) as exc:
            return _tool_error(request, exc)


graph: CompiledStateGraph = create_agent(
    model=get_model(),
    tools=get_tools(),
    system_prompt=SYSTEM_PROMPT,
    middleware=[SurfaceApiErrors()],
    context_schema=AgentContext,
    name="{{cookiecutter.project_name}}",
)
