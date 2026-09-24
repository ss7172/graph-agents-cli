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

"""The ``run`` command end to end against the fake contract server."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from graph_agents_cli._chat_client import SseEvent
from graph_agents_cli.run import _local_server, cmd_run
from graph_agents_cli.run.cmd_run import (
    AgentError,
    RunOutcome,
    compose_message,
    render_chat_events,
)

from .conftest import contract_sequence


@pytest.fixture
def local_project(monkeypatch, tmp_path: Path, chat_server):
    """A fake project whose local server is the fake chat server."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("API_KEY=local-key\nCHECKPOINTER=memory\n")
    cfg = SimpleNamespace(agent_directory="app", runtime="fastapi", checkpointer="postgres")
    monkeypatch.setattr(cmd_run, "chdir_project_root", lambda *a, **k: None)
    monkeypatch.setattr(cmd_run, "read_project_config", lambda *a, **k: cfg)
    monkeypatch.setattr(cmd_run, "require_agent_directory", lambda cfg: None)

    port = int(chat_server.url.rsplit(":", 1)[1])
    state = SimpleNamespace(ensure_calls=[], stop_calls=[], started=True, cfg=cfg)

    def fake_ensure(root, agent_dir, **kwargs):
        state.ensure_calls.append({"root": root, "agent_dir": agent_dir, **kwargs})
        return _local_server.ServerInfo(
            port=port, started=state.started, pid=4242, runtime="fastapi", checkpointer="memory"
        )

    def fake_stop(root, pid=None):
        state.stop_calls.append({"root": root, "pid": pid})
        return True

    monkeypatch.setattr(cmd_run, "ensure_server", fake_ensure)
    monkeypatch.setattr(cmd_run, "stop_server", fake_stop)
    monkeypatch.delenv("GRAPH_AGENTS_CLI_API_KEY", raising=False)
    return state


def invoke(*args: str):
    return CliRunner().invoke(cmd_run.cmd_run, list(args), catch_exceptions=False)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def test_render_chat_events_prints_text_tools_and_collects_end(capsys):
    events = [SseEvent(name, data, json.dumps(data)) for name, data in contract_sequence()]
    outcome = render_chat_events(events)
    out = capsys.readouterr().out
    assert out.startswith("[agent]: Hello\n")
    assert '[tool_call: get_weather({"query": "SF"})]' in out
    assert "[tool_result: get_weather -> sunny]" in out
    assert out.rstrip().endswith(", world")
    assert outcome == RunOutcome(
        thread_id="thread-1",
        run_id="run-1",
        rendered=True,
        usage={"input_tokens": 10, "output_tokens": 5},
        latency_ms=42,
        status="ok",
    )


def test_render_chat_events_raises_agent_error(capsys):
    events = [
        SseEvent("message.delta", {"text": "partial"}, ""),
        SseEvent("error", {"code": "upstream", "message": "model down"}, ""),
    ]
    with pytest.raises(AgentError) as excinfo:
        render_chat_events(events)
    assert excinfo.value.code == "upstream"
    assert capsys.readouterr().out == "[agent]: partial\n"


def test_render_chat_events_verbose_prints_one_line_per_event(capsys):
    events = [SseEvent(name, data, "") for name, data in contract_sequence()]
    render_chat_events(events, verbose=True)
    out = capsys.readouterr().out
    assert 'event: message.start {"thread_id": "thread-1", "run_id": "run-1"}\n' in out
    assert (
        'event: tool.call {"id": "call-1", "name": "get_weather", "args": {"query": "SF"}}' in out
    )
    # Deltas are on screen as text; -v counts them instead of repeating each one.
    assert "event: message.delta x1 (5 characters)" in out
    assert "event: message.delta x1 (7 characters)" in out
    assert '"text"' not in out
    # One line per event (the answer text lines aside): no pretty-printed JSON.
    assert len(out.splitlines()) == 10, out
    assert out.rstrip().splitlines()[-1].startswith('event: message.end {"thread_id"')


