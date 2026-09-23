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

"""`eval generate` against a fake SSE chat client and a fake local server."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from conftest import DATASET, ok_stream, read_traces, sse

from graph_agents_cli.eval import _paths
from graph_agents_cli.eval._client import run_case
from graph_agents_cli.eval.cmd_generate import cmd_generate
from graph_agents_cli.eval.dataset import parse_case

GOOD_STREAMS = {
    "hi": ok_stream("hello there"),
    "weather in SF?": ok_stream(
        "It is sunny in SF.",
        tool_calls=[
            {"id": "c1", "name": "get_weather", "args": {"query": "SF"}, "result": "sunny"}
        ],
    ),
    "say nothing special": ok_stream("ok"),
}


def test_generate_with_url_writes_traces_per_contract(
    project: Path, runner: CliRunner, fake_chat, dataset_digest: str
) -> None:
    chat = fake_chat(GOOD_STREAMS)
    result = runner.invoke(
        cmd_generate,
        ["--url", "http://agent.example/", "--header", "Authorization: Bearer abc"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    doc = read_traces(project)
    assert set(doc) >= {"dataset_hash", "generated_at", "agent_version", "model", "traces"}
    assert doc["dataset_hash"] == dataset_digest
    assert doc["dataset_paths"] == [_paths.DEFAULT_INPUT_DATASET]
    assert doc["agent_version"] == "0.3.1" and doc["model"] == "fake/fake-model"
    assert doc["app_name"] == "app"
    traces = {t["case_id"]: t for t in doc["traces"]}
    assert set(traces) == {"greeting", "weather", "plain"}
    weather = traces["weather"]
    assert weather["status"] == "ok"
    assert weather["response"] == "It is sunny in SF."
    assert weather["tool_calls"] == [
        {
            "id": "c1",
            "name": "get_weather",
            "args": {"query": "SF"},
            "result": "sunny",
            "is_error": False,
        }
    ]
    assert weather["usage"] == {"input_tokens": 5, "output_tokens": 7}
    assert weather["latency_ms"] == 42
    assert weather["thread_id"] == "t-1" and weather["run_id"] == "r-1"
    assert weather["case"] == DATASET["cases"][1]
    assert weather["error"] is None
    # Transport details: base URL without trailing slash, headers, metadata.
    call = next(c for c in chat.calls if c["message"] == "weather in SF?")
    assert call["base_url"] == "http://agent.example"
    assert call["headers"]["Authorization"] == "Bearer abc"
    assert (
        call["metadata"]["case_id"] == "weather"
        and call["metadata"]["dataset_hash"] == dataset_digest
    )
    assert call["thread_id"] is None


def test_generate_records_error_and_missing_and_exits_2(
    project: Path, runner: CliRunner, fake_chat
) -> None:
    streams = dict(GOOD_STREAMS)
    streams["hi"] = [
        sse("message.start", {"thread_id": "t", "run_id": "r"}),
        sse("error", {"code": "boom", "message": "model down"}),
    ]
    streams["say nothing special"] = []
    fake_chat(streams)
    result = runner.invoke(cmd_generate, ["--url", "http://agent.example"])
    assert result.exit_code == 2, result.output
    traces = {t["case_id"]: t for t in read_traces(project)["traces"]}
    assert (
        traces["greeting"]["status"] == "error"
        and traces["greeting"]["error"] == "model down (boom)"
    )
    assert traces["plain"]["status"] == "missing" and traces["plain"]["response"] is None
    assert traces["weather"]["status"] == "ok"
    assert "Generate summary: 1/3 cases produced a response, 2 did not." in result.output
    assert "greeting: error: model down (boom)" in result.output


def test_generate_transport_exception_and_truncated_stream(
    project: Path, runner: CliRunner, fake_chat
) -> None:
    streams = dict(GOOD_STREAMS)
    streams["hi"] = ConnectionError("refused")
    streams["say nothing special"] = [
        sse("message.start", {"thread_id": "t"}),
        sse("message.delta", {"text": "partial"}),
    ]
    fake_chat(streams)
    result = runner.invoke(cmd_generate, ["--url", "http://agent.example"])
    assert result.exit_code == 2
    traces = {t["case_id"]: t for t in read_traces(project)["traces"]}
    assert (
        traces["greeting"]["status"] == "error"
        and "ConnectionError: refused" in traces["greeting"]["error"]
    )
    assert (
        traces["plain"]["status"] == "error"
        and traces["plain"]["error"] == "stream closed before message.end"
    )
    assert traces["plain"]["response"] == "partial"


def test_generate_starts_and_stops_local_server_with_env_api_key(
    project: Path, runner: CliRunner, fake_chat, fake_local_server
) -> None:
    chat = fake_chat(GOOD_STREAMS)
    result = runner.invoke(cmd_generate, [], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert fake_local_server.started == 1 and fake_local_server.stopped == 1
    assert fake_local_server.stopped_pid == 4242
    assert fake_local_server.calls == [
        {
            "project_root": project,
            "agent_dir": "app",
            "runtime": "fastapi",
            "checkpointer": "memory",
        }
    ]
    assert chat.calls[0]["base_url"] == "http://127.0.0.1:18080"
    # The project's .env API_KEY is used as the shared-bearer credential locally.
    assert chat.calls[0]["headers"]["Authorization"] == "Bearer local-secret"
    assert read_traces(project)["base_url"] == "http://127.0.0.1:18080"


def test_generate_stops_server_even_when_dispatch_fails(
    project: Path, runner: CliRunner, fake_local_server, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args, **kwargs):
        raise RuntimeError("dispatch failed")

    monkeypatch.setattr("graph_agents_cli.eval.cmd_generate._dispatch", explode)
    result = runner.invoke(cmd_generate, [])
    assert result.exit_code == 1
    assert fake_local_server.stopped == 1


def test_a_local_server_that_cannot_start_is_a_tool_failure(
    project: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 2, as documented (0/2/3): never 1, which `eval run` reads as a failed gate."""
    from graph_agents_cli.run import _local_server

    def cannot_start(*args, **kwargs):
        raise _local_server.ServerStartError(
            "Local server process exited during startup (exit code 3)."
        )

    monkeypatch.setattr(_local_server, "ensure_server", cannot_start)
    result = runner.invoke(cmd_generate, [])
    assert result.exit_code == 2, result.output
    assert "exited during startup" in result.output


