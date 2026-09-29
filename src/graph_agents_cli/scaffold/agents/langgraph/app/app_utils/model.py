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

OpenAI-API models (`openai`, `openai-compatible`) take two more settings,
checked at startup like the limits:

* `MODEL_REASONING_EFFORT`: `none`, `minimal`, `low`, `medium`, `high` or
  `xhigh` (what a model accepts varies); unset leaves the model's default.
  Sent as `reasoning_effort` on Chat Completions and as `reasoning.effort` on
  the Responses API.
* `MODEL_USE_RESPONSES_API`: `true` sends every request to the Responses API
  (`/v1/responses`), `false` to Chat Completions (`/v1/chat/completions`).
  Unset lets langchain-openai choose: Chat Completions, except for the models
  its own list says need the Responses API and for requests that use a
  Responses-only feature. Some models refuse function tools with a reasoning
  effort on Chat Completions ("use /v1/responses"): set `true` (or
  `MODEL_REASONING_EFFORT=none`). An OpenAI-compatible server that has no
  `/v1/responses` needs `false`, or unset.

Either set for another provider stops startup: it would be ignored. The judge
takes `JUDGE_REASONING_EFFORT` and `JUDGE_USE_RESPONSES_API`; unset, it keeps
the agent's values when it is an OpenAI-API model too, and drops them when it
is not.

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

from {{cookiecutter.agent_directory}}.app_utils.content import (
    unfence_agent_request,
    unfence_tool_output,
)
from {{cookiecutter.agent_directory}}.app_utils.limits import SettingsError
from {{cookiecutter.agent_directory}}.app_utils.structured import ANSWER_TOOL, validate

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

# The providers that speak the OpenAI API (langchain-openai's ChatOpenAI): the only ones
# the reasoning-effort and Responses-API settings apply to.
OPENAI_API_PROVIDERS = frozenset({"openai", "openai-compatible"})
# `MODEL_REASONING_EFFORT` / `JUDGE_REASONING_EFFORT`: what OpenAI's reasoning models accept
# (each model takes a subset; the API refuses the rest).
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


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


def _effort(name: str) -> str | None:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return None
    if raw not in REASONING_EFFORTS:
        raise SettingsError(f"{name}={raw!r} must be one of {', '.join(REASONING_EFFORTS)}.")
    return raw


def _switch(name: str) -> bool | None:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return None
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise SettingsError(f"{name}={raw!r} must be true or false (unset: langchain-openai chooses).")


def model_api_options(*, judge: bool = False) -> dict[str, Any]:
    """`{"reasoning_effort", "use_responses_api"}` of the agent's (or the judge's) model.

    Only the options that are set; `SettingsError` for a bad value, or for one
    set explicitly for a provider that is not an OpenAI-API one (`fake` takes
    and ignores them). The judge's own `JUDGE_*` values win; unset, it keeps the
    agent's when its provider is an OpenAI-API one too.
    """
    provider = model_settings(judge=judge)["provider"] or ""
    options: dict[str, Any] = {}
    for key, variable, read in (
        ("reasoning_effort", "REASONING_EFFORT", _effort),
        ("use_responses_api", "USE_RESPONSES_API", _switch),
    ):
        own = f"JUDGE_{variable}" if judge else f"MODEL_{variable}"
        value = read(own)
        explicit = value is not None
        if judge and value is None:
            value = read(f"MODEL_{variable}")
        if value is None:
            continue
        if provider in OPENAI_API_PROVIDERS:
            options[key] = value
        elif provider != FAKE_PROVIDER and explicit:
            raise SettingsError(
                f"{own} applies to an OpenAI-API model (MODEL_PROVIDER openai or "
                f"openai-compatible), not {provider!r}: unset it."
            )
    return options


def model_options() -> tuple[dict[str, Any], dict[str, Any]]:
    """The agent's and the judge's options, validated (startup check)."""
    return model_api_options(), model_api_options(judge=True)


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
    if lc_provider == "openai":
        # Ask for token usage on streamed responses (stream_options.include_usage).
        # langchain-openai does this by itself only for api.openai.com, so without it an
        # OpenAI-compatible endpoint (a proxy, gateway or self-hosted server) records zero
        # usage in run records and eval traces, and `expect.max_tokens` passes vacuously.
        params.setdefault("stream_usage", True)
    from langchain.chat_models import init_chat_model

    return init_chat_model(name, model_provider=lc_provider, **params)


