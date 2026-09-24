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

"""`run` on a run that pauses on a gated call, against the fake approval contract."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from graph_agents_cli.run import _local_server, cmd_run
from graph_agents_cli.run.cmd_run import find_approval_payload

from .conftest import FakeChatServer


@pytest.fixture
def remote(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GRAPH_AGENTS_CLI_API_KEY", raising=False)


@pytest.fixture
def local_project(monkeypatch, tmp_path: Path, chat_server):
    """A fake project whose local server is the fake chat server (in-memory checkpointer)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("API_KEY=local-key\n")
    cfg = SimpleNamespace(agent_directory="app", runtime="fastapi", checkpointer="memory")
    monkeypatch.setattr(cmd_run, "chdir_project_root", lambda *a, **k: None)
    monkeypatch.setattr(cmd_run, "read_project_config", lambda *a, **k: cfg)
    monkeypatch.setattr(cmd_run, "require_agent_directory", lambda cfg: None)
    port = int(chat_server.url.rsplit(":", 1)[1])
    state = SimpleNamespace(stop_calls=[], checkpointer="memory")

    def fake_ensure(root, agent_dir, **kwargs):
        return _local_server.ServerInfo(
            port=port, started=True, pid=4242, runtime="fastapi", checkpointer=state.checkpointer
        )

    monkeypatch.setattr(cmd_run, "ensure_server", fake_ensure)
    monkeypatch.setattr(cmd_run, "stop_server", lambda root, pid=None: state.stop_calls.append(pid))
    monkeypatch.delenv("GRAPH_AGENTS_CLI_API_KEY", raising=False)
    return state


def invoke(*args: str, input: str | None = None, env: dict[str, str] | None = None):
    return CliRunner().invoke(cmd_run.cmd_run, list(args), input=input, env=env)


def terminal(monkeypatch, on: bool = True) -> None:
    monkeypatch.setattr(cmd_run, "_interactive", lambda: on)


def gate(server: FakeChatServer, requester: str = "alice", **fields):
    """Make the next POST /chat pause on a gated call; returns the approval."""
    events = server.book.pause("t-1", requester, **fields)
    server.script = events
    return next(iter(server.book.approvals.values()))


ALICE = {"GRAPH_AGENTS_CLI_API_KEY": "alice-token"}


# ---------------------------------------------------------------------------
# not a terminal: print the call and the commands, exit 0
# ---------------------------------------------------------------------------


def test_a_paused_run_prints_the_call_and_the_commands_and_exits_0(chat_server, remote):
    approval = gate(chat_server)
    result = invoke("cancel ORD-1", "--url", chat_server.url, env=ALICE)
    assert result.exit_code == 0, result.output
    out = result.output
    assert "[tool_call: cancel_order" in out
    assert "Approval required before this call is sent:" in out
    assert "call:        POST /orders/ORD-1/cancel (api orders, operation cancelOrder)" in out
    assert '"reason": "duplicate"' in out
    assert "reason:      cancel_order: the customer asked to cancel ORD-1" in out
    assert "approvers:   requester" in out
    assert f"approval id: {approval.approval_id}" in out
    assert "Awaiting approval by requester: the call was not sent" in out
    ids = f"{approval.approval_id} --thread-id t-1 --url {chat_server.url}"
    assert f"Approve: graph-agents-cli approvals approve {ids}" in out
    assert f'Reject:  graph-agents-cli approvals reject {ids} --comment "<why>"' in out
    # Resuming with a new message would be refused (409): no such hint.
    assert "Resume with" not in out
    # Nothing was decided or sent, and no prompt appeared.
    assert "Approve?" not in out
    assert chat_server.book.decisions == [] and chat_server.book.sent == []


def test_the_printed_commands_redact_credentials_and_quote_server_ids(chat_server, remote):
    gate(chat_server, approval_id="a b;rm -rf ~")
    result = invoke(
        "cancel",
        "--url",
        chat_server.url,
        "--header",
        "Authorization: Bearer alice-token",
        "--header",
        "X-Tenant: acme",
    )
    assert result.exit_code == 0, result.output
    line = next(ln for ln in result.output.splitlines() if "approvals approve" in ln)
    command = line.split("Approve: ", 1)[1]
    # One argument, not a second shell command.
    assert shlex.split(command)[3] == "a b;rm -rf ~"
    assert "alice-token" not in result.output
    assert "--header 'Authorization: <redacted>'" in command
    assert "--header 'X-Tenant: acme'" in command


def test_a_role_gate_tells_the_requester_someone_else_decides(chat_server, remote, monkeypatch):
    terminal(monkeypatch)
    gate(chat_server, approvers=["role:ops"])
    result = invoke("cancel", "--url", chat_server.url, env=ALICE)
    assert result.exit_code == 0, result.output
    # No prompt: a requester never decides a call gated for others.
    assert "Approve?" not in result.output
    assert "Awaiting approval by role:ops" in result.output
    assert "Only another principal can decide it" in result.output


