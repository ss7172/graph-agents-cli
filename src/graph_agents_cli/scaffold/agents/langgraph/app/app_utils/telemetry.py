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

"""Tracing setup: explicit opt-in, LangSmith or OpenTelemetry.

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
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_initialized = False


def tracing_enabled() -> bool:
    return (os.environ.get("TRACING_ENABLED") or "false").strip().lower() in ("1", "true", "yes")


def capture_mode() -> str:
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
    logger.info("Tracing over OTLP to %s (capture=%s).", endpoint, "full" if full else "metadata")


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
