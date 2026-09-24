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

"""Message content for each audience: plain text, the model's view of tool
results, and what clients see of failed tool calls.

* `content_to_text`: LangChain message content as plain text (SSE deltas, A2A
  parts, traces).
* `UntrustedToolResults`: agent middleware that fences every tool result the
  model reads in `<tool_output ... trust="untrusted">` tags. Tool results
  carry text other people or systems wrote (a customer's note, an upstream
  error body); the fence, with the default system prompt's rule to never
  follow instructions found inside it, keeps that text data rather than
  instructions. Only the model's request is changed: the thread's state,
  `tool.result` events and the thread history keep the tool's own output.
* `client_tool_result` / `client_message`: a failed tool call as a client sees
  it outside `APP_ENV=dev`: a generic message with an `error_id`, never the
  error text (exception names, policy rules and limits, upstream status and
  body), which only the model reads. Under `APP_ENV=dev` the text is kept.
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
from collections.abc import Mapping
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

logger = logging.getLogger(__name__)


def content_to_text(content: object) -> str:
    """Return a message's ``content`` as plain text.

    LangChain 1.x messages carry ``content`` either as a string or as a list of
    content blocks (e.g. ``[{"type": "text", "text": "hi"}]``). An A2A text ``Part``
    requires a string, so flatten the block form here.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for block in content:
            if isinstance(block, dict):
                out.append(str(block.get("text", "")))
            elif isinstance(block, str):
                out.append(block)
        return "".join(out)
    return ""


# ---------------------------------------------------------------------------
# The model's view: tool results are untrusted data
# ---------------------------------------------------------------------------

TOOL_OUTPUT_TAG = "tool_output"
# Our tag's name inside a tool result is renamed (`<tool_output` -> `<tool-output`),
# so the text cannot close the fence early or open a fake one.
_TAG_IN_TEXT = re.compile(r"<(\s*/?\s*)tool_output", re.IGNORECASE)


def _neutralise(text: str) -> str:
    return _TAG_IN_TEXT.sub(r"<\1tool-output", text)


def fence_tool_output(content: Any, *, name: str | None, status: str | None) -> Any:
    """`content` (a string or content blocks) inside the untrusted-data fence."""
    attrs = f' name="{html.escape(name or "", quote=True)}"'
    if status == "error":
        attrs += ' status="error"'
    opening = f'<{TOOL_OUTPUT_TAG}{attrs} trust="untrusted">\n'
    closing = f"\n</{TOOL_OUTPUT_TAG}>"
    if isinstance(content, str):
        return f"{opening}{_neutralise(content)}{closing}"
    if isinstance(content, list):
        blocks: list[Any] = [{"type": "text", "text": opening}]
        for block in content:
            if isinstance(block, str):
                blocks.append(_neutralise(block))
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                blocks.append({**block, "text": _neutralise(block["text"])})
            else:
                blocks.append(block)
        blocks.append({"type": "text", "text": closing})
        return blocks
    return f"{opening}{closing}"


def is_fenced(content: Any) -> bool:
    text = content if isinstance(content, str) else content_to_text(content)
    return text.startswith(f"<{TOOL_OUTPUT_TAG} ")


def unfence_tool_output(text: str) -> str:
    """The tool's own text from a fenced result (for fakes and tests that echo it)."""
    match = re.fullmatch(
        rf"<{TOOL_OUTPUT_TAG}[^>\n]*>\n(.*)\n</{TOOL_OUTPUT_TAG}>", text, flags=re.DOTALL
    )
    return match.group(1) if match else text


def fence_tool_messages(messages: list[Any]) -> list[Any]:
    """`messages` with every tool result fenced (a new list; the originals are untouched)."""
    out: list[Any] = []
    for message in messages:
        if isinstance(message, ToolMessage) and not is_fenced(message.content):
            message = message.model_copy(
                update={
                    "content": fence_tool_output(
                        message.content, name=message.name, status=message.status
                    )
                }
            )
        out.append(message)
    return out


class UntrustedToolResults(AgentMiddleware):
    """Fence every tool result in the model's request as untrusted data.

    Add it to `create_agent(..., middleware=[...])`. The system prompt tells the
    model that text inside `<tool_output>` is data and never instructions.
    It lowers the odds that planted text steers the agent; it does not make a
    tool safe to call with a record the user never named (see
    `api_client.require_user_mentioned` and `require_owner`).
    """

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        return handler(request.override(messages=fence_tool_messages(list(request.messages))))

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        return await handler(request.override(messages=fence_tool_messages(list(request.messages))))


# ---------------------------------------------------------------------------
# The client's view: failed tool calls
# ---------------------------------------------------------------------------

TOOL_ERROR_MESSAGE = "The tool call did not succeed."
_ERROR_TYPE = re.compile(r"^([A-Z][A-Za-z0-9_]*(?:Error|Exception|Refused)):")


def tool_error_id(thread_id: str | None, call_id: str | None) -> str:
    """A stable reference for one failed tool call: the same in the stream and in the history."""
    seed = f"tool-error:{thread_id or ''}:{call_id or ''}".encode()
    return hashlib.sha256(seed).hexdigest()[:16]


def tool_error_type(text: str) -> str | None:
    """The exception class a tool error names first (`ApiPolicyError: ...`), if any."""
    match = _ERROR_TYPE.match(text or "")
    return match.group(1) if match else None


def client_tool_result(
    data: Mapping[str, Any], *, thread_id: str | None, dev: bool, log: bool = True
) -> dict[str, Any]:
    """A `tool.result` event as a client may see it.

    A successful result is unchanged. A failed one (`is_error`) gets
    `error_id` and, outside `APP_ENV=dev`, a generic `result`; the text the
    model read stays out of the event. `log` records the failure (tool
    name, error type and id; the text only at DEBUG, since it can hold tool
    arguments and upstream data).
    """
    event = dict(data)
    if not event.get("is_error"):
        return event
    error_id = tool_error_id(thread_id, str(event.get("id") or ""))
    text = str(event.get("result") or "")
    if log:
        logger.warning(
            "tool call failed (error_id=%s)",
            error_id,
            extra={"tool": str(event.get("name") or ""), "error_type": tool_error_type(text)},
        )
        logger.debug("tool call failed (error_id=%s): %s", error_id, text)
    event["error_id"] = error_id
    if not dev:
        event["result"] = f"{TOOL_ERROR_MESSAGE} Reference: {error_id}."
    return event


def client_message(
    message: Mapping[str, Any], *, thread_id: str | None, dev: bool
) -> dict[str, Any]:
    """A thread-history message as a client may see it: a failed tool result as in the stream."""
    out = dict(message)
    if out.get("role") != "tool" or not out.get("is_error"):
        return out
    error_id = tool_error_id(thread_id, str(out.get("tool_call_id") or ""))
    out["error_id"] = error_id
    if not dev:
        out["content"] = f"{TOOL_ERROR_MESSAGE} Reference: {error_id}."
    return out
