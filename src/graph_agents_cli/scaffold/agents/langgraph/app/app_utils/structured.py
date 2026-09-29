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

"""Structured final answers: the agent's final answer is JSON in the shape the project declares.

The project declares the shape in `<agent directory>/response_schema.json`
(`RESPONSE_SCHEMA_PATH` names another file), a JSON Schema whose root is an
object. Without the file the agent answers in text, as before. With it:

* The model is made to answer in that shape through LangChain's
  `response_format` strategies (`response_format()`): the provider's own
  structured output (OpenAI's `json_schema` response format, strict; Anthropic's
  and Gemini's native equivalents) where the model supports it, else a tool the
  model must call with its answer (`final_answer`, with `tool_choice` forcing a
  call at every step). `RESPONSE_FORMAT_STRATEGY` picks: `auto` (default: the
  provider's own when LangChain's model profile says the model has it, with
  the agent's tools bound; the tool otherwise), `provider` or `tool`.
* Every answer is checked against the schema here (`StructuredAnswer`):
  LangChain returns a raw JSON-schema answer unchecked under both strategies.
  An answer that does not fit, a reply that is not JSON, a final reply in
  plain text, or an answer given beside other tool calls (none of which runs)
  is sent back to the model with what is wrong, up to
  `MAX_ANSWER_ATTEMPTS` tries in the same step (the failed tries are not kept
  in the thread; their token usage is added to the answer's). When no try
  fits, the step fails with `StructuredAnswerError` and the run ends with the
  error code `invalid_structured_response`.
* The chat runtime (`chat.py`) delivers the answer: a completed run's
  `message.end` carries `structured_response` (the object), and the run's
  reply text is exactly its JSON text (one `message.delta`): nothing else the
  model wrote on the way is streamed. The A2A reply adds a data part with the
  object.

The checker supports a documented subset of JSON Schema (`SUPPORTED_KEYWORDS`):
a schema that uses anything else (`if`/`then`, `patternProperties`,
`prefixItems`, a remote `$ref`, a misspelt keyword, ...) stops startup rather
than being half-checked. `format` is an annotation only (as JSON Schema 2020-12
says by default), and `pattern` uses Python's regular expressions.

With the provider strategy on OpenAI Chat Completions, strict structured
output applies: langchain-openai makes every property of the answer required
and forbids extra ones (the model fills an optional property; give it a
`null` type where it may have no value), and makes every tool strict too (a
tool's optional arguments become required: the model passes a value for
each). A schema the provider refuses fails every run with the provider's
error: use `RESPONSE_FORMAT_STRATEGY=tool` for it.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelResponse
from langchain.agents.structured_output import (
    ProviderStrategy,
    StructuredOutputValidationError,
    ToolStrategy,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from {{cookiecutter.agent_directory}}.app_utils.limits import SettingsError

logger = logging.getLogger(__name__)

SCHEMA_FILENAME = "response_schema.json"
# The name of the answer's schema at the provider and of the answer tool (the tool
# strategy): the project's own title stays in the file, not in the name a provider checks.
ANSWER_TOOL = "final_answer"
DEFAULT_DESCRIPTION = (
    "Your final answer to the user, in this shape. Give it once you have what you need; "
    "it ends your turn."
)
STRATEGY_AUTO = "auto"
STRATEGY_PROVIDER = "provider"
STRATEGY_TOOL = "tool"
STRATEGIES = (STRATEGY_AUTO, STRATEGY_PROVIDER, STRATEGY_TOOL)
# Tries of one answer (the first and two corrections) before the run fails.
MAX_ANSWER_ATTEMPTS = 3
# How many schema problems one correction names.
MAX_REPORTED_PROBLEMS = 5
# What the answer tool's result says (the model reads it on the thread's later turns).
ANSWER_RECORDED = "The answer was given to the user."
# The result of a tool call made beside an answer (it did not run: an answer ends the turn).
OTHER_CALL_NOT_RUN = (
    f"Not run: it came with a {ANSWER_TOOL} call. Call tools first, then {ANSWER_TOOL} alone."
)
# What is wrong with an answer given beside other tool calls (the tool strategy): the
# calls would run after the answer was written, or pause the run for an approval with
# the answer already given, so the try is sent back and none of its calls runs.
ANSWER_NOT_ALONE = (
    "your answer came with other tool calls, which did not run: an answer ends your turn, "
    f"so call the tools you need first, then {ANSWER_TOOL} alone once you have their results"
)

# --- BEGIN SHARED RESPONSE SCHEMA RULES ---
# The JSON Schema subset a response schema may use, and the check of a schema
# against it. The same block is in graph_agents_cli/_response_schema.py, which
# `create --response-schema` and `lint` use: keep the two byte-identical
# (tests/dev/test_response_schema_parity.py), so a schema the CLI accepts
# starts the agent. No Jinja, no imports but json, math, re and typing.Any.

# Keywords that are checked, and annotations that are allowed and ignored.
VALIDATION_KEYWORDS = frozenset(
    {
        "type",
        "enum",
        "const",
        "properties",
        "required",
        "additionalProperties",
        "minProperties",
        "maxProperties",
        "items",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "anyOf",
        "oneOf",
        "allOf",
        "not",
        "$ref",
        "$defs",
        "definitions",
    }
)
ANNOTATION_KEYWORDS = frozenset(
    {
        "$schema",
        "$id",
        "$comment",
        "title",
        "description",
        "default",
        "examples",
        "deprecated",
        "readOnly",
        "writeOnly",
        "format",
    }
)
SUPPORTED_KEYWORDS = VALIDATION_KEYWORDS | ANNOTATION_KEYWORDS
JSON_TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})
_SCHEMA_LISTS = ("anyOf", "oneOf", "allOf")
_SCHEMA_MAPS = ("properties", "$defs", "definitions")
_SCHEMA_VALUES = ("items", "additionalProperties", "not")
_COUNTS = ("minProperties", "maxProperties", "minItems", "maxItems", "minLength", "maxLength")
_NUMBERS = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf")


def _pointer(schema: Any, ref: str) -> Any:
    """The subschema a local `$ref` (`#`, `#/$defs/name`, ...) names; KeyError when none."""
    if ref == "#":
        return schema
    if not ref.startswith("#/"):
        raise KeyError(ref)
    node = schema
    for raw in ref[2:].split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and key in node:
            node = node[key]
        elif isinstance(node, list) and key.isdigit() and int(key) < len(node):
            node = node[int(key)]
        else:
            raise KeyError(ref)
    return node


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def schema_problems(schema: Any) -> list[str]:
    """What stops `schema` from being a response schema; empty when it can be one.

    The root is an object schema (`"type": "object"`); every keyword anywhere
    is one `SUPPORTED_KEYWORDS` lists, with a value of the right kind; every
    `$ref` points inside the document; every `pattern` compiles.
    """
    if not isinstance(schema, dict):
        return ["the schema must be a JSON object"]
    problems: list[str] = []
    if schema.get("type") != "object":
        problems.append('$: the root must be an object schema ("type": "object")')

    def walk(node: Any, where: str) -> None:
        if isinstance(node, bool):
            return
        if not isinstance(node, dict):
            problems.append(f"{where}: a schema must be an object or a boolean")
            return
        for key, value in node.items():
            at = f"{where}.{key}"
            if key not in SUPPORTED_KEYWORDS:
                problems.append(f"{at}: `{key}` is not a keyword the answer check supports")
            elif key == "type":
                types = value if isinstance(value, list) else [value]
                if not types or not all(t in JSON_TYPES for t in types):
                    problems.append(f"{at}: must name JSON types ({', '.join(sorted(JSON_TYPES))})")
            elif key == "required":
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    problems.append(f"{at}: must be a list of property names")
            elif key == "enum":
                if not isinstance(value, list) or not value:
                    problems.append(f"{at}: must be a non-empty list")
            elif key in _COUNTS:
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    problems.append(f"{at}: must be a whole number >= 0")
            elif key in _NUMBERS:
                if not _is_number(value) or (key == "multipleOf" and value <= 0):
                    problems.append(
                        f"{at}: must be a number" + (" > 0" if key == "multipleOf" else "")
                    )
            elif key == "uniqueItems":
                if not isinstance(value, bool):
                    problems.append(f"{at}: must be true or false")
            elif key == "pattern":
                try:
                    re.compile(value)
                except (re.error, TypeError) as exc:
                    problems.append(f"{at}: not a regular expression ({exc})")
            elif key == "$ref":
                try:
                    _pointer(schema, value if isinstance(value, str) else "")
                except KeyError:
                    problems.append(f"{at}: {value!r} does not point inside this schema")
            elif key in _SCHEMA_LISTS:
                if not isinstance(value, list) or not value:
                    problems.append(f"{at}: must be a non-empty list of schemas")
                else:
                    for i, sub in enumerate(value):
                        walk(sub, f"{at}[{i}]")
            elif key in _SCHEMA_MAPS:
                if not isinstance(value, dict):
                    problems.append(f"{at}: must map names to schemas")
                else:
                    for name, sub in value.items():
                        walk(sub, f"{at}.{name}")
            elif key in _SCHEMA_VALUES:
                if key == "items" and isinstance(value, list):
                    problems.append(
                        f"{at}: must be one schema (a list of schemas is not supported)"
                    )
                else:
                    walk(value, at)

    walk(schema, "$")
    return problems


# --- END SHARED RESPONSE SCHEMA RULES ---


class StructuredAnswerError(RuntimeError):
    """No try of the model's final answer fitted the response schema."""