def get_model(**kwargs: Any) -> BaseChatModel:
    """The agent's chat model, from MODEL_PROVIDER / MODEL_NAME / OPENAI_BASE_URL (and, for an
    OpenAI-API model, MODEL_REASONING_EFFORT / MODEL_USE_RESPONSES_API)."""
    s = model_settings()
    return build_model(
        s["provider"] or "",
        s["name"] or "",
        base_url=s["base_url"],
        api_key=s["api_key"],
        **{**model_api_options(), **kwargs},
    )


def get_judge_model(**kwargs: Any) -> BaseChatModel:
    """The judge model, from JUDGE_* with the agent's values as defaults."""
    s = model_settings(judge=True)
    return build_model(
        s["provider"] or "",
        s["name"] or "",
        base_url=s["base_url"],
        api_key=s["api_key"],
        **{**model_api_options(judge=True), **kwargs},
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
        if "const" in schema:  # a Literal of one value
            args[name] = schema["const"]
        elif schema.get("enum"):
            args[name] = schema["enum"][0]
        else:
            args[name] = _PLACEHOLDER_BY_TYPE.get(schema.get("type"), _subject(prompt))
    return args


def _misfit(schema: Any, value: Any, root: Any) -> bool:
    """Whether `value` breaks `schema` (a part of the response schema `root`)."""
    return bool(validate(schema, value, root=root))


def _fake_answer(schema: Any, text: str, root: Any = None) -> Any:
    """A value that fits `schema` (a response schema), built from `text`: deterministic.

    Every property is filled (as strict structured output does): a string with
    `text` (cut to `maxLength`), a number with its `minimum` (else 1), a flag
    with false, a list with `minItems` items, an enum or a choice with its
    first option. A `pattern` is not followed: a string that breaks it takes the
    first later choice that fits (of an `anyOf` or `oneOf`, or `null` in a type
    list), so an optional id with a pattern is null; with no such choice the
    answer does not fit (the tests' failing path).
    """
    root = schema if root is None else root
    if not isinstance(schema, dict):
        return text
    if isinstance(schema.get("$ref"), str) and schema["$ref"].startswith("#"):
        node: Any = root
        for key in [k for k in schema["$ref"][1:].split("/") if k]:
            node = node.get(key, {}) if isinstance(node, dict) else {}
        return _fake_answer(node, text, root)
    if "const" in schema:
        return schema["const"]
    if schema.get("enum"):
        return schema["enum"][0]
    for key in ("anyOf", "oneOf"):
        if schema.get(key):
            values = [_fake_answer(option, text, root) for option in schema[key]]
            fits = (v for v, o in zip(values, schema[key], strict=True) if not _misfit(o, v, root))
            return next(fits, values[0])
    if schema.get("allOf"):
        return _fake_answer(schema["allOf"][0], text, root)
    types = schema.get("type")
    types = types if isinstance(types, list) else [types]
    kind = next((t for t in types if t != "null"), None if "null" in types else "string")
    if kind is not None and "null" in types:
        value = _fake_answer({**schema, "type": kind}, text, root)
        return None if _misfit(schema, value, root) else value
    if kind == "object":
        properties = schema.get("properties") or {}
        return {name: _fake_answer(sub, text, root) for name, sub in properties.items()}
    if kind == "array":
        return [_fake_answer(schema.get("items") or {}, text, root)] * int(
            schema.get("minItems") or 0
        )
    if kind in ("integer", "number"):
        return schema.get("minimum", 1)
    if kind == "boolean":
        return False
    if kind is None:
        return None
    limit = schema.get("maxLength")
    return text[:limit] if isinstance(limit, int) else text


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
        text: the `<tool_output>` fence the agent adds is taken off; so is the
        `<agent_request>` fence of a request another agent presents)
      * a judge prompt (mentions "score" and "JSON"): ``{"score": 5, "explanation": ...}``
      * a request that mentions a bound tool (its name, or a distinctive word of
        it: "weather" for `get_weather`, "orders" for `list_orders`): a call of
        the first such tool. Every required argument is filled: text with the
        request's subject (what follows its last "in", "for" or "about", else
        the whole request; "Paris" in "What is the weather in Paris?"), an enum
        with its first value (a constant with its value), a number with 1, a
        flag with false, a list or an object empty
      * a greeting: ``Hello! How can I help you today?``
      * anything else: ``I am a fake model. I can use these tools: <name> (<first
        sentence of its description>), ... You said: <text>`` (without the tools
        part when none is bound)

    Structured answers (a response schema, `structured.py`): bound with a
    provider `response_format`, a reply that would be text is instead that text
    as a JSON answer (`_fake_answer`); bound with a forced `tool_choice` and the
    answer tool (the tool strategy), it is a call of the answer tool with that
    answer. It never calls the answer tool for a request that names it.
    """

    bound_tools: list[str] = Field(default_factory=list)
    tool_specs: list[dict[str, Any]] = Field(default_factory=list)
    # The response schema of a provider `response_format`, when one is bound.
    answer_schema: dict[str, Any] | None = None
    # A bound `tool_choice` that forces a tool call ("any", "required", true).
    forced_tool: bool = False

    @property
    def _llm_type(self) -> str:
        return "fake"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"bound_tools": list(self.bound_tools)}

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:  # type: ignore[override]
        specs = [spec for spec in (_tool_spec(t) for t in tools) if spec is not None]
        response_format = kwargs.get("response_format")
        schema = None
        if isinstance(response_format, dict):
            schema = (response_format.get("json_schema") or {}).get("schema")
        return self.model_copy(
            update={
                "bound_tools": [s["name"] for s in specs],
                "tool_specs": specs,
                "answer_schema": schema if isinstance(schema, dict) else None,
                "forced_tool": kwargs.get("tool_choice") in ("any", "required", True),
            }
        )

    def _structured(self, reply: AIMessage) -> AIMessage:
        """A text reply as a structured answer, when one is bound; else the reply itself."""
        if reply.tool_calls:
            return reply
        text = str(reply.content)
        if self.answer_schema is not None:
            answer = _fake_answer(self.answer_schema, text)
            content = json.dumps(answer, ensure_ascii=False)
            return AIMessage(content=content, usage_metadata=reply.usage_metadata)
        spec = next((s for s in self.tool_specs if s["name"] == ANSWER_TOOL), None)
        if self.forced_tool and spec is not None:
            answer = _fake_answer(spec["parameters"], text)
            return AIMessage(
                content="",
                tool_calls=[{"name": ANSWER_TOOL, "args": answer, "id": f"call_{ANSWER_TOOL}"}],
                usage_metadata=reply.usage_metadata,
            )
        return reply

    def _tool_call(self, prompt: str) -> AIMessage | None:
        for spec in self.tool_specs:
            if spec["name"] != ANSWER_TOOL and _mentions(prompt, spec["name"]):
                args = _fake_args(spec["parameters"], prompt)
                return AIMessage(
                    content="",
                    tool_calls=[{"name": spec["name"], "args": args, "id": f"call_{spec['name']}"}],
                    usage_metadata=_usage(prompt, spec["name"]),
                )
        return None

    def _reply(self, messages: list[BaseMessage]) -> AIMessage:
        return self._structured(self._text_reply(messages))

    def _text_reply(self, messages: list[BaseMessage]) -> AIMessage:
        last = messages[-1]
        if isinstance(last, ToolMessage):
            # The agent fences tool results (`UntrustedToolResults`); echo the tool's own text.
            found = unfence_tool_output(_text_of(last))
            text = f"Here is what I found: {found}"
            return AIMessage(content=text, usage_metadata=_usage(found, text))
        # A delegated run's request is fenced as the calling agent's: read the request itself.
        prompt = unfence_agent_request(_text_of(last))
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
