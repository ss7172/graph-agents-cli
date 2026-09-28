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

"""Logging and tracing setup.

Logging (`setup_logging()`, fastapi runtime): one handler on the root
logger, level `LOG_LEVEL` (default INFO), format `LOG_FORMAT=json|text`
(default `json`, `text` under `APP_ENV=dev`). Every record carries the
request id, and inside a run the run id, the thread id and the hashed
principal, from context variables set by the HTTP middleware and the chat
runtime. Nothing logs headers, bodies or credentials:

* uvicorn's access lines keep the path and drop the query string (a client
  that puts a token in the URL, `?access_token=...`, never gets it logged);
* the HTTP client libraries (`httpx`, `httpcore`, and `httpx2`/`httpcore2`
  that model provider SDKs use) log at WARNING only, since their INFO lines
  carry every outbound URL with its query and path values (tool arguments,
  customer ids); `app_utils.api_client` logs each outbound call itself with
  the API, method, operation id and path template instead;
* Python warnings are captured into the same handler (one JSON record each,
  logger `py.warnings`), and the value a pydantic serializer warning echoes
  (`input_value=...`, which can be a run context holding a forwarded
  credential) is redacted.

LangGraph Server configures its own handlers, format and level (its
`LOG_LEVEL`, `LOG_JSON`). Under `langgraph-server`, `setup_server_logging()`
applies the same three rules to them: the server's access lines
(`langgraph_api.server`) lose their `query_string` field, the HTTP client
libraries log at WARNING only (`quiet_client_loggers()`; `api_client`
does the same when imported, in every process that runs the graph), and
warnings are captured with their values redacted.

Tracing: explicit opt-in, LangSmith or OpenTelemetry.

Nothing is configured unless `TRACING_ENABLED=true`. Then:

* with `LANGSMITH_API_KEY` set, LangChain's native LangSmith tracing is enabled
  through the environment (`LANGSMITH_TRACING`, `LANGSMITH_PROJECT`,
  `LANGSMITH_ENDPOINT`);
* otherwise spans go over OTLP/HTTP to `OTEL_EXPORTER_OTLP_ENDPOINT` using
  OpenInference's LangChain instrumentation.

`TRACE_CAPTURE=metadata` (default) records structure, timing, model and tool
names, token counts, error types and identifiers (thread, run, hashed
principal) but no prompt or completion text, tool arguments or results, or
error messages. `TRACE_CAPTURE=full` records everything. The same policy is
applied to LangSmith (`hide_inputs`/`hide_outputs` plus a client anonymizer
that reduces a run's `error` to the exception class), to OpenInference (its
masking config plus an exporter that strips exception messages, stack traces
and the span status description) and to the run records the app keeps.

Correlation across services (`PROPAGATE_TRACE_HEADERS`, default true): every
call of the policy client to an `auth: forward` API (another agent, reached
with the caller's own credential) carries this request's `X-Request-ID` and,
when spans go over OTLP, the W3C trace context of the current span
(`outbound_trace_headers`, installed with `api_client.set_outbound_headers`;
`api_client.propagates` names the APIs), and a call to any other API carries
neither; an incoming `traceparent` is attached to the request's context
(`attach_trace_context`, in `middleware.RequestContextMiddleware`), so an agent
this one calls over A2A logs the same request id and its spans join this
trace. Under LangSmith tracing only the request id is passed on.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections.abc import Iterable
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from {{cookiecutter.agent_directory}}.app_utils.limits import SettingsError

logger = logging.getLogger(__name__)

_initialized = False
# Set once spans are exported over OTLP: the W3C trace context is then passed on and taken.
_otlp_active = False

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

# Correlation ids attached to every log record of the current request / run.
LOG_CONTEXT: dict[str, ContextVar[str | None]] = {
    "request_id": ContextVar("request_id", default=None),
    "run_id": ContextVar("run_id", default=None),
    "thread_id": ContextVar("thread_id", default=None),
    "principal_hash": ContextVar("principal_hash", default=None),
    # The agent presenting a delegated request (a client name, not personal data).
    "actor": ContextVar("actor", default=None),
}

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_HANDLER_FLAG = "_graph_agents_handler"
_STANDARD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
    "color_message",  # uvicorn's ANSI-coloured duplicate of the message
    "ids",  # set by TextFormatter
}


def bind_log_context(**values: str | None) -> None:
    """Set correlation ids (`request_id`, `run_id`, `thread_id`, `principal_hash`, `actor`)."""
    for name, value in values.items():
        LOG_CONTEXT[name].set(value)


def log_level() -> str:
    """`LOG_LEVEL` (default INFO); an unknown level is a startup error."""
    level = (os.environ.get("LOG_LEVEL") or "INFO").strip().upper()
    if level not in LOG_LEVELS:
        raise SettingsError(f"LOG_LEVEL={level!r} is not one of {', '.join(LOG_LEVELS)}.")
    return level


def log_format() -> str:
    """`LOG_FORMAT` (`json` or `text`); defaults to `text` under APP_ENV=dev, else `json`."""
    fmt = (os.environ.get("LOG_FORMAT") or "").strip().lower()
    if not fmt:
        return "text" if os.environ.get("APP_ENV") == "dev" else "json"
    if fmt not in ("json", "text"):
        raise SettingsError(f"LOG_FORMAT={fmt!r} must be 'json' or 'text'.")
    return fmt


class ContextFilter(logging.Filter):
    """Copy the correlation ids of the current context onto each record."""

    def filter(self, record: logging.LogRecord) -> bool:
        for name, var in LOG_CONTEXT.items():
            if getattr(record, name, None) is None:
                setattr(record, name, var.get())
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, message, correlation ids, extras."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name in LOG_CONTEXT:
            value = getattr(record, name, None)
            if value:
                payload[name] = value
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key in payload or key in LOG_CONTEXT or value is None:
                continue
            if key.startswith("_"):
                continue
            payload[key] = value if isinstance(value, str | int | float | bool) else repr(value)
        if record.exc_info:
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s%(ids)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        ids = " ".join(
            f"{name}={getattr(record, name)}"
            for name in ("request_id", "run_id")
            if getattr(record, name, None)
        )
        record.ids = f" [{ids}]" if ids else ""
        return super().format(record)


class _StderrHandler(logging.StreamHandler):
    """Writes to whatever `sys.stderr` is at emit time (test runners swap it)."""

    def __init__(self) -> None:
        logging.Handler.__init__(self)

    @property
    def stream(self) -> Any:  # type: ignore[override]
        return sys.stderr


class AccessLogFilter(logging.Filter):
    """Drop the query string from uvicorn's access lines.

    uvicorn logs `'%s - "%s %s HTTP/%s" %d'` with the full path and query as
    the third argument. Query strings carry whatever a client put there,
    credentials included (`?access_token=...`), so only the path is kept.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            path = args[2]
            if "?" in path:
                record.args = (*args[:2], path.split("?", 1)[0], *args[3:])
        return True