# ---------------------------------------------------------------------------
# The schema: where it is, and whether this checker can check it
# ---------------------------------------------------------------------------


def schema_path() -> Path | None:
    """`RESPONSE_SCHEMA_PATH` when set (it must exist); else the agent package's file, if any."""
    raw = (os.environ.get("RESPONSE_SCHEMA_PATH") or "").strip()
    if raw:
        path = Path(raw)
        if not path.is_file():
            raise SettingsError(f"RESPONSE_SCHEMA_PATH={raw!r} is not a file.")
        return path
    path = Path(__file__).resolve().parent.parent / SCHEMA_FILENAME
    return path if path.is_file() else None


_CACHE: dict[tuple[str, int], dict[str, Any]] = {}


def response_schema() -> dict[str, Any] | None:
    """The project's response schema, checked; None when it declares none (the mode is off).

    `SettingsError` for a file that is not JSON or not a schema this checker
    can check (startup names it with every other bad setting).
    """
    path = schema_path()
    if path is None:
        return None
    key = (str(path.resolve()), path.stat().st_mtime_ns)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SettingsError(f"{path.name}: not a JSON file ({exc}).") from None
    problems = schema_problems(schema)
    if problems:
        raise SettingsError(f"{path.name} cannot be the response schema: " + "; ".join(problems))
    _CACHE.clear()
    _CACHE[key] = schema
    return schema