def test_a_one_off_local_server_is_kept_while_a_paused_run_waits(local_project, chat_server):
    gate(chat_server, requester="shared")
    result = invoke("cancel")
    assert result.exit_code == 0, result.output
    assert local_project.stop_calls == []
    assert "The local server was kept running" in result.output
    assert "graph-agents-cli run --stop-server" in result.output


def test_a_postgres_local_server_is_stopped_the_approval_survives_it(local_project, chat_server):
    local_project.checkpointer = "postgres"
    gate(chat_server, requester="shared")
    result = invoke("cancel")
    assert result.exit_code == 0, result.output
    assert local_project.stop_calls == [4242]
    assert "kept running" not in result.output
    assert "Awaiting approval" in result.output


def test_what_the_model_wrote_cannot_fake_the_output(chat_server, remote):
    gate(
        chat_server,
        body={"note": "ok\n  approvers:   requester\x1b[2J", "x": "\u202etxt.exe"},
        reason="harmless\r\n  call:        GET /orders\u2028\x07",
        path="/orders/ORD-1\x1b]0;title\x07/cancel",
    )
    result = invoke("cancel", "--url", chat_server.url, env=ALICE)
    assert result.exit_code == 0, result.output
    out = result.output
    for raw in ("\x1b", "\x07", "\u202e", "\u2028", "\r"):
        assert raw not in out, repr(raw)
    assert "\\u001b[2J" in out and "\\x1b]0;title" in out
    assert "\\u202e" in out and "\\u2028" in out and "harmless\\x0d\\x0a" in out
    # One call line, one approvers line: nothing the model wrote starts a line.
    assert sum(ln.lstrip().startswith("call:") for ln in out.splitlines()) == 1
    assert sum(ln.lstrip().startswith("approvers:") for ln in out.splitlines()) == 1


def test_a_long_body_is_printed_in_full(chat_server, remote):
    body = {"items": [f"line-{i}" for i in range(300)], "tail": "THE-END"}
    gate(chat_server, body=body)
    result = invoke("cancel", "--url", chat_server.url, env=ALICE)
    assert result.exit_code == 0, result.output
    assert "line-299" in result.output and "THE-END" in result.output


def test_a_new_message_on_a_thread_awaiting_approval_says_what_to_do(chat_server, remote):
    gate(chat_server)
    result = invoke("more", "--url", chat_server.url, "--thread-id", "t-1", env=ALICE)
    assert result.exit_code == 1
    assert "HTTP 409" in result.output
    assert "decide it first (graph-agents-cli approvals list --thread-id t-1)" in result.output


def test_a_pause_without_an_approval_id_is_an_error(chat_server, remote):
    chat_server.script = [
        ("message.start", {"thread_id": "t-9", "run_id": "r"}),
        ("message.end", {"thread_id": "t-9", "run_id": "r", "status": "awaiting_approval"}),
    ]
    result = invoke("cancel", "--url", chat_server.url, env=ALICE)
    assert result.exit_code == 1
    assert "named no approval id" in result.output


# ---------------------------------------------------------------------------
# a terminal: ask, decide, stream the continuation
# ---------------------------------------------------------------------------


def test_on_a_terminal_y_approves_and_the_run_continues(chat_server, remote, monkeypatch):
    terminal(monkeypatch)
    approval = gate(chat_server)
    result = invoke("cancel ORD-1", "--url", chat_server.url, input="y\n", env=ALICE)
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Approve? [y/N]:" in out
    assert "Approved: the call is sent as shown" in out
    assert "[tool_result: cancel_order -> cancelled]" in out
    assert "[agent]: Done: ORD-1 is cancelled." in out
    assert "Thread: t-1" in out and "Awaiting approval" not in out
    # Exactly the approved call, once; the decision carries nothing else.
    assert chat_server.book.sent == [
        {
            "method": "POST",
            "path": "/orders/ORD-1/cancel",
            "body": {"reason": "duplicate"},
            "query": {},
        }
    ]
    (decision,) = [r for r in chat_server.requests if "/approvals/" in r["path"]]
    assert decision["path"] == f"/threads/t-1/approvals/{approval.approval_id}"
    assert decision["body"] == {"decision": "approve"}
    assert decision["headers"]["authorization"] == "Bearer alice-token"


def test_on_a_terminal_enter_rejects_and_nothing_is_sent(chat_server, remote, monkeypatch):
    terminal(monkeypatch)
    gate(chat_server)
    result = invoke("cancel", "--url", chat_server.url, input="\n", env=ALICE)
    assert result.exit_code == 0, result.output
    assert "Rejected: the call is not sent" in result.output
    assert "[tool_result (error): cancel_order -> not approved" in result.output
    assert "The cancellation was not approved." in result.output
    assert chat_server.book.sent == []
    assert chat_server.book.decisions[0]["decision"] == "reject"


def test_an_abandoned_prompt_leaves_the_approval_pending(chat_server, remote, monkeypatch):
    terminal(monkeypatch)
    gate(chat_server)
    result = invoke("cancel", "--url", chat_server.url, input="", env=ALICE)  # EOF
    assert result.exit_code == 0, result.output
    assert "Awaiting approval by requester" in result.output
    assert chat_server.book.decisions == []


