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

"""Model wiring through LangChain's `init_chat_model`.

`MODEL_PROVIDER` / `MODEL_NAME` select the agent model; `JUDGE_MODEL_PROVIDER`,
`JUDGE_MODEL_NAME`, `JUDGE_BASE_URL` and `JUDGE_API_KEY` select the judge model
and default to the agent's values. The agent code never names a provider.

Every provider model gets a request timeout and a retry budget:
`MODEL_TIMEOUT_S` (default 60 seconds per model request; `0` leaves the
provider SDK's own default) and `MODEL_MAX_RETRIES` (default 2). The run as a
whole is bounded separately by `RUN_TIMEOUT_S` (see `limits.py`).

Provider `fake` is a deterministic in-process chat model for tests and CI. It
is never offered by `graph-agents-cli create`.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator, Sequence
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import Field

from {{cookiecutter.agent_directory}}.app_utils.content import unfence_tool_output
from {{cookiecutter.agent_directory}}.app_utils.limits import SettingsError

# graph-agents-cli provider name -> LangChain `model_provider`.
PROVIDER_TO_LANGCHAIN: dict[str, str] = {
    "openai": "openai",
    "anthropic": "anthropic",
    "gemini": "google_genai",
    "openai-compatible": "openai",
}

# Provider -> the environment variable holding its key.
PROVIDER_KEY_VARS: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GOOGLE_API_KEY",
    "openai-compatible": "MODEL_API_KEY",
}

FAKE_PROVIDER = "fake"

DEFAULT_MODEL_TIMEOUT_S = 60.0
DEFAULT_MODEL_MAX_RETRIES = 2


def model_timeout_s() -> float | None:
    """`MODEL_TIMEOUT_S` (default 60); `0` means the provider SDK's own default."""
    raw = (os.environ.get("MODEL_TIMEOUT_S") or "").strip()
    if not raw:
        return DEFAULT_MODEL_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        raise SettingsError(f"MODEL_TIMEOUT_S={raw!r} is not a number.") from None
    if value < 0 or value != value or value == float("inf"):
        raise SettingsError(f"MODEL_TIMEOUT_S={raw} must be a finite number >= 0.")
    return value or None


def model_max_retries() -> int:
    """`MODEL_MAX_RETRIES` (default 2): retries of a failed model request."""
    raw = (os.environ.get("MODEL_MAX_RETRIES") or "").strip()
    if not raw:
        return DEFAULT_MODEL_MAX_RETRIES
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(f"MODEL_MAX_RETRIES={raw!r} is not an integer.") from None
    if value < 0:
        raise SettingsError(f"MODEL_MAX_RETRIES={value} must be >= 0.")
    return value


def model_limits() -> tuple[float | None, int]:
    """Both limits, validated (startup check)."""
    return model_timeout_s(), model_max_retries()


def model_settings(*, judge: bool = False) -> dict[str, str | None]:
    """Resolve provider, model name, base URL and key from the environment."""
    provider = os.environ.get("MODEL_PROVIDER", "openai").strip().lower()
    name = os.environ.get("MODEL_NAME", "").strip()
    base_url = os.environ.get("OPENAI_BASE_URL") or None
    api_key = os.environ.get(PROVIDER_KEY_VARS.get(provider, "MODEL_API_KEY")) or None
    if judge:
        provider = (os.environ.get("JUDGE_MODEL_PROVIDER") or provider).strip().lower()
        name = (os.environ.get("JUDGE_MODEL_NAME") or name).strip()
        base_url = os.environ.get("JUDGE_BASE_URL") or base_url
        api_key = (
            os.environ.get("JUDGE_API_KEY")
            or os.environ.get(PROVIDER_KEY_VARS.get(provider, "MODEL_API_KEY"))
            or api_key
        )
    return {"provider": provider, "name": name, "base_url": base_url, "api_key": api_key}


def model_label(*, judge: bool = False) -> str:
    """`<provider>/<model>` as recorded in run records and eval traces."""
    s = model_settings(judge=judge)
    return f"{s['provider']}/{s['name'] or 'fake' if s['provider'] == FAKE_PROVIDER else s['name']}"