def enabled() -> bool:
    """Whether the project declares a response schema (a bad one raises `SettingsError`)."""
    return response_schema() is not None


def response_strategy() -> str:
    """`RESPONSE_FORMAT_STRATEGY`: auto (default), provider or tool."""
    raw = (os.environ.get("RESPONSE_FORMAT_STRATEGY") or "").strip().lower()
    if not raw:
        return STRATEGY_AUTO
    if raw not in STRATEGIES:
        raise SettingsError(
            f"RESPONSE_FORMAT_STRATEGY={raw!r} must be one of {', '.join(STRATEGIES)}."
        )
    return raw


def structured_settings() -> tuple[dict[str, Any] | None, str]:
    """The schema and the strategy, validated (startup check)."""
    return response_schema(), response_strategy()


# ---------------------------------------------------------------------------
# The strategy the agent is built with
# ---------------------------------------------------------------------------


def _model_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """The schema as the model sees it: named `ANSWER_TOOL`, with a description."""
    return {
        **schema,
        "title": ANSWER_TOOL,
        "description": str(schema.get("description") or DEFAULT_DESCRIPTION),
    }


def provider_supported(model: Any, tools: list[Any]) -> bool:
    """Whether LangChain would give `model` (with `tools` bound) its provider's structured output.

    LangChain's own rule (`create_agent`'s `AutoStrategy`): the model's profile
    says it has structured output, except Gemini before 3 with tools.
    """
    try:
        from langchain.agents.factory import _supports_provider_strategy
    except ImportError:  # moved in a later LangChain: read the model profile
        profile = getattr(model, "profile", None) or {}
        return bool(profile.get("structured_output"))
    return bool(_supports_provider_strategy(model, tools=tools))


