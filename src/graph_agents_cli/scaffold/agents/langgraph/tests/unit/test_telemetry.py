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
from typing import Any

import pytest

from {{cookiecutter.agent_directory}}.app_utils import telemetry

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