def test_verbose_counts_a_run_of_deltas_on_one_line(capsys):
    events = [SseEvent("message.start", {"thread_id": "t"}, "")]
    events += [SseEvent("message.delta", {"text": w}, "") for w in ("one ", "two ", "three")]
    events.append(SseEvent("message.end", {"thread_id": "t"}, ""))
    render_chat_events(events, verbose=True)
    out = capsys.readouterr().out
    assert "[agent]: one two three\n" in out
    assert "event: message.delta x3 (13 characters)" in out
    assert out.count("message.delta") == 1


def test_compose_message_attaches_text_files(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("line one\nline two\n", encoding="utf-8")
    composed = compose_message("summarise", [str(f)])
    assert composed.startswith(
        "summarise\n\n--- Attached file: notes.txt ---\nline one\nline two\n"
    )
    assert composed.endswith("--- End of notes.txt ---")


def test_compose_message_rejects_binary(tmp_path):
    f = tmp_path / "blob.bin"
    f.write_bytes(b"\xff\xfe\x00binary")
    import click

    with pytest.raises(click.ClickException, match="not a UTF-8 text file"):
        compose_message("x", [str(f)])


# ---------------------------------------------------------------------------
# local runs
# ---------------------------------------------------------------------------


def test_local_run_streams_and_tears_down_one_off_server(local_project, chat_server):
    result = invoke("hello there")
    assert result.exit_code == 0, result.output
    out = result.output
    assert "[user]: hello there" in out
    assert "[agent]: Hello" in out
    assert "[tool_call: get_weather" in out
    assert "Thread: thread-1" in out
    # One-off server with memory checkpointer: no resume command, a hint instead.
    assert "--start-server" in out
    assert "Resume with" not in out
    assert "tokens in/out 10/5" in out and "42 ms" in out

    (call,) = local_project.ensure_calls
    assert call["agent_dir"] == "app"
    assert call["runtime"] == "fastapi"
    assert call["checkpointer"] == "postgres"
    assert call["keep_running"] is False
    assert local_project.stop_calls == [{"root": Path.cwd(), "pid": 4242}]

    (req,) = chat_server.chat_requests
    assert req["body"]["message"] == "hello there"
    assert "thread_id" not in req["body"]
    # API_KEY from the project's .env is used for a local shared-bearer server.
    assert req["headers"]["authorization"] == "Bearer local-key"


def test_local_run_with_start_server_keeps_server_and_prints_resume(local_project):
    result = invoke("hi", "--start-server")
    assert result.exit_code == 0, result.output
    assert local_project.stop_calls == []
    assert local_project.ensure_calls[0]["keep_running"] is True
    assert 'Resume with: graph-agents-cli run "<message>" --thread-id thread-1' in result.output


def test_local_run_reused_server_is_left_running(local_project):
    local_project.started = False
    result = invoke("hi")
    assert result.exit_code == 0, result.output
    assert local_project.stop_calls == []
    assert "Resume with" in result.output


def test_thread_id_is_forwarded_for_continuity(local_project, chat_server):
    chat_server.script = lambda body: contract_sequence(thread_id=body.get("thread_id", "new"))
    result = invoke("again", "--thread-id", "thread-77", "--start-server")
    assert result.exit_code == 0, result.output
    assert chat_server.chat_requests[0]["body"]["thread_id"] == "thread-77"
    assert "Thread: thread-77" in result.output
    assert "--thread-id thread-77" in result.output


def test_error_event_exits_1_after_partial_output(local_project, chat_server):
    chat_server.script = [
        ("message.start", {"thread_id": "t", "run_id": "r"}),
        ("message.delta", {"text": "partial"}),
        ("error", {"code": "upstream_error", "message": "model unavailable"}),
    ]
    result = invoke("hi")
    assert result.exit_code == 1
    assert "[agent]: partial" in result.output
    assert "[error: upstream_error]: model unavailable" in result.output
    # The one-off server is still torn down.
    assert local_project.stop_calls


def test_an_error_event_still_prints_the_thread_and_how_to_resume(
    chat_server, monkeypatch, tmp_path
):
    """The thread survives a failed turn: the footer names it, as after a success."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GRAPH_AGENTS_CLI_API_KEY", raising=False)
    chat_server.script = [
        ("message.start", {"thread_id": "srv-thread-5", "run_id": "run-5"}),
        ("message.delta", {"text": "partial"}),
        (
            "error",
            {
                "code": "recursion_limit",
                "message": "The run reached the step limit. Reference: e1.",
                "error_id": "e1",
                "run_id": "run-5",
            },
        ),
    ]
    result = invoke("hi", "--url", chat_server.url)
    assert result.exit_code == 1, result.output
    out = result.output
    assert "[error: recursion_limit]: The run reached the step limit. Reference: e1." in out
    assert "Run: run-5" in out and "Thread: srv-thread-5" in out
    assert f'Resume with: graph-agents-cli run "<message>" --url {chat_server.url}' in out
    assert "--thread-id srv-thread-5" in out
    assert "(no response content)" not in out
    assert out.index("[error: recursion_limit]") < out.index("Thread: srv-thread-5")


def test_an_error_event_on_a_one_off_local_server_says_the_thread_is_gone(
    local_project, chat_server
):
    chat_server.script = [
        ("message.start", {"thread_id": "t-local", "run_id": "r"}),
        ("error", {"code": "timeout", "message": "The run timed out."}),
    ]
    result = invoke("hi")
    assert result.exit_code == 1
    assert "Thread: t-local" in result.output
    assert "One-off server with an in-memory checkpointer" in result.output
    assert "(no response content)" not in result.output


def test_a_stream_that_drops_mid_run_is_not_reported_as_unreachable(
    chat_server, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GRAPH_AGENTS_CLI_API_KEY", raising=False)
    chat_server.script = contract_sequence(thread_id="srv-thread-9", deltas=("Hello",) * 8)
    chat_server.drop_after_bytes = 128
    result = invoke("hi", "--url", chat_server.url)
    assert result.exit_code == 2, result.output  # a transport failure, as before
    out = result.output
    assert "Could not reach" not in out
    assert f"The connection to the remote agent at {chat_server.url} dropped after the run" in out
    # No reply text had arrived: nothing above to call incomplete.
    assert "The run was interrupted" in out and "The answer above" not in out
    assert "the thread keeps every turn that finished" in out
    assert "Thread: srv-thread-9" in out
    assert f"--url {chat_server.url} --thread-id srv-thread-9" in out


def test_a_connection_closed_before_any_answer_says_so(chat_server, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GRAPH_AGENTS_CLI_API_KEY", raising=False)
    chat_server.drop_after_bytes = 0
    result = invoke("hi", "--url", chat_server.url, "--thread-id", "t-1")
    assert result.exit_code == 2, result.output
    assert "failed before the run started" in result.output
    assert "Could not reach" not in result.output and "Resume with" not in result.output


def test_a_local_server_that_dies_mid_run_is_stopped_and_the_thread_named(
    local_project, chat_server
):
    local_project.started = False  # even a reused server is stopped: it is gone or wedged
    chat_server.script = contract_sequence(thread_id="t-mem", deltas=("Hello",) * 8)
    chat_server.drop_after_bytes = 128
    result = invoke("hi")
    assert result.exit_code == 2, result.output
    out = result.output
    assert "The connection to the local server dropped after the run started" in out
    assert "The local server has been stopped" in out
    assert "Thread: t-mem" in out
    # The fake ensure_server reports a memory checkpointer: nothing survives the stop,
    # so no resume line, no "keeps every turn" and no advice to add --start-server.
    assert "its in-memory checkpointer lost this thread" in out
    assert "Resume with" not in out and "keeps every turn" not in out
    assert "add --start-server" not in out
    assert local_project.stop_calls == [{"root": Path.cwd(), "pid": 4242}]


def test_http_401_surfaces_body_and_auth_hint(local_project, chat_server):
    chat_server.status = 401
    chat_server.error_body = '{"detail": "invalid token"}'
    result = invoke("hi")
    assert result.exit_code == 1
    assert "HTTP 401" in result.output
    assert "invalid token" in result.output
    assert "GRAPH_AGENTS_CLI_API_KEY" in result.output


DEV_TOKEN_EXPORT = (
    'export GRAPH_AGENTS_CLI_API_KEY="$(graph-agents-cli auth dev-token --sub <user>)"'
)


@pytest.mark.parametrize(
    ("policy", "expected", "absent"),
    [
        ("jwt", DEV_TOKEN_EXPORT, "--header 'Authorization"),
        ("shared-bearer", "export GRAPH_AGENTS_CLI_API_KEY=<API_KEY>", "dev-token"),
        ("custom", "--cookie name=value", "dev-token"),
    ],
)
def test_the_401_hint_follows_the_projects_policy_and_keeps_tokens_out_of_argv(
    local_project, chat_server, policy, expected, absent
):
    with open(".env", "a", encoding="utf-8") as env:
        env.write(f"AUTH_POLICY={policy}\n")
    (Path.cwd() / "graph-agents-cli-manifest.yaml").write_text("name: x\n")
    chat_server.status = 401
    chat_server.error_body = '{"detail": "Missing bearer token."}'
    result = invoke("hi")
    assert result.exit_code == 1
    assert expected in result.output
    assert absent not in result.output


def test_the_401_hint_outside_a_project_leads_with_the_variable(chat_server, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GRAPH_AGENTS_CLI_API_KEY", raising=False)
    chat_server.status = 401
    result = invoke("hi", "--url", chat_server.url)
    assert result.exit_code == 1
    assert "export GRAPH_AGENTS_CLI_API_KEY=<credential>" in result.output
    assert "out of argv and shell history" in result.output
    assert "--header 'Authorization" not in result.output


def test_a_403_is_not_reported_as_failed_authentication(local_project, chat_server):
    chat_server.status = 403
    chat_server.error_body = '{"detail": "This thread belongs to another principal."}'
    result = invoke("hi", "--thread-id", "x")
    assert result.exit_code == 1
    assert "Authentication failed" not in result.output
    assert "refused the request" in result.output


@pytest.mark.parametrize(
    ("body", "remote", "expected"),
    [
        ("AUTH_POLICY=jwt is not configured on the server", False, "auth dev-token --sub"),
        ("AUTH_POLICY=jwt is not configured on the server", True, "AUTH_JWT_ISSUER"),
        ("API_KEY is not configured on the server", False, "login --write-env"),
        ("API_KEY is not configured on the server", True, "secrets apply --env"),
        ("AUTH_POLICY=custom is not implemented", False, "policies/custom.py"),
        ("database unavailable", False, "see its log"),
    ],
)
def test_the_503_hint_names_what_the_server_misses(
    local_project, chat_server, body, remote, expected
):
    chat_server.status = 503
    chat_server.error_body = json.dumps({"detail": body})
    result = invoke("hi", "--url", chat_server.url) if remote else invoke("hi")
    assert result.exit_code == 1
    assert expected in result.output
    assert "is the auth policy implemented" not in result.output


def test_run_help_recommends_the_variable_for_bearer_credentials():
    result = CliRunner().invoke(cmd_run.cmd_run, ["--help"], terminal_width=120)
    assert result.exit_code == 0
    text = " ".join(result.output.split())
    assert "jwt GRAPH_AGENTS_CLI_API_KEY=<token> (locally: auth dev-token)" in text
    assert "shared-bearer GRAPH_AGENTS_CLI_API_KEY=<API_KEY>" in text
    assert "Bearer <token>'" not in text


def test_http_404_with_thread_id_explains_lost_thread(local_project, chat_server):
    chat_server.status = 404
    chat_server.error_body = '{"detail": "thread not found"}'
    result = invoke("hi", "--thread-id", "gone")
    assert result.exit_code == 1
    assert "Thread gone was not found" in result.output
    assert "--start-server" in result.output


def test_file_attachments_become_context(local_project, chat_server, tmp_path):
    f = tmp_path / "ctx.md"
    f.write_text("# context\nfacts\n")
    result = invoke("use this", "-f", str(f))
    assert result.exit_code == 0, result.output
    body = chat_server.chat_requests[0]["body"]["message"]
    assert body.startswith("use this\n\n--- Attached file: ctx.md ---\n# context\nfacts")
    # The display line shows only the prompt, not the attachment.
    assert "[user]: use this\n" in result.output


def test_explicit_header_overrides_env_api_key(local_project, chat_server, monkeypatch):
    monkeypatch.setenv("GRAPH_AGENTS_CLI_API_KEY", "env-key")
    result = invoke(
        "hi", "-H", "Authorization: Bearer explicit", "--cookie", "s=1", "--session-token", "tok"
    )
    assert result.exit_code == 0, result.output
    headers = chat_server.chat_requests[0]["headers"]
    assert headers["authorization"] == "Bearer explicit"
    assert headers["cookie"] == "s=1"
    assert headers["x-session-token"] == "tok"
    # The hidden alias still works but says it is deprecated, once, pointing at --header.
    assert result.output.count("--session-token is deprecated") == 1
    assert "--header 'X-Session-Token: <token>'" in result.output


def test_session_token_is_not_advertised_in_help():
    result = invoke("--help")
    assert result.exit_code == 0, result.output
    assert "session" not in result.output.lower()


def test_verbose_prints_event_payloads(local_project):
    result = invoke("hi", "-v")
    assert result.exit_code == 0, result.output
    assert 'event: tool.call {"id": "call-1", "name": "get_weather"' in result.output


def test_local_a2a_mode_without_sdk_gives_install_hint(local_project, monkeypatch):
    monkeypatch.setitem(sys.modules, "a2a", None)
    result = invoke("hi", "--mode", "a2a")
    assert result.exit_code == 1
    assert "graph-agents-cli[a2a]" in result.output


# ---------------------------------------------------------------------------
# remote runs
# ---------------------------------------------------------------------------


def test_remote_run_uses_env_api_key_and_prints_resume_flags(chat_server, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GRAPH_AGENTS_CLI_API_KEY", "remote-key")
    result = invoke("hi", "--url", chat_server.url + "/", "-H", "X-Tenant: acme")
    assert result.exit_code == 0, result.output
    assert f"Querying remote agent: {chat_server.url}/ (mode: chat)" in result.output
    headers = chat_server.chat_requests[0]["headers"]
    assert headers["authorization"] == "Bearer remote-key"
    assert headers["x-tenant"] == "acme"
    assert (
        f"--url {chat_server.url}/ --header 'X-Tenant: acme' --thread-id thread-1" in result.output
    )


def test_resume_line_redacts_credentials(chat_server, monkeypatch, tmp_path):
    """The footer lands in CI logs and transcripts: no credential value is echoed."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GRAPH_AGENTS_CLI_API_KEY", raising=False)
    result = invoke(
        "hi",
        "--url",
        chat_server.url,
        "-H",
        "Authorization: Bearer sk-SECRET-123",
        "-H",
        "X-Api-Key: k-SECRET",
        "-H",
        "X-Tenant: acme",
        "--cookie",
        "session=COOKIE-SECRET",
        "--session-token",
        "TOKEN-SECRET",
    )
    assert result.exit_code == 0, result.output
    out = result.output
    for secret in ("sk-SECRET-123", "k-SECRET", "COOKIE-SECRET", "TOKEN-SECRET"):
        assert secret not in out, secret
    assert "--header 'Authorization: <redacted>'" in out
    assert "--header 'X-Api-Key: <redacted>'" in out
    assert "--header 'X-Tenant: acme'" in out  # routing headers stay readable
    assert "--cookie 'session=<redacted>'" in out
    # The deprecated alias is resumed with the flag that replaces it.
    assert "--header 'X-Session-Token: <redacted>'" in out
    assert "--session-token <redacted>" not in out
    assert "--thread-id thread-1" in out and "re-supply the redacted credential values" in out
    # The request itself carried the real values.
    headers = chat_server.chat_requests[0]["headers"]
    assert headers["authorization"] == "Bearer sk-SECRET-123"
    assert headers["cookie"] == "session=COOKIE-SECRET"
    assert headers["x-session-token"] == "TOKEN-SECRET"