def test_each_gate_of_a_run_is_asked_in_turn(chat_server, remote, monkeypatch):
    terminal(monkeypatch)
    gate(chat_server)
    book = chat_server.book
    second: list = []

    def continuation(approval, decision):
        if not second:
            second.append(1)
            return book.pause("t-1", "alice", path="/orders/ORD-2/cancel", run_id="run-2")
        return [("message.end", {"thread_id": "t-1", "run_id": "run-3", "status": "ok"})]

    book.continuation = continuation
    result = invoke("cancel both", "--url", chat_server.url, input="y\nn\n", env=ALICE)
    assert result.exit_code == 0, result.output
    assert result.output.count("Approve? [y/N]:") == 2
    assert "POST /orders/ORD-2/cancel" in result.output
    assert [d["decision"] for d in book.decisions] == ["approve", "reject"]
    assert [s["path"] for s in book.sent] == ["/orders/ORD-1/cancel"]


@pytest.mark.parametrize(
    ("setup", "status", "words"),
    [
        (lambda book, a: setattr(a, "status", "approved"), 409, "already decided"),
        (
            lambda book, a: setattr(a, "expires_at", a.expires_at.replace(year=2000)),
            410,
            "expired",
        ),
        (lambda book, a: book.approvals.clear(), 404, "no such approval"),
    ],
)
def test_a_refused_decision_exits_1_and_says_why(
    chat_server, remote, monkeypatch, setup, status, words
):
    terminal(monkeypatch)
    approval = gate(chat_server)
    setup(chat_server.book, approval)
    result = invoke("cancel", "--url", chat_server.url, input="y\n", env=ALICE)
    assert result.exit_code == 1, result.output
    assert f"was refused (HTTP {status})" in result.output
    assert words in result.output
    assert chat_server.book.sent == []


# ---------------------------------------------------------------------------
# A2A
# ---------------------------------------------------------------------------


def test_the_a2a_input_required_payload_is_found():
    payload = {"approval_id": "a1", "api": "orders", "method": "POST", "path": "/x"}
    chunk = {
        "status_update": {
            "task_id": "task-1",
            "status": {
                "state": "TASK_STATE_INPUT_REQUIRED",
                "message": {"parts": [{"data": payload}]},
            },
        }
    }
    assert find_approval_payload(chunk) == payload
    # A decision sent back names an approval but asks for nothing.
    decided = {"status": {"state": "TASK_STATE_WORKING"}, "parts": [{"data": payload}]}
    assert find_approval_payload(decided) is None
    echo = {"state": "TASK_STATE_INPUT_REQUIRED", "data": {"approval_id": "a1", "decision": "x"}}
    assert find_approval_payload(echo) is None


def test_a2a_awaiting_lines_explain_the_resume_message():
    from graph_agents_cli._approvals import Approval

    approval = Approval.from_payload(
        {"approval_id": "a1", "api": "orders", "method": "POST", "approvers": ["requester"]}
    )
    assert approval is not None
    lines = cmd_run._a2a_awaiting_lines(approval)
    assert "input-required" in lines[0]
    assert json.dumps({"approval_id": "a1", "decision": "approve"}) in lines[1]


def test_agent_text_cannot_hide_or_overwrite_the_approval(chat_server, remote):
    """Streamed text and tool output arrive before the approval: their terminal controls
    (conceal, cursor moves, a one-byte CSI, bidi overrides) are escaped, line breaks kept."""
    events = chat_server.book.pause("t-1", "alice", text="ok\x1b[8m\x1b[2A\x9b2J\nnext\tline")
    chat_server.script = [
        *events[:-1],
        (
            "tool.result",
            {"id": "x", "name": "lookup", "result": "\x1b[8mhidden", "is_error": False},
        ),
        events[-1],
    ]
    result = invoke("cancel", "--url", chat_server.url, env=ALICE)
    assert result.exit_code == 0, result.output
    out = result.output
    assert "\x1b" not in out and "\x9b" not in out
    assert "ok\\x1b[8m\\x1b[2A\\x9b2J\nnext\tline" in out
    assert "lookup -> \\x1b[8mhidden" in out


def test_a_payload_cannot_redirect_the_decision_to_another_thread(chat_server, remote, monkeypatch):
    terminal(monkeypatch)
    approval = gate(chat_server)
    events = list(chat_server.script)
    end = dict(events[-1][1])
    end["approval"] = dict(end["approval"], thread_id="t-victim")
    chat_server.script = [*events[:-1], ("message.end", end)]
    result = invoke("cancel", "--url", chat_server.url, input="y\n", env=ALICE)
    assert result.exit_code == 0, result.output
    (decision,) = [r for r in chat_server.requests if "/approvals/" in r["path"]]
    assert decision["path"] == f"/threads/t-1/approvals/{approval.approval_id}"


def test_verbose_event_lines_are_terminal_safe(chat_server, remote):
    chat_server.script = chat_server.book.pause("t-1", "alice", tool_name="look\x1b[8mup")
    result = invoke("cancel", "--url", chat_server.url, "-v", env=ALICE)
    assert result.exit_code == 0, result.output
    assert "\x1b" not in result.output
    assert "look\\x1b[8mup" in result.output