def response_format(
    model: Any, tools: list[Any]
) -> ToolStrategy[Any] | ProviderStrategy[Any] | None:
    """The `response_format` to build the agent with; None when there is no response schema.

    `auto` decides here, from the model and tools the agent is built with, so
    that the provider strategy is strict (LangChain's own `AutoStrategy` asks
    OpenAI for a best-effort `json_schema`, which the model need not follow).
    """
    schema, strategy = structured_settings()
    if schema is None:
        return None
    names = {getattr(t, "name", None) for t in tools}
    if ANSWER_TOOL in names:
        raise SettingsError(
            f"A tool is named {ANSWER_TOOL!r}, the name of the structured answer: rename the tool."
        )
    if strategy == STRATEGY_AUTO:
        strategy = STRATEGY_PROVIDER if provider_supported(model, tools) else STRATEGY_TOOL
    logger.info("structured answers: the %s strategy", strategy)
    if strategy == STRATEGY_PROVIDER:
        return ProviderStrategy(_model_schema(schema), strict=True)
    return ToolStrategy(_model_schema(schema), tool_message_content=ANSWER_RECORDED)


# ---------------------------------------------------------------------------
# The check: a JSON Schema subset validator
# ---------------------------------------------------------------------------


def _type_matches(value: Any, name: str) -> bool:
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    if name == "number":
        return _is_number(value)
    # integer: 1.0 is one too (JSON Schema counts a number with no fraction).
    return _is_number(value) and float(value).is_integer()


