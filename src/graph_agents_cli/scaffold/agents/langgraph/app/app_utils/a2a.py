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

"""A2A protocol layer: the executor, the agent card, and the routes (D15).

The executor drives the same invocation path as `/chat` (`ChatRuntime`), so
the A2A `contextId` is the chat thread id and the same policy, run records
and thread ownership apply. The card is served at
`/a2a/<agent_directory>/.well-known/agent-card.json` and JSON-RPC at
`/a2a/<agent_directory>`; both are guarded by the policy middleware in
`fast_api_app.py` (`card.read` / `a2a.invoke`), and the card advertises the
resulting security scheme.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from a2a.helpers import new_task_from_user_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.server.routes.common import DefaultServerCallContextBuilder
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    APIKeySecurityScheme,
    HTTPAuthSecurityScheme,
    Part,
    SecurityScheme,
)
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH, PROTOCOL_VERSION_1_0
from fastapi import FastAPI
from starlette.requests import Request

from {{cookiecutter.agent_directory}}.app_utils.auth import PRODUCT_SESSION, Principal, policy_name
from {{cookiecutter.agent_directory}}.app_utils.chat import (
    EVENT_DELTA,
    EVENT_ERROR,
    RUNTIME,
    ChatRequest,
)

logger = logging.getLogger(__name__)

# Mount name for the A2A endpoint: the agent directory, so it matches the
# `graph-agents-cli run --mode a2a` default.
A2A_NAME = os.environ.get("A2A_NAME") or "{{cookiecutter.agent_directory}}"
A2A_RPC_PATH = f"/a2a/{A2A_NAME}"
A2A_CARD_PATH = f"{A2A_RPC_PATH}{AGENT_CARD_WELL_KNOWN_PATH}"
DESCRIPTION = "{{cookiecutter.project_name}}: a LangGraph agent served over the A2A protocol."


def advertised_base_url() -> str:
    """Base URL for the agent card: APP_URL, else the host and port we bind.

    The chart sets APP_URL from `appUrl` or the gateway/ingress hostname; a
    deployment without one (APP_ENV other than dev) is warned about, because
    A2A clients dial the card's URL, not the URL they fetched the card from.
    """
    if app_url := os.environ.get("APP_URL"):
        return app_url.rstrip("/")
    host = os.environ.get("HOST", "127.0.0.1")
    if host in ("0.0.0.0", "::", ""):  # what we bind is not what clients dial
        host = "127.0.0.1"
    fallback = f"http://{host}:{os.environ.get('PORT', '8000')}"
    if (os.environ.get("APP_ENV") or "dev").strip().lower() != "dev":
        logger.warning(
            "APP_URL is not set: the A2A agent card advertises %s (the bind address), which "
            "remote clients cannot reach. Set appUrl (or a gateway/ingress hostname) in the "
            "chart values, or APP_URL in the environment.",
            fallback,
        )
    return fallback


class PolicyContextBuilder(DefaultServerCallContextBuilder):
    """The default call context (headers, auth) plus the principal set by the policy middleware."""

    def build(self, request: Request) -> ServerCallContext:
        context = super().build(request)
        context.state["principal"] = getattr(request.state, "principal", None)
        return context


class LangGraphAgentExecutor(AgentExecutor):
    """Bridge the A2A request lifecycle to the chat runtime."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        user_input = context.get_user_input()
        task = context.current_task
        if task is None:
            # First message of a conversation. The Task itself has to reach the
            # queue before any status update, or the server rejects the stream.
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.start_work()

        state = context.call_context.state if context.call_context is not None else {}
        principal = state.get("principal") or Principal(id="anonymous")
        req = ChatRequest(message=user_input, thread_id=task.context_id or None)
        try:
            thread_id = await RUNTIME.resolve_thread(principal, req)
        except Exception as exc:  # ownership or server errors end the task
            await updater.failed(
                updater.new_agent_message([Part(text=f"{type(exc).__name__}: {exc}")])
            )
            return

        # Stream the reply as task artifacts (A2A clients read the reply from
        # artifacts, not from the final status message).
        artifact_id = uuid.uuid4().hex
        streamed = False
        async for event, data in RUNTIME.stream(principal, req, thread_id):
            if event == EVENT_DELTA and data.get("text"):
                await updater.add_artifact(
                    [Part(text=data["text"])],
                    artifact_id=artifact_id,
                    name="response",
                    append=streamed,
                    last_chunk=False,
                )
                streamed = True
            elif event == EVENT_ERROR:
                await updater.failed(
                    updater.new_agent_message(
                        [Part(text=f"{data.get('code')}: {data.get('message')}")]
                    )
                )
                return
        if not streamed:
            await updater.add_artifact([Part(text="")], artifact_id=artifact_id, name="response")
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("This agent does not support cancellation.")


def _security() -> tuple[dict[str, SecurityScheme], str]:
    if policy_name() == PRODUCT_SESSION:
        return (
            {
                "session": SecurityScheme(
                    api_key_security_scheme=APIKeySecurityScheme(
                        name="X-Session-Token",
                        location="header",
                        description="Product session token (forwarded session cookie also accepted).",
                    )
                )
            },
            "session",
        )
    return (
        {
            "bearer": SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(
                    scheme="bearer", description="Shared bearer key (API_KEY)."
                )
            )
        },
        "bearer",
    )


def agent_card() -> AgentCard:
    rpc_url = f"{advertised_base_url()}{A2A_RPC_PATH}"
    schemes, required = _security()
    card = AgentCard(
        name=A2A_NAME,
        description=DESCRIPTION,
        # One 1.0 interface: advertising 0.3 as well steers 1.0 clients to the
        # wrong version. 0.3 clients are still served on the same URL
        # (enable_v0_3_compat below).
        supported_interfaces=[
            AgentInterface(
                url=rpc_url, protocol_binding="JSONRPC", protocol_version=PROTOCOL_VERSION_1_0
            ),
        ],
        version=os.environ.get("AGENT_VERSION", "0.1.0"),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=True),
        security_schemes=schemes,
        skills=[
            AgentSkill(
                id="chat",
                name="chat",
                description="Hold a conversation with the agent.",
                tags=["chat", "langgraph"],
            )
        ],
    )
    requirement = card.security_requirements.add()
    requirement.schemes[required].SetInParent()
    return card


def add_a2a_routes(app: FastAPI) -> Any:
    """Mount the JSON-RPC endpoint and the agent card on the app."""
    card = agent_card()
    request_handler = DefaultRequestHandler(
        agent_executor=LangGraphAgentExecutor(),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card, card_url=A2A_CARD_PATH),
        # v0.3 compat keeps older A2A clients working against the same endpoint.
        jsonrpc_routes=create_jsonrpc_routes(
            request_handler,
            rpc_url=A2A_RPC_PATH,
            context_builder=PolicyContextBuilder(),
            enable_v0_3_compat=True,
        ),
    )
    return card
