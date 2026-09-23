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

    Replies (all stable across calls and safe under concurrency):
      * after a tool result: ``Here is what I found: <tool result>``
      * a question mentioning "weather" with a `get_weather` tool bound: a
        `get_weather(query=<place>)` tool call
      * a judge prompt (mentions "score" and "JSON"): ``{"score": 5, "explanation": ...}``
      * a greeting: ``Hello! How can I help you today?``
      * anything else: ``I am a fake model. I can check the weather. You said: <text>``
    """

    bound_tools: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "fake"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"bound_tools": list(self.bound_tools)}

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:  # type: ignore[override]
        names: list[str] = []
        for t in tools:
            name = getattr(t, "name", None) or getattr(t, "__name__", None)
            if name is None and isinstance(t, dict):
                name = t.get("name") or (t.get("function") or {}).get("name")
            if name:
                names.append(str(name))
        return self.model_copy(update={"bound_tools": names})

    def _reply(self, messages: list[BaseMessage]) -> AIMessage:
        last = messages[-1]
        if isinstance(last, ToolMessage):
            text = f"Here is what I found: {_text_of(last)}"
            return AIMessage(content=text, usage_metadata=_usage(_text_of(last), text))
        prompt = _text_of(last)
        lowered = prompt.lower()
        if "score" in lowered and "json" in lowered:
            text = json.dumps({"score": 5, "explanation": "fake judge: deterministic pass"})
            return AIMessage(content=text, usage_metadata=_usage(prompt, text))
        if "weather" in lowered and "get_weather" in self.bound_tools:
            match = re.search(r"\bin\s+([A-Za-z][A-Za-z .'-]*?)\s*[?.!]*$", prompt.strip())
            place = match.group(1).strip() if match else prompt.strip()
            return AIMessage(
                content="",
                tool_calls=[
                    {"name": "get_weather", "args": {"query": place}, "id": "call_get_weather"}
                ],
                usage_metadata=_usage(prompt, "get_weather"),
            )
        if lowered.strip(" !.?,") in _GREETINGS or lowered.startswith(_GREETINGS):
            text = "Hello! How can I help you today?"
            return AIMessage(content=text, usage_metadata=_usage(prompt, text))
        text = f"I am a fake model. I can check the weather. You said: {prompt}"
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
