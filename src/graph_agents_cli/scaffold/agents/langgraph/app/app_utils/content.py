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
* `AnswerInvalidToolCalls`: agent middleware that answers a tool call whose
  arguments are not valid JSON with an error result, so the model can call
  again and the thread stays valid for the provider.
* `client_tool_result` / `client_message`: a failed tool call as a client sees
  it outside `APP_ENV=dev`: a generic message with an `error_id`, never the
  error text (exception names, policy rules and limits, upstream status and
  body), which only the model reads. Under `APP_ENV=dev` the text is kept.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelResponse
from langchain_core.messages import AIMessage, ToolMessage

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
# Content blocks a provider sends as media, not as text the model reads.
_MEDIA_BLOCK_TYPES = frozenset(
    {"image", "image_url", "audio", "input_audio", "video", "file", "document"}
)


def _plain_form(text: str) -> str:
    """`text` as a reader takes it: compatibility forms folded (a full-width
    less-than sign is `<`), invisible format characters (zero-width spaces, soft
    hyphens) dropped, HTML entities decoded (`&lt;`, twice encoded included)."""
    if text.isascii() and "&" not in text:
        return text  # nothing to fold, drop or decode
    plain = unicodedata.normalize("NFKC", text)
    plain = "".join(ch for ch in plain if unicodedata.category(ch) != "Cf")
    for _ in range(3):
        decoded = html.unescape(plain)
        if decoded == plain:
            break
        plain = decoded
    return plain


def _neutralise(text: str) -> str:
    text = _TAG_IN_TEXT.sub(r"<\1tool-output", text)
    plain = _plain_form(text)
    if plain is not text and _TAG_IN_TEXT.search(plain):
        # A look-alike of the tag (full-width brackets, a zero-width space in
        # it, HTML entities): the model reads the result in its plain form,
        # with the tag renamed there too.
        text = _TAG_IN_TEXT.sub(r"<\1tool-output", plain)
    return text


def _block_text(block: Any) -> str | None:
    """The text a content block puts before the model; None for a media block."""
    if isinstance(block, str):
        return block
    if isinstance(block, Mapping):
        if isinstance(block.get("text"), str):
            return block["text"]
        if block.get("type") in _MEDIA_BLOCK_TYPES:
            return None
    # Anything else (a `json` block, an unknown type) is data the model reads as text.
    return json.dumps(block, ensure_ascii=False, default=str)


def fence_tool_output(content: Any, *, name: str | None, status: str | None) -> Any:
    """`content` (a string or content blocks) inside the untrusted-data fence.

    Content blocks are fenced as one text: a provider joins adjacent text
    blocks, so a tag split across two of them (`<` + `/tool_output>`) would
    otherwise reach the model whole. Their text (and any other non-media
    block, as JSON) is joined and neutralised at once; media blocks (images,
    files, audio) follow it inside the fence.
    """
    attrs = f' name="{html.escape(name or "", quote=True)}"'
    if status == "error":
        attrs += ' status="error"'
    opening = f'<{TOOL_OUTPUT_TAG}{attrs} trust="untrusted">\n'
    closing = f"\n</{TOOL_OUTPUT_TAG}>"
    if isinstance(content, str):
        return f"{opening}{_neutralise(content)}{closing}"
    if isinstance(content, list):
        texts: list[str] = []
        media: list[Any] = []
        for block in content:
            text = _block_text(block)
            if text is None:
                media.append(block)
            else:
                texts.append(text)
        body = _neutralise("".join(texts))
        if not media:
            return f"{opening}{body}{closing}"
        return [
            {"type": "text", "text": f"{opening}{body}"},
            *media,
            {"type": "text", "text": closing},
        ]
    return f"{opening}{closing}"


# Set on the fenced copy the model reads (never on the thread's state). Whether
# a result is already fenced is known from this mark, never from its text: a
# tool result is text an upstream controls, and one that starts like a fence
# would otherwise reach the model unfenced, with the attributes it chose.
FENCED_MARK = "graph_agents_fenced"


def is_fenced(message: Any) -> bool:
    """Whether `message` is a copy `fence_tool_messages` made (by its mark, not its text)."""
    metadata = getattr(message, "response_metadata", None)
    return isinstance(metadata, Mapping) and metadata.get(FENCED_MARK) is True