def build_model(
    provider: str,
    name: str,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    **kwargs: Any,
) -> BaseChatModel:
    """Build a chat model for `provider`; only `fake` avoids a provider package."""
    if provider == FAKE_PROVIDER:
        return FakeChatModel(**kwargs)
    lc_provider = PROVIDER_TO_LANGCHAIN.get(provider)
    if lc_provider is None:
        raise ValueError(
            f"Unknown MODEL_PROVIDER {provider!r}; expected one of "
            f"{', '.join(PROVIDER_TO_LANGCHAIN)} (or 'fake' in tests)."
        )
    if not name:
        raise ValueError("MODEL_NAME is not set; put the model name in .env or the chart values.")
    params: dict[str, Any] = dict(kwargs)
    timeout = model_timeout_s()
    if timeout is not None:
        params.setdefault("timeout", timeout)
    params.setdefault("max_retries", model_max_retries())
    if api_key:
        params["api_key"] = api_key
    if provider == "openai-compatible":
        if not base_url:
            raise ValueError("OPENAI_BASE_URL is required when MODEL_PROVIDER=openai-compatible.")
        params["base_url"] = base_url
    from langchain.chat_models import init_chat_model

    return init_chat_model(name, model_provider=lc_provider, **params)


def get_model(**kwargs: Any) -> BaseChatModel:
    """The agent's chat model, from MODEL_PROVIDER / MODEL_NAME / OPENAI_BASE_URL."""
    s = model_settings()
    return build_model(
        s["provider"] or "", s["name"] or "", base_url=s["base_url"], api_key=s["api_key"], **kwargs
    )


def get_judge_model(**kwargs: Any) -> BaseChatModel:
    """The judge model, from JUDGE_* with the agent's values as defaults."""
    s = model_settings(judge=True)
    return build_model(
        s["provider"] or "", s["name"] or "", base_url=s["base_url"], api_key=s["api_key"], **kwargs
    )


# ---------------------------------------------------------------------------
# Deterministic fake model (tests and CI only)
# ---------------------------------------------------------------------------

_GREETINGS = ("hi", "hello", "hey", "good morning", "good afternoon", "good evening")

# Words of a tool name that say nothing about what the tool is for: `list_orders`
# is picked by "orders", never by "list".
_GENERIC_NAME_WORDS = frozenset(
    "all and api call create delete fetch find for from get list look lookup make new "
    "query run search set the tool update with".split()
)
# The subject of a request: the text after its last "in", "for" or "about".
_SUBJECT_MARKER = re.compile(r"\b(?:in|for|about)\s+", re.IGNORECASE)
# A value per JSON-schema type for the arguments a fake tool call must fill.
_PLACEHOLDER_BY_TYPE: dict[str, Any] = {
    "integer": 1,
    "number": 1,
    "boolean": False,
    "array": [],
    "object": {},
}


def _tool_spec(tool: Any) -> dict[str, Any] | None:
    """``{"name", "description", "parameters"}`` of a bound tool, or None when unreadable."""
    from langchain_core.utils.function_calling import convert_to_openai_tool

    try:
        function = convert_to_openai_tool(tool).get("function") or {}
    except Exception:  # an unusual tool object: bound, but never called by the fake model
        return None
    name = function.get("name")
    if not name:
        return None
    return {
        "name": str(name),
        "description": str(function.get("description") or ""),
        "parameters": function.get("parameters") or {},
    }


def _name_words(name: str) -> set[str]:
    words = {w for w in re.split(r"[^a-z0-9]+", name.lower()) if w}
    return {w for w in words if w not in _GENERIC_NAME_WORDS and len(w) > 2}


def _mentions(prompt: str, name: str) -> bool:
    """The prompt names the tool, or a distinctive word of its name (a plural counts)."""
    lowered = prompt.lower()
    if name.lower() in lowered:
        return True
    words = set(re.findall(r"[a-z0-9]+", lowered))
    for word in _name_words(name):
        singular = word[:-1] if word.endswith("s") else word
        if {word, singular, f"{singular}s"} & words:
            return True
    return False


def _subject(prompt: str) -> str:
    text = prompt.strip()
    last = None
    for match in _SUBJECT_MARKER.finditer(text):
        last = match
    subject = text[last.end() :] if last is not None else text
    return subject.strip().rstrip("?.!").strip() or text


def _fake_args(parameters: dict[str, Any], prompt: str) -> dict[str, Any]:
    """A value for every required argument: the request's subject for text, a fixed one else."""
    properties = parameters.get("properties") or {}
    args: dict[str, Any] = {}
    for name in parameters.get("required") or []:
        schema = properties.get(name) or {}
        if schema.get("enum"):
            args[name] = schema["enum"][0]
        else:
            args[name] = _PLACEHOLDER_BY_TYPE.get(schema.get("type"), _subject(prompt))
    return args