def test_build_resume_flags_redaction_rules():
    from graph_agents_cli.run.cmd_run import _build_resume_flags

    flags = _build_resume_flags(
        "https://agent",
        "chat",
        ("Proxy-Authorization: Basic x", "Cookie: a=b", "X-Session-Token: t", "X-Request-Id: 7"),
        ("sid=1",),
        "tok",
    )
    assert flags == (
        " --url https://agent --header 'Proxy-Authorization: <redacted>' --header 'Cookie: <redacted>'"
        " --header 'X-Session-Token: <redacted>' --header 'X-Request-Id: 7'"
        " --cookie 'sid=<redacted>' --session-token <redacted>"
    )
    assert _build_resume_flags(None, "chat", (), (), None) == ""


def test_stalled_stream_is_reported_and_leaves_a_reused_server_running(
    local_project, chat_server, monkeypatch
):
    """A long silent turn is a ReadTimeout, not 'could not reach': no teardown of a reused server."""
    import httpx

    from graph_agents_cli import _chat_client

    monkeypatch.setattr(
        _chat_client,
        "STREAM_TIMEOUT",
        httpx.Timeout(connect=10.0, read=0.5, write=10.0, pool=10.0),
    )
    chat_server.stall_seconds = 2.0
    local_project.started = False  # a persistent --start-server instance is reused
    result = invoke("hi", "--thread-id", "t-9")
    # The agent went silent: a tool failure (2), not a refusal (1).
    assert result.exit_code == 2, result.output
    assert (
        "No event from the agent for 0 s" in result.output
        or "No event from the agent" in result.output
    )
    assert "Could not reach" not in result.output
    assert "It has been stopped" not in result.output
    assert "--thread-id t-9" in result.output
    assert local_project.stop_calls == []