def _equal(a: Any, b: Any) -> bool:
    """JSON equality: `true` is not `1`, and `1` equals `1.0`."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if _is_number(a) and _is_number(b):
        return a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_equal(x, y) for x, y in zip(a, b, strict=True))
    return type(a) is type(b) and a == b


def validate(schema: Any, value: Any, *, root: Any = None, path: str = "$") -> list[str]:
    """Where `value` does not fit `schema` (one checked by `schema_problems`); empty when it fits."""
    root = schema if root is None else root
    if schema is True:
        return []
    if schema is False:
        return [f"{path}: no value is allowed here"]
    errors: list[str] = []
    if "$ref" in schema:
        errors += validate(_pointer(root, schema["$ref"]), value, root=root, path=path)
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_type_matches(value, t) for t in types):
            got = "null" if value is None else type(value).__name__
            return [*errors, f"{path}: must be {' or '.join(types)}, not {got}"]
    if "enum" in schema and not any(_equal(value, option) for option in schema["enum"]):
        errors.append(f"{path}: must be one of {json.dumps(schema['enum'], ensure_ascii=False)}")
    if "const" in schema and not _equal(value, schema["const"]):
        errors.append(f"{path}: must be {json.dumps(schema['const'], ensure_ascii=False)}")
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if name not in value:
                errors.append(f"{path}: missing the required property {name!r}")
        for name, item in value.items():
            if name in properties:
                errors += validate(properties[name], item, root=root, path=f"{path}.{name}")
            elif "additionalProperties" in schema:
                extra = schema["additionalProperties"]
                if extra is False:
                    errors.append(f"{path}: the property {name!r} is not allowed")
                else:
                    errors += validate(extra, item, root=root, path=f"{path}.{name}")
        if len(value) < schema.get("minProperties", 0):
            errors.append(f"{path}: needs at least {schema['minProperties']} properties")
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            errors.append(f"{path}: may have at most {schema['maxProperties']} properties")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: needs at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: may have at most {schema['maxItems']} items")
        if schema.get("uniqueItems") and any(
            _equal(value[i], value[j]) for i in range(len(value)) for j in range(i)
        ):
            errors.append(f"{path}: the items must be unique")
        if "items" in schema:
            for i, item in enumerate(value):
                errors += validate(schema["items"], item, root=root, path=f"{path}[{i}]")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: must be at least {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: must be at most {schema['maxLength']} characters")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            errors.append(f"{path}: must match the pattern {schema['pattern']!r}")
    if _is_number(value) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: must be <= {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: must be > {schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            errors.append(f"{path}: must be < {schema['exclusiveMaximum']}")
        if "multipleOf" in schema:
            ratio = value / schema["multipleOf"]
            if not math.isclose(ratio, round(ratio), rel_tol=0, abs_tol=1e-9):
                errors.append(f"{path}: must be a multiple of {schema['multipleOf']}")
    for sub in schema.get("allOf") or []:
        errors += validate(sub, value, root=root, path=path)
    if "anyOf" in schema and all(
        validate(sub, value, root=root, path=path) for sub in schema["anyOf"]
    ):
        errors.append(f"{path}: fits none of the anyOf choices")
    if "oneOf" in schema:
        fits = sum(1 for sub in schema["oneOf"] if not validate(sub, value, root=root, path=path))
        if fits != 1:
            errors.append(f"{path}: fits {fits} of the oneOf choices, not exactly one")
    if "not" in schema and not validate(schema["not"], value, root=root, path=path):
        errors.append(f"{path}: must not fit the `not` schema")
    return errors


# ---------------------------------------------------------------------------
# The middleware: check each final answer, and ask again when it does not fit
# ---------------------------------------------------------------------------


def _last_ai(messages: list[Any]) -> AIMessage | None:
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            return message
    return None


def _answer_call(message: AIMessage) -> dict[str, Any] | None:
    for call in message.tool_calls:
        if call.get("name") == ANSWER_TOOL:
            return call
    return None


def _correction(bad: AIMessage, problem: str) -> list[Any]:
    """The try and what the model reads about it: a tool error, or a note from the user's side."""
    call = _answer_call(bad)
    if call is not None:
        if problem == ANSWER_NOT_ALONE:
            note = f"Error: {problem}."
        else:
            note = (
                f"Error: your answer does not fit the required schema: {problem}. "
                f"Call {ANSWER_TOOL} again with a corrected answer."
            )
        # Every call of the try gets a result, or the provider refuses the request.
        return [
            bad,
            *(
                ToolMessage(
                    note if other is call else OTHER_CALL_NOT_RUN,
                    tool_call_id=other["id"],
                    name=other["name"],
                    status="error",
                )
                for other in bad.tool_calls
            ),
        ]
    note = (
        f"Your final answer does not fit the required JSON schema: {problem}. "
        "Reply again with only the corrected JSON answer."
    )
    return [bad, HumanMessage(note)]


def _add_usage(total: dict[str, Any] | None, usage: Any) -> dict[str, Any] | None:
    if not isinstance(usage, dict):
        return total
    summed = dict(total or {})
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        summed[key] = int(summed.get(key) or 0) + int(usage.get(key) or 0)
    return summed