# A pydantic serializer warning names the value it could not serialize:
# `[field_name='context', input_value=AgentContext(...), input_type=...]`. The
# value's repr can hold a forwarded credential (a run context's attributes),
# so everything between `input_value=` and the last `, input_type=` of the line goes.
_WARNING_VALUE = re.compile(r"input_value=.*, input_type=")


class WarningRedactionFilter(logging.Filter):
    """Redact the values pydantic warnings echo (see `_WARNING_VALUE`)."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "input_value=" in message:
            record.msg = _WARNING_VALUE.sub("input_value=<redacted>, input_type=", message)
            record.args = None
        return True


# Loggers whose INFO lines carry full outbound URLs (query and path values):
# httpx, and httpx2, which the model provider SDKs use.
QUIET_LOGGERS = ("httpx", "httpcore", "httpx2", "httpcore2")
# LangGraph Server's access logger (a structlog event dict per request).
SERVER_ACCESS_LOGGER = "langgraph_api.server"


class ServerAccessLogFilter(logging.Filter):
    """Drop the query string from LangGraph Server's access lines.

    The server logs each request through structlog with a `query_string`
    field holding the raw query (credentials a client put in the URL
    included). Its records carry the event dict as `msg`; the field is
    removed before any handler formats it. The line's text holds the path only.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        event = record.msg
        if isinstance(event, dict) and "query_string" in event:
            record.msg = {k: v for k, v in event.items() if k != "query_string"}
        return True