def test_stalled_stream_still_stops_a_one_off_server(local_project, chat_server, monkeypatch):
    import httpx

    from graph_agents_cli import _chat_client

    monkeypatch.setattr(
        _chat_client, "STREAM_TIMEOUT", httpx.Timeout(connect=10.0, read=0.5, write=10.0, pool=10.0)
    )
    chat_server.stall_seconds = 2.0
    result = invoke("hi", "--thread-id", "t-10")
    assert result.exit_code == 2
    assert "No event from the agent" in result.output
    assert local_project.stop_calls == [{"root": Path.cwd(), "pid": 4242}]
    # Its memory checkpointer went with it: no "check the thread later".
    assert "Check the thread later" not in result.output
    assert "Its in-memory thread went with it" in result.output


def test_remote_run_warns_about_start_server(chat_server, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GRAPH_AGENTS_CLI_API_KEY", raising=False)
    result = invoke("hi", "--url", chat_server.url, "--start-server")
    assert result.exit_code == 0, result.output
    assert "--start-server has no effect" in result.output


def test_remote_unreachable_is_a_clean_error(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GRAPH_AGENTS_CLI_API_KEY", raising=False)
    # Port 9 (discard) on loopback is closed on every developer machine.
    result = invoke("hi", "--url", "http://127.0.0.1:9")
    assert result.exit_code == 2  # unreachable: a tool failure, not a refusal
    assert "Could not reach remote agent" in result.output


def test_remote_a2a_without_sdk_fails_before_any_request(chat_server, monkeypatch, tmp_path):
    # The missing extra is detected before the card probe or any server start,
    # and the hint is a single line.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GRAPH_AGENTS_CLI_API_KEY", raising=False)
    monkeypatch.setitem(sys.modules, "a2a", None)
    monkeypatch.setattr(cmd_run, "find_project_root", lambda *a, **k: tmp_path)
    monkeypatch.setattr(
        cmd_run, "read_project_config", lambda *a, **k: SimpleNamespace(agent_directory="app")
    )
    chat_server.card = {"name": "agent"}
    result = invoke("hi", "--url", chat_server.url, "--mode", "a2a")
    assert result.exit_code == 1
    assert "graph-agents-cli[a2a]" in result.output
    hint_lines = [line for line in result.output.splitlines() if "a2a" in line]
    assert len(hint_lines) == 1
    assert not chat_server.requests


# ---------------------------------------------------------------------------
# --stop-server
# ---------------------------------------------------------------------------


def test_stop_server_flag(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cmd_run, "chdir_project_root", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(cmd_run, "stop_server", lambda root, pid=None: calls.append(root) or True)
    result = invoke("--stop-server")
    assert result.exit_code == 0, result.output
    assert calls == [tmp_path]

    monkeypatch.setattr(cmd_run, "stop_server", lambda root, pid=None: False)
    result = invoke("--stop-server")
    assert result.exit_code == 1
    assert "No local server is running" in result.output