class StructuredAnswer(AgentMiddleware):
    """Check the model's final answer against the response schema; ask again when it does not fit.

    A no-op without a response schema. Put it last in the middleware list: it
    wraps the model call innermost, so the correction it adds is not fenced as
    a tool result, and it sees the model's reply before any other middleware.
    """

    def __init__(self, attempts: int = MAX_ANSWER_ATTEMPTS) -> None:
        super().__init__()
        self.attempts = attempts

    def _problem(
        self, response: Any, schema: dict[str, Any]
    ) -> tuple[str | None, AIMessage | None]:
        """What is wrong with this reply as a final answer (None: nothing), and the reply.

        An answer must come alone: under the tool strategy LangChain takes an
        answer given beside other tool calls and still runs those calls, after
        the answer was written (a gated one pauses the run with the answer
        already given, and the resumed run then ends with none), so such a try
        is sent back whether or not the answer fits.
        """
        bad = _last_ai(response.result)
        answer = response.structured_response
        if answer is not None:
            if bad is not None and any(c.get("name") != ANSWER_TOOL for c in bad.tool_calls):
                return ANSWER_NOT_ALONE, bad
            errors = validate(schema, answer)
            if not errors:
                return None, bad
            return "; ".join(errors[:MAX_REPORTED_PROBLEMS]), bad
        if bad is None or bad.tool_calls or bad.invalid_tool_calls:
            return None, bad  # not a final reply: tools run, or AnswerInvalidToolCalls answers
        return (
            f"the reply is plain text, not an answer: call {ANSWER_TOOL} with your answer",
            bad,
        )

    def _prepare(self, request: Any) -> dict[str, Any] | None:
        if request.response_format is None:
            return None
        return response_schema()

    def _settle(self, response: Any, tries: list[AIMessage]) -> Any:
        """The reply that fits, with the failed tries' token usage added to its own."""
        if not tries:
            return response
        answer = _last_ai(response.result)
        usage = answer.usage_metadata if answer is not None else None
        for bad in tries:
            usage = _add_usage(usage, bad.usage_metadata)
        logger.info("structured answer: it fitted the schema after %d correction(s)", len(tries))
        result = [
            m.model_copy(update={"usage_metadata": usage}) if m is answer and usage else m
            for m in response.result
        ]
        return ModelResponse(result=result, structured_response=response.structured_response)

    def _next(self, request: Any, extra: list[Any], problem: str, attempt: int) -> Any:
        if attempt + 1 >= self.attempts:
            raise StructuredAnswerError(
                f"The answer did not fit the response schema after {self.attempts} tries: {problem}"
            )
        logger.info("structured answer: try %d did not fit; asking again", attempt + 1)
        return request.override(messages=[*request.messages, *extra])

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        schema = self._prepare(request)
        if schema is None:
            return handler(request)
        tries: list[AIMessage] = []
        extra: list[Any] = []
        current = request
        for attempt in range(self.attempts):
            try:
                response = handler(current)
            except StructuredOutputValidationError as exc:
                problem, bad = f"the reply is not valid JSON ({exc.source})", exc.ai_message
            else:
                problem, bad = self._problem(response, schema)
                if problem is None:
                    return self._settle(response, tries)
            tries.append(bad)
            extra += _correction(bad, problem)
            current = self._next(request, extra, problem, attempt)
        raise AssertionError("unreachable")

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        schema = self._prepare(request)
        if schema is None:
            return await handler(request)
        tries: list[AIMessage] = []
        extra: list[Any] = []
        current = request
        for attempt in range(self.attempts):
            try:
                response = await handler(current)
            except StructuredOutputValidationError as exc:
                problem, bad = f"the reply is not valid JSON ({exc.source})", exc.ai_message
            else:
                problem, bad = self._problem(response, schema)
                if problem is None:
                    return self._settle(response, tries)
            tries.append(bad)
            extra += _correction(bad, problem)
            current = self._next(request, extra, problem, attempt)
        raise AssertionError("unreachable")


def answer_text(answer: Any) -> str:
    """The answer's JSON text, as the reply text of a structured run."""
    return json.dumps(answer, ensure_ascii=False)
