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

"""Capture policy: under `metadata` no error message or stack trace leaves the process."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from {{cookiecutter.agent_directory}}.app_utils import telemetry
from {{cookiecutter.agent_directory}}.app_utils.limits import SettingsError

SECRET = "SECRET-PROMPT-TEXT"


@pytest.fixture
def tracing_env(monkeypatch: pytest.MonkeyPatch):
    # Every variable setup_telemetry() writes goes through monkeypatch so the
    # environment (and LangChain's tracing switch) is restored for later tests.
    for var in (
        "LANGSMITH_TRACING",
        "LANGCHAIN_TRACING_V2",
        "LANGSMITH_HIDE_INPUTS",
        "LANGSMITH_HIDE_OUTPUTS",
    ):
        monkeypatch.setenv(var, "false")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "http://127.0.0.1:9")  # never a real upload
    monkeypatch.setenv("TRACING_ENABLED", "true")
    monkeypatch.setenv("TRACE_CAPTURE", "metadata")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_test")
    monkeypatch.setenv("LANGSMITH_PROJECT", "test-project")
    monkeypatch.setattr(telemetry, "_initialized", False)
    yield
    import langsmith

    langsmith.configure(client=None)
    monkeypatch.setattr(telemetry, "_initialized", False)


def test_redact_error_keeps_only_the_exception_class() -> None:
    error = f"ValueError('unknown item {SECRET}')Traceback (most recent call last):\n  File ..."
    assert telemetry.redact_error({"error": error}) == {"error": "ValueError: <redacted>"}
    # Metadata and other payloads pass through untouched (the client runs the
    # anonymizer over run metadata as well).
    assert telemetry.redact_error({"error": "x", "other": 1}) == {"error": "x", "other": 1}
    assert telemetry.redact_error({"thread_id": "t"}) == {"thread_id": "t"}
    assert telemetry.redact_error(["not", "a", "dict"]) == ["not", "a", "dict"]


async def test_langsmith_uploads_no_error_message_under_metadata_capture(
    tracing_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from langchain_core.runnables import RunnableLambda
    from langchain_core.tracers.langchain import LangChainTracer
    from langsmith import Client

    bodies: list[bytes] = []

    def fake_request(self: Any, method: str, pathname: str, *, request_kwargs: Any, **_: Any):
        content = request_kwargs.get("content") or request_kwargs.get("data") or b""
        if isinstance(content, str):
            content = content.encode()
        if content:
            bodies.append(content)

        class _Resp:
            status_code = 200

            def json(self) -> dict:
                return {}

            def raise_for_status(self) -> None:
                pass

        return _Resp()

    monkeypatch.setattr(Client, "request_with_retries", fake_request)
    telemetry.setup_telemetry()

    import langsmith

    client = langsmith.run_trees.get_cached_client()
    assert client._hide_inputs is True and client._hide_outputs is True
    tracer = LangChainTracer(client=client, project_name="test-project")

    def _boom(_x: Any) -> Any:
        raise ValueError(f"unknown item {SECRET}")

    with pytest.raises(ValueError):
        RunnableLambda(_boom).invoke("hi", config={"callbacks": [tracer]})
    tracer.wait_for_futures()
    client.flush()

    payloads = b"".join(bodies).decode("utf-8", errors="replace")
    assert payloads, "nothing was uploaded"
    assert SECRET not in payloads
    assert "Traceback" not in payloads
    assert "ValueError: <redacted>" in payloads


def test_langsmith_full_capture_keeps_the_message(
    tracing_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRACE_CAPTURE", "full")
    telemetry.setup_telemetry()
    import langsmith

    client = langsmith.run_trees.get_cached_client()
    assert client._hide_inputs is False and client._anonymizer is None
    error = {"error": f"ValueError('{SECRET}')"}
    assert client._hide_run_error(error) == error


def _encode(spans: list) -> dict:
    from google.protobuf.json_format import MessageToDict
    from opentelemetry.exporter.otlp.proto.common._internal.trace_encoder import encode_spans

    return MessageToDict(encode_spans(spans))


def test_otlp_exporter_strips_error_text_from_events_and_status() -> None:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.trace import Status, StatusCode

    class _Sink:
        def __init__(self) -> None:
            self.spans: list = []

        def export(self, spans: Any) -> Any:
            self.spans.extend(spans)

        def shutdown(self) -> None:
            pass

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return True

    sink = _Sink()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(telemetry._MetadataOnlyExporter(sink)))
    tracer = provider.get_tracer("test")
    message = f"ValueError('{SECRET}')\nTraceback (most recent call last): ..."
    with tracer.start_as_current_span("with-events") as span:
        span.add_event(
            "exception",
            {
                "exception.type": "ValueError",
                "exception.message": SECRET,
                "exception.stacktrace": "tb",
            },
        )
        span.set_status(Status(StatusCode.ERROR, message))
    with tracer.start_as_current_span("status-only") as span:
        span.set_status(Status(StatusCode.ERROR, message))
    provider.force_flush()

    encoded = json.dumps(_encode(sink.spans))
    assert SECRET not in encoded and "Traceback" not in encoded
    spans = _encode(sink.spans)["resourceSpans"][0]["scopeSpans"][0]["spans"]
    by_name = {s["name"]: s for s in spans}
    for name in ("with-events", "status-only"):
        assert by_name[name]["status"]["code"] == "STATUS_CODE_ERROR"
        assert "message" not in by_name[name]["status"]
    event_attrs = {a["key"] for a in by_name["with-events"]["events"][0]["attributes"]}
    assert event_attrs == {"exception.type"}


# --- correlation across services ---------------------------------------------------------

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
TRACEPARENT = f"00-{TRACE_ID}-00f067aa0ba902b7-01"


def test_outbound_headers_carry_the_request_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(telemetry, "_otlp_active", False)
    monkeypatch.delenv("PROPAGATE_TRACE_HEADERS", raising=False)
    token = telemetry.LOG_CONTEXT["request_id"].set("req-9")
    try:
        # Without OTLP tracing only the request id is passed on.
        assert telemetry.outbound_trace_headers() == {"X-Request-ID": "req-9"}
        monkeypatch.setenv("PROPAGATE_TRACE_HEADERS", "false")
        assert telemetry.outbound_trace_headers() == {}
    finally:
        telemetry.LOG_CONTEXT["request_id"].reset(token)


def test_an_incoming_trace_is_continued_and_passed_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from opentelemetry.sdk.trace import TracerProvider

    monkeypatch.setattr(telemetry, "_otlp_active", True)
    monkeypatch.delenv("PROPAGATE_TRACE_HEADERS", raising=False)
    tracer = TracerProvider().get_tracer("test")
    incoming = [(b"traceparent", TRACEPARENT.encode()), (b"baggage", b"user=alice")]
    token = telemetry.attach_trace_context(incoming, "/a2a/app")
    assert token is not None
    try:
        with tracer.start_as_current_span("run") as span:
            outbound = telemetry.outbound_trace_headers()
    finally:
        telemetry.detach_trace_context(token)
    context = span.get_span_context()
    # The run's span joined the caller's trace, and the next hop continues it from the run.
    assert format(context.trace_id, "032x") == TRACE_ID
    _, trace_id, parent_id, _ = outbound["traceparent"].split("-")
    assert (trace_id, parent_id) == (TRACE_ID, format(context.span_id, "016x"))
    assert "baggage" not in outbound  # W3C trace context only
    # Nothing is attached without a traceparent, without OTLP tracing, or when switched off.
    assert telemetry.attach_trace_context([(b"x-request-id", b"r")], "/a2a/app") is None
    monkeypatch.setattr(telemetry, "_otlp_active", False)
    assert telemetry.attach_trace_context(incoming, "/a2a/app") is None
    monkeypatch.setattr(telemetry, "_otlp_active", True)
    for off in ("false", "off", "0"):
        monkeypatch.setenv("PROPAGATE_TRACE_HEADERS", off)
        assert telemetry.attach_trace_context(incoming, "/a2a/app") is None
    # Under peers (the default, and what true reads as), only on the A2A routes.
    for peers in ("", "peers", "true", "TRUE"):
        monkeypatch.setenv("PROPAGATE_TRACE_HEADERS", peers)
        for path in ("/chat", "/threads/t1/runs", "/a2a", "/A2A/app", None):
            assert telemetry.attach_trace_context(incoming, path) is None
    # Under all, on every path (a tracing gateway in front of the agent).
    monkeypatch.setenv("PROPAGATE_TRACE_HEADERS", "all")
    token = telemetry.attach_trace_context(incoming, "/chat")
    assert token is not None
    telemetry.detach_trace_context(token)


async def test_the_request_middleware_continues_the_callers_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx
    from opentelemetry import trace
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from {{cookiecutter.agent_directory}}.app_utils.middleware import RequestContextMiddleware

    monkeypatch.setattr(telemetry, "_otlp_active", True)
    monkeypatch.delenv("PROPAGATE_TRACE_HEADERS", raising=False)

    async def endpoint(request: Request) -> JSONResponse:
        context = trace.get_current_span().get_span_context()
        return JSONResponse(
            {
                "trace_id": format(context.trace_id, "032x"),
                "out": telemetry.outbound_trace_headers(),
            }
        )

    routes = [Route("/a2a/app", endpoint, methods=["POST"]), Route("/chat", endpoint)]
    app = RequestContextMiddleware(Starlette(routes=routes))
    caller = {"traceparent": TRACEPARENT, "X-Request-ID": "req-7"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        body = (await c.post("/a2a/app", headers=caller)).json()
        other = (await c.post("/a2a/app")).json()
        public = (await c.get("/chat", headers=caller)).json()
        monkeypatch.setenv("PROPAGATE_TRACE_HEADERS", "all")
        gateway = (await c.get("/chat", headers=caller)).json()
    assert body["trace_id"] == TRACE_ID
    assert body["out"]["X-Request-ID"] == "req-7"
    assert body["out"]["traceparent"].split("-")[1] == TRACE_ID
    # The context is detached after the request: the next one starts on its own.
    assert other["trace_id"] == "0" * 32 and "traceparent" not in other["out"]
    # A caller of a public route cannot choose this agent's trace ids (the request id is
    # still taken and echoed); behind a tracing gateway, `all` continues it there too.
    assert public["trace_id"] == "0" * 32 and public["out"]["X-Request-ID"] == "req-7"
    assert gateway["trace_id"] == TRACE_ID


@pytest.mark.parametrize(
    ("value", "scope"),
    [
        (None, "peers"),
        ("", "peers"),
        ("peers", "peers"),
        ("true", "peers"),
        ("1", "peers"),
        ("Yes", "peers"),
        ("on", "peers"),
        ("all", "all"),
        ("ALL", "all"),
        ("off", "off"),
        ("false", "off"),
        ("0", "off"),
        ("no", "off"),
    ],
)
def test_the_trace_scope(monkeypatch: pytest.MonkeyPatch, value: str | None, scope: str) -> None:
    if value is None:
        monkeypatch.delenv("PROPAGATE_TRACE_HEADERS", raising=False)
    else:
        monkeypatch.setenv("PROPAGATE_TRACE_HEADERS", value)
    assert telemetry.trace_scope() == scope
    assert telemetry.propagate_trace_headers() is (scope != "off")
    assert telemetry.propagate_to_every_api() is (scope == "all")


def test_true_is_read_as_peers_and_said_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(telemetry, "_said_true_is_peers", False)
    monkeypatch.setenv("PROPAGATE_TRACE_HEADERS", "true")
    with caplog.at_level(logging.INFO, logger=telemetry.__name__):
        assert telemetry.trace_scope() == telemetry.trace_scope() == "peers"
    assert caplog.text.count("PROPAGATE_TRACE_HEADERS=true reads as peers") == 1


async def test_a_bad_trace_scope_stops_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    from {{cookiecutter.agent_directory}} import fast_api_app

    monkeypatch.setenv("PROPAGATE_TRACE_HEADERS", "everywhere")
    with pytest.raises(SettingsError, match="PROPAGATE_TRACE_HEADERS must be peers, all or off"):
        async with fast_api_app.lifespan(fast_api_app.app):
            pass
    # At runtime a value startup refuses propagates nothing.
    assert not telemetry.propagate_trace_headers() and not telemetry.propagate_to_every_api()


@pytest.mark.parametrize(
    ("value", "receive"),
    [
        ("peers", {"peer", "forward", "exchange"}),
        ("all", {"peer", "forward", "exchange", "bearer", "none", "jsonrpc"}),
        ("off", set()),
    ],
)
def test_which_apis_get_the_headers(
    monkeypatch: pytest.MonkeyPatch, value: str, receive: set[str]
) -> None:
    from {{cookiecutter.agent_directory}}.app_utils import api_client

    apis = {
        "peer": {"protocol": "a2a", "auth": "bearer"},
        "forward": {"auth": "forward"},
        "exchange": {"auth": "exchange"},
        "bearer": {"auth": "bearer"},
        "none": {"auth": "none"},
        "jsonrpc": {"protocol": "jsonrpc", "auth": "bearer"},
    }
    monkeypatch.setattr(telemetry, "_otlp_active", False)
    monkeypatch.setenv("PROPAGATE_TRACE_HEADERS", value)
    api_client.set_outbound_headers(
        telemetry.outbound_trace_headers, everywhere=telemetry.propagate_to_every_api
    )
    token = telemetry.LOG_CONTEXT["request_id"].set("req-3")
    try:
        got = {name for name, api in apis.items() if api_client.outbound_headers(api)}
    finally:
        telemetry.LOG_CONTEXT["request_id"].reset(token)
        api_client.set_outbound_headers(None)
    assert got == receive


async def test_a_tool_passes_its_own_span_on_without_a_current_otel_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `/chat` run with no incoming trace: OpenInference does not make its spans current,
    so the headers must come from the tool's own span, not from the (empty) OTel context."""
    from langchain_core.tools import tool
    from openinference.instrumentation.langchain import LangChainInstrumentor
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    monkeypatch.setattr(telemetry, "_otlp_active", True)
    monkeypatch.delenv("PROPAGATE_TRACE_HEADERS", raising=False)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    @tool
    async def ask_peer(question: str) -> str:
        """Returns the correlation headers an outbound call would carry."""
        assert not trace.get_current_span().get_span_context().is_valid
        return json.dumps(telemetry.outbound_trace_headers())

    instrumentor = LangChainInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        headers = json.loads(await ask_peer.ainvoke({"question": "q"}))
    finally:
        instrumentor.uninstrument()
    (span,) = [s for s in exporter.get_finished_spans() if s.name == "ask_peer"]
    _, trace_id, parent_id, _ = headers["traceparent"].split("-")
    assert trace_id == format(span.context.trace_id, "032x")
    assert parent_id == format(span.context.span_id, "016x")