def _first_sentence(text: str) -> str:
    return text.strip().split("\n")[0].split(". ")[0].rstrip(".")


def _text_of(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") if isinstance(block, dict) else str(block) for block in content
        )
    return str(content)


def _usage(prompt: str, completion: str) -> dict[str, int]:
    inp = max(1, len(prompt.split()))
    out = max(1, len(completion.split()))
    return {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out}


class FakeChatModel(BaseChatModel):
    """Deterministic chat model for tests: the reply depends only on the input.

    It knows no tool by name: it calls whichever bound tool the request
    mentions, so the tests and the eval smoke cases keep working when the
    project's tools change. Replies (all stable across calls and safe under
    concurrency):
      * after a tool result: ``Here is what I found: <tool result>`` (the tool's own
        text: the `<tool_output>` fence the agent adds is taken off)
      * a judge prompt (mentions "score" and "JSON"): ``{"score": 5, "explanation": ...}``
      * a request that mentions a bound tool (its name, or a distinctive word of
        it: "weather" for `get_weather`, "orders" for `list_orders`): a call of
        the first such tool. Every required argument is filled: text with the
        request's subject (what follows its last "in", "for" or "about", else
        the whole request; "Paris" in "What is the weather in Paris?"), an enum
        with its first value, a number with 1, a flag with false, a list or an
        object empty
      * a greeting: ``Hello! How can I help you today?``
      * anything else: ``I am a fake model. I can use these tools: <name> (<first
        sentence of its description>), ... You said: <text>`` (without the tools
        part when none is bound)
    """

    bound_tools: list[str] = Field(default_factory=list)
    tool_specs: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "fake"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"bound_tools": list(self.bound_tools)}

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:  # type: ignore[override]
        specs = [spec for spec in (_tool_spec(t) for t in tools) if spec is not None]
        return self.model_copy(
            update={"bound_tools": [s["name"] for s in specs], "tool_specs": specs}
        )

    def _tool_call(self, prompt: str) -> AIMessage | None:
        for spec in self.tool_specs:
            if _mentions(prompt, spec["name"]):
                args = _fake_args(spec["parameters"], prompt)
                return AIMessage(
                    content="",
                    tool_calls=[{"name": spec["name"], "args": args, "id": f"call_{spec['name']}"}],
                    usage_metadata=_usage(prompt, spec["name"]),
                )
        return None

    def _reply(self, messages: list[BaseMessage]) -> AIMessage:
        last = messages[-1]
        if isinstance(last, ToolMessage):
            # The agent fences tool results (`UntrustedToolResults`); echo the tool's own text.
            found = unfence_tool_output(_text_of(last))
            text = f"Here is what I found: {found}"
            return AIMessage(content=text, usage_metadata=_usage(found, text))
        prompt = _text_of(last)
        lowered = prompt.lower()
        if "score" in lowered and "json" in lowered:
            text = json.dumps({"score": 5, "explanation": "fake judge: deterministic pass"})
            return AIMessage(content=text, usage_metadata=_usage(prompt, text))
        call = self._tool_call(prompt)
        if call is not None:
            return call
        if lowered.strip(" !.?,") in _GREETINGS or lowered.startswith(_GREETINGS):
            text = "Hello! How can I help you today?"
            return AIMessage(content=text, usage_metadata=_usage(prompt, text))
        tools = ", ".join(
            f"{s['name']} ({_first_sentence(s['description'])})" if s["description"] else s["name"]
            for s in self.tool_specs
        )
        can = f" I can use these tools: {tools}." if tools else ""
        text = f"I am a fake model.{can} You said: {prompt}"
        return AIMessage(content=text, usage_metadata=_usage(prompt, text))

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._reply(messages))])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        reply = self._reply(messages)
        if reply.tool_calls:
            call = reply.tool_calls[0]
            chunk = AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {
                        "name": call["name"],
                        "args": json.dumps(call["args"]),
                        "id": call["id"],
                        "index": 0,
                    }
                ],
                usage_metadata=reply.usage_metadata,
            )
            yield ChatGenerationChunk(message=chunk)
            return
        words = str(reply.content).split(" ")
        for i, word in enumerate(words):
            last = i == len(words) - 1
            piece = word if last else word + " "
            chunk = AIMessageChunk(
                content=piece, usage_metadata=reply.usage_metadata if last else None
            )
            if run_manager is not None:
                run_manager.on_llm_new_token(piece, chunk=ChatGenerationChunk(message=chunk))
            yield ChatGenerationChunk(message=chunk)