def test_generate_credentials_env_cookie_session_token(
    project: Path, runner: CliRunner, fake_chat, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GRAPH_AGENTS_CLI_API_KEY", "env-key")
    chat = fake_chat(GOOD_STREAMS)
    result = runner.invoke(
        cmd_generate,
        [
            "--url",
            "http://agent.example",
            "--cookie",
            "sid=1",
            "--cookie",
            "t=2",
            "--session-token",
            "tok",
            "--app-name",
            "svc",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    headers = chat.calls[0]["headers"]
    assert headers["Authorization"] == "Bearer env-key"
    assert headers["Cookie"] == "sid=1; t=2"
    assert headers["X-Session-Token"] == "tok"
    assert chat.calls[0]["metadata"]["app_name"] == "svc"


def test_generate_output_and_dataset_flags(
    project: Path, runner: CliRunner, fake_chat, tmp_path: Path
) -> None:
    fake_chat(GOOD_STREAMS)
    out = tmp_path / "custom" / "t.json"
    result = runner.invoke(
        cmd_generate,
        ["--url", "http://x", "--output", str(out), "--dataset", "tests/eval/datasets"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text())["dataset_paths"] == [
        "tests/eval/datasets/basic-dataset.json"
    ]


def test_generate_configuration_errors_exit_3(
    project: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = runner.invoke(cmd_generate, ["--url", "http://x", "--dataset", "nope.json"])
    assert result.exit_code == 3 and "dataset not found" in result.output
    (project / _paths.DEFAULT_INPUT_DATASET).unlink()
    result = runner.invoke(cmd_generate, ["--url", "http://x"])
    assert result.exit_code == 3 and "no dataset found" in result.output
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cmd_generate, ["--url", "http://x"])
    assert result.exit_code == 3 and "not inside a graph-agents-cli project" in result.output


def test_run_case_multi_turn_keeps_thread_and_reports_final_turn(fake_chat) -> None:
    def second_turn(message, kwargs):
        assert kwargs["thread_id"] == "t-1"
        return ok_stream("second answer", thread_id="t-1")

    chat = fake_chat({"first": ok_stream("first answer", thread_id="t-1"), "second": second_turn})
    case = parse_case(
        {
            "id": "multi",
            "messages": [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "ignored by transport"},
                {"role": "user", "content": "second"},
            ],
        }
    )
    trace = run_case("http://x", case, headers={})
    assert [c["message"] for c in chat.calls] == ["first", "second"]
    assert trace["status"] == "ok" and trace["response"] == "second answer"
    assert trace["thread_id"] == "t-1"
    assert len(trace["turns"]) == 2 and trace["turns"][0]["response"] == "first answer"


def test_run_case_stops_at_first_failed_turn(fake_chat) -> None:
    fake_chat({"first": [sse("error", {"message": "nope"})], "second": ok_stream("x")})
    case = parse_case(
        {
            "id": "m",
            "messages": [
                {"role": "user", "content": "first"},
                {"role": "user", "content": "second"},
            ],
        }
    )
    trace = run_case("http://x", case, headers={})
    assert trace["status"] == "error" and trace["error"] == "nope"
    assert "turns" not in trace


def test_run_case_tool_result_without_call_and_end_status_error(fake_chat) -> None:
    fake_chat(
        {
            "q": [
                sse("message.start", {"thread_id": "t", "run_id": "r"}),
                sse("tool.result", {"id": "orphan", "name": "x", "result": "r", "is_error": True}),
                sse("message.delta", {"text": "done"}),
                sse(
                    "message.end",
                    {"thread_id": "t", "run_id": "r", "status": "failed", "latency_ms": 9},
                ),
            ]
        }
    )
    case = parse_case({"id": "m", "messages": [{"role": "user", "content": "q"}]})
    trace = run_case("http://x", case, headers={})
    assert trace["status"] == "error" and "status 'failed'" in trace["error"]
    assert trace["tool_calls"][0]["name"] == "x" and trace["tool_calls"][0]["is_error"] is True
    assert trace["latency_ms"] == 9 and trace["response"] == "done"