def quiet_client_loggers() -> None:
    """`QUIET_LOGGERS` at WARNING: their INFO lines carry full outbound URLs."""
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def setup_server_logging() -> None:
    """langgraph-server: the logging rules of `setup_logging()` on the server's own handlers."""
    _add_filter_once(logging.getLogger(SERVER_ACCESS_LOGGER), ServerAccessLogFilter)
    _add_filter_once(logging.getLogger("uvicorn.access"), AccessLogFilter)
    quiet_client_loggers()
    logging.captureWarnings(True)
    _add_filter_once(logging.getLogger("py.warnings"), WarningRedactionFilter)


def setup_logging() -> None:
    """Route every logger (uvicorn's included) through one handler; idempotent."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_FLAG, False):
            root.removeHandler(handler)
    handler = _StderrHandler()
    setattr(handler, _HANDLER_FLAG, True)
    handler.addFilter(ContextFilter())
    handler.setFormatter(JsonFormatter() if log_format() == "json" else TextFormatter())
    root.addHandler(handler)
    root.setLevel(log_level())
    # uvicorn installs its own handlers before the app loads; send its records
    # (startup, errors, access lines) through the same handler instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers = []
        uv_logger.propagate = True
    _add_filter_once(logging.getLogger("uvicorn.access"), AccessLogFilter)
    quiet_client_loggers()
    # Warnings become records of this handler (JSON under LOG_FORMAT=json)
    # instead of raw multi-line text on stderr.
    logging.captureWarnings(True)
    _add_filter_once(logging.getLogger("py.warnings"), WarningRedactionFilter)


def _add_filter_once(target: logging.Logger, kind: type[logging.Filter]) -> None:
    if not any(isinstance(f, kind) for f in target.filters):
        target.addFilter(kind())


# ---------------------------------------------------------------------------
# Tracing
# ---------------------------------------------------------------------------


def tracing_enabled() -> bool:
    return (os.environ.get("TRACING_ENABLED") or "false").strip().lower() in ("1", "true", "yes")


CAPTURE_MODES = ("metadata", "full")


def trace_capture() -> str:
    """`TRACE_CAPTURE` (`metadata`, the default, or `full`); anything else is a startup error."""
    mode = (os.environ.get("TRACE_CAPTURE") or "metadata").strip().lower()
    if mode not in CAPTURE_MODES:
        raise SettingsError(f"TRACE_CAPTURE={mode!r} must be 'metadata' or 'full'.")
    return mode


def capture_mode() -> str:
    """The capture mode for a trace. Startup refuses a bad `TRACE_CAPTURE`
    (`trace_capture`); should one be read anyway, it records metadata only."""
    mode = (os.environ.get("TRACE_CAPTURE") or "metadata").strip().lower()
    return "full" if mode == "full" else "metadata"


def project_name() -> str:
    return (
        os.environ.get("LANGSMITH_PROJECT")
        or os.environ.get("OTEL_SERVICE_NAME")
        or Path.cwd().name
    )


def setup_telemetry() -> None:
    """Configure tracing once, per the environment. Safe to call repeatedly."""
    global _initialized
    if _initialized:
        return
    _initialized = True

    if not tracing_enabled():
        # A stray LANGSMITH_TRACING=true must not create a client: TRACING_ENABLED is the switch.
        os.environ["LANGSMITH_TRACING"] = "false"
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
        logger.info("Tracing disabled (TRACING_ENABLED != true).")
        return

    full = capture_mode() == "full"
    if os.environ.get("LANGSMITH_API_KEY"):
        _setup_langsmith(full)
    else:
        _setup_otlp(full)


_ERROR_TYPE = re.compile(r"\s*([A-Za-z_][\w.]*)")


def redact_error(payload: Any) -> Any:
    """LangSmith anonymizer: keep the exception class, drop the message and the stack trace.

    LangChain records `run.error` as `repr(exc)` plus the formatted traceback,
    and `LANGSMITH_HIDE_*` never touch it; only a client anonymizer does. The
    client applies it to the `{"error": <str>}` wrapper (and to run metadata,
    which passes through untouched), so only that exact shape is rewritten.
    """
    if (
        isinstance(payload, dict)
        and set(payload) == {"error"}
        and isinstance(payload["error"], str)
    ):
        m = _ERROR_TYPE.match(payload["error"])
        return {"error": (m.group(1) if m else "Error") + ": <redacted>"}
    return payload


def _setup_langsmith(full: bool) -> None:
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ.setdefault("LANGSMITH_PROJECT", project_name())
    hide = "false" if full else "true"
    # Kept for any client created elsewhere from the environment.
    os.environ["LANGSMITH_HIDE_INPUTS"] = hide
    os.environ["LANGSMITH_HIDE_OUTPUTS"] = hide
    try:
        import langsmith
    except ImportError as exc:  # pragma: no cover - dependency is in pyproject
        logger.warning("LangSmith tracing unavailable: %s", exc)
        return
    # One process-wide client: every LangChainTracer (env-driven, /chat, A2A,
    # the playground) picks it up, so the error redaction cannot be bypassed.
    client = langsmith.Client(
        hide_inputs=not full,
        hide_outputs=not full,
        anonymizer=None if full else redact_error,
    )
    langsmith.configure(client=client)
    logger.info(
        "Tracing to LangSmith project %r (capture=%s, endpoint=%s).",
        os.environ["LANGSMITH_PROJECT"],
        "full" if full else "metadata",
        os.environ.get("LANGSMITH_ENDPOINT", "default"),
    )


def _setup_otlp(full: bool) -> None:
    global _otlp_active
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        logger.warning(
            "TRACING_ENABLED=true but neither LANGSMITH_API_KEY nor OTEL_EXPORTER_OTLP_ENDPOINT is set; "
            "no exporter configured."
        )
        return
    try:
        from openinference.instrumentation import TraceConfig
        from openinference.instrumentation.langchain import LangChainInstrumentor
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:  # pragma: no cover - dependencies are in pyproject
        logger.warning("OpenTelemetry tracing unavailable: %s", exc)
        return

    resource = Resource.create(
        {
            "service.name": os.environ.get("OTEL_SERVICE_NAME") or project_name(),
            "service.version": os.environ.get("AGENT_VERSION", "0.1.0"),
            "deployment.environment": os.environ.get("APP_ENV", "dev"),
        }
    )
    provider = TracerProvider(resource=resource)
    exporter: Any = (
        OTLPSpanExporter()
    )  # reads OTEL_EXPORTER_OTLP_ENDPOINT (+ /v1/traces) and headers
    if not full:
        exporter = _MetadataOnlyExporter(exporter)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    hide = not full
    config = TraceConfig(
        hide_inputs=hide,
        hide_outputs=hide,
        hide_input_messages=hide,
        hide_output_messages=hide,
        hide_input_text=hide,
        hide_output_text=hide,
        hide_prompts=hide,
        hide_choices=hide,
        hide_llm_invocation_parameters=False,
        hide_llm_tools=False,
    )
    LangChainInstrumentor().instrument(tracer_provider=provider, config=config)
    _otlp_active = True
    logger.info("Tracing over OTLP to %s (capture=%s).", endpoint, "full" if full else "metadata")


# ---------------------------------------------------------------------------
# Correlation across services
# ---------------------------------------------------------------------------

TRACE_CONTEXT_HEADERS = ("traceparent", "tracestate")
_FALSE = ("0", "false", "no", "off")
# Where an incoming `traceparent` is continued under the default scope (0.3:
# `PROPAGATE_TRACE_HEADERS=peers`): the A2A routes only, so a caller of the public routes
# (`/chat`) cannot choose this agent's trace ids. `all` continues it on every path.
PEERS_INBOUND_TRACE_PREFIX = "/a2a/"


def propagate_trace_headers() -> bool:
    """`PROPAGATE_TRACE_HEADERS` (default true): pass the request id and trace context on
    to `auth: forward` APIs, and continue an incoming trace. false turns both off."""
    return (os.environ.get("PROPAGATE_TRACE_HEADERS") or "true").strip().lower() not in _FALSE


def _trace_context_propagator() -> Any:
    # W3C Trace Context only, whatever OTEL_PROPAGATORS says: no baggage leaves or enters.
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    return TraceContextTextMapPropagator()


def outbound_trace_headers() -> dict[str, str]:
    """The headers that carry this request's correlation to the agents it calls.

    `X-Request-ID`, the id every log record of this request carries (a service
    built from this template takes a caller's id as its own), and, when spans are
    exported over OTLP, the W3C trace context of the current span (`traceparent`,
    `tracestate`), so the callee's spans join this trace. The policy client adds
    them only to calls of `auth: forward` APIs (`api_client.propagates`). Empty
    under `PROPAGATE_TRACE_HEADERS=false`.
    """
    if not propagate_trace_headers():
        return {}
    headers: dict[str, str] = {}
    request_id = LOG_CONTEXT["request_id"].get()
    if request_id:
        headers["X-Request-ID"] = request_id
    if _otlp_active:
        carrier: dict[str, str] = {}
        _trace_context_propagator().inject(carrier, context=_current_span_context())
        headers.update({k: v for k, v in carrier.items() if k in TRACE_CONTEXT_HEADERS})
    return headers


def _current_span_context() -> Any:
    """The OTel context to pass on: the span of the LangChain run in progress (a tool's).

    OpenInference does not make its spans current in the OTel context (attaching
    one from a callback could leak it to later spans), so inside a tool the OTel
    current span is only what the request itself carried: nothing on a `/chat`
    call that came without a `traceparent`. OpenInference keeps the run's span,
    though, and the callee's spans then nest under the tool span that called it.
    None (the OTel current context) outside a LangChain run.
    """
    try:
        from openinference.instrumentation.langchain import get_current_span
        from opentelemetry import trace

        span = get_current_span()
    except Exception:
        return None
    if span is None or not span.get_span_context().is_valid:
        return None
    return trace.set_span_in_context(span)


def attach_trace_context(headers: Iterable[tuple[bytes, bytes]]) -> Any:
    """Continue the caller's trace: attach the W3C trace context of an incoming request.

    `headers` are the ASGI scope's. Returns the token `detach_trace_context`
    takes, or None when nothing was attached (no `traceparent`, spans not
    exported over OTLP, or `PROPAGATE_TRACE_HEADERS=false`). A malformed
    `traceparent` starts a new trace, as if there were none.
    """
    if not (_otlp_active and propagate_trace_headers()):
        return None
    carrier: dict[str, str] = {}
    for key, value in headers:
        name = key.decode("latin-1").lower()
        if name in TRACE_CONTEXT_HEADERS:
            carrier[name] = value.decode("latin-1")
    if "traceparent" not in carrier:
        return None
    from opentelemetry import context as otel_context

    return otel_context.attach(_trace_context_propagator().extract(carrier))


def detach_trace_context(token: Any) -> None:
    """Undo `attach_trace_context` (a None token: nothing was attached)."""
    if token is not None:
        from opentelemetry import context as otel_context

        otel_context.detach(token)


class _MetadataOnlyExporter:
    """Wraps an exporter and strips exception messages and stack traces from spans.

    Under `metadata` capture only the error *type* leaves the process: the
    `exception.message` / `exception.stacktrace` event attributes are dropped
    (`exception.type` stays) and an ERROR status loses its description, which
    OpenInference fills with `repr(exc)` plus the traceback (OTLP `status.message`).
    """

    _DROP = ("exception.message", "exception.stacktrace")

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def export(self, spans: Any) -> Any:
        return self._inner.export([self._redact(s) for s in spans])

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return bool(self._inner.force_flush(timeout_millis))

    def _redact(self, span: Any) -> Any:
        try:
            from opentelemetry.sdk.trace import Event
            from opentelemetry.trace import Status, StatusCode

            status = getattr(span, "_status", None)
            if status is not None and status.status_code is StatusCode.ERROR and status.description:
                span._status = Status(StatusCode.ERROR)
            events = getattr(span, "_events", None)
            if events:
                redacted = []
                for ev in events:
                    attrs = {k: v for k, v in (ev.attributes or {}).items() if k not in self._DROP}
                    redacted.append(Event(name=ev.name, attributes=attrs, timestamp=ev.timestamp))
                span._events = redacted
        except Exception:  # never fail export over redaction
            logger.debug("span redaction skipped", exc_info=True)
        return span