def unfence_tool_output(text: str) -> str:
    """The tool's own text from a fenced result (for fakes and tests that echo it)."""
    match = re.fullmatch(
        rf"<{TOOL_OUTPUT_TAG}[^>\n]*>\n(.*)\n</{TOOL_OUTPUT_TAG}>", text, flags=re.DOTALL
    )
    return match.group(1) if match else text


def fence_tool_messages(messages: list[Any]) -> list[Any]:
    """`messages` with every tool result fenced (a new list; the originals are untouched).

    Every `ToolMessage` is fenced, whatever its text looks like; only the
    copies this function made (marked out of band) are left as they are.
    """
    out: list[Any] = []
    for message in messages:
        if isinstance(message, ToolMessage) and not is_fenced(message):
            message = message.model_copy(
                update={
                    "content": fence_tool_output(
                        message.content, name=message.name, status=message.status
                    ),
                    "response_metadata": {**message.response_metadata, FENCED_MARK: True},
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
# Tool calls whose arguments are not valid JSON
# ---------------------------------------------------------------------------

# The `type` LangChain gives a call whose arguments did not parse.
INVALID_TOOL_CALL_TYPE = "invalid_tool_call"
INVALID_TOOL_CALL_RESULT = (
    "The tool was not called: the arguments of this call were not valid JSON. "
    "Call the tool again with its arguments as one JSON object."
)


# How many times a model whose tool calls all had invalid arguments is asked again
# in one step (each is one more model call).
INVALID_TOOL_CALL_RETRIES = 2


def invalid_tool_call_results(messages: list[Any]) -> list[ToolMessage]:
    """An error result for each tool call in `messages` whose arguments are not valid JSON."""
    return [
        ToolMessage(
            content=INVALID_TOOL_CALL_RESULT,
            tool_call_id=str(call.get("id") or ""),
            name=str(call.get("name") or ""),
            status="error",
        )
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.invalid_tool_calls
    ]


def _makes_valid_calls(messages: list[Any]) -> bool:
    return any(isinstance(m, AIMessage) and m.tool_calls for m in messages)


class AnswerInvalidToolCalls(AgentMiddleware):
    """Answer each tool call whose arguments are not valid JSON, and ask the model again.

    A model can return arguments that do not parse (`{'query': 'SF'}`, a
    trailing comma, `query=SF`); OpenAI-compatible servers pass them on as
    written. LangChain keeps such a call in `invalid_tool_calls` and runs no
    tool, so without this the run ends with no reply and a call with no
    result, which LangChain sends back to the provider on the next turn and
    the provider refuses on every later turn.

    Here each such call gets an error result right after it, saying why. When
    the reply has no call that can run, the model is asked again in the same
    step (at most `retries` times; its replies and the results stay in the
    thread); the calls that can run go to the tools as usual. Put it before
    `UntrustedToolResults`, so the model reads those results fenced like any
    other. It adds no graph step: `RECURSION_LIMIT` counts the same.
    """

    def __init__(self, retries: int = INVALID_TOOL_CALL_RETRIES) -> None:
        super().__init__()
        self.retries = retries

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        answered: list[Any] = []  # replies whose calls could not run, and their results
        response = handler(request)
        for _ in range(self.retries):
            results = invalid_tool_call_results(response.result)
            if not results or _makes_valid_calls(response.result):
                break
            answered += [*response.result, *results]
            response = handler(request.override(messages=[*request.messages, *answered]))
        return self._response(answered, response)

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        answered: list[Any] = []
        response = await handler(request)
        for _ in range(self.retries):
            results = invalid_tool_call_results(response.result)
            if not results or _makes_valid_calls(response.result):
                break
            answered += [*response.result, *results]
            response = await handler(request.override(messages=[*request.messages, *answered]))
        return self._response(answered, response)

    @staticmethod
    def _response(answered: list[Any], response: Any) -> Any:
        """The step's messages: the earlier tries, then the last reply, its invalid calls answered."""
        results = invalid_tool_call_results(response.result)
        if not answered and not results:
            return response
        logger.info(
            "answered %d tool call(s) whose arguments were not valid JSON",
            len(results) + sum(isinstance(m, ToolMessage) for m in answered),
        )
        return ModelResponse(
            result=[*answered, *response.result, *results],
            structured_response=response.structured_response,
        )


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
