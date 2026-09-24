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

"""`graph-agents-cli approvals` against the fake approval contract.

Also the adversarial cases a client must survive: deciding someone else's
call, replaying a decision, two decisions racing, deciding after expiry, a
thread deleted while its call waits, and ids that try to leave the route.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from graph_agents_cli import _chat_client
from graph_agents_cli._chat_client import ChatHTTPError
from graph_agents_cli.main import main
from graph_agents_cli.run import _local_server, cmd_approvals

from .conftest import FakeChatServer

ENV = {"GRAPH_AGENTS_CLI_NO_UPDATE_CHECK": "1", "GRAPH_AGENTS_CLI_DISABLE_OVERRIDES": "1"}


@pytest.fixture(autouse=True)
def _outside_a_project(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GRAPH_AGENTS_CLI_API_KEY", raising=False)


def approvals(*args: str, token: str | None = "alice-token"):
    env = dict(ENV)
    if token:
        env["GRAPH_AGENTS_CLI_API_KEY"] = token
    return CliRunner().invoke(main, ["approvals", *args], env=env)


def paused(server: FakeChatServer, thread_id: str = "t-1", requester: str = "alice", **fields):
    server.book.pause(thread_id, requester, **fields)
    return next(a for a in reversed(server.book.approvals.values()) if a.thread_id == thread_id)


def decisions(server: FakeChatServer) -> list[dict]:
    return [r for r in server.requests if r["method"] == "POST" and "/approvals/" in r["path"]]


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_shows_a_threads_pending_calls_and_how_to_decide_them(chat_server):
    approval = paused(chat_server)
    result = approvals("list", "--url", chat_server.url, "--thread-id", "t-1")
    assert result.exit_code == 0, result.output
    out = result.output
    assert "call:        POST /orders/ORD-1/cancel (api orders, operation cancelOrder)" in out
    assert "status:      pending" in out
    assert (
        f"Approve: graph-agents-cli approvals approve {approval.approval_id} --thread-id t-1 "
        f"--url {chat_server.url}"
    ) in out
    (req,) = [r for r in chat_server.requests if r["path"].endswith("/approvals")]
    assert req["headers"]["authorization"] == "Bearer alice-token"


def test_list_without_a_thread_searches_your_own_threads(chat_server):
    paused(chat_server, "t-1")
    paused(chat_server, "t-2", path="/orders/ORD-2/cancel")
    paused(chat_server, "t-bob", requester="bob", path="/orders/ORD-9/cancel")
    result = approvals("list", "--url", chat_server.url)
    assert result.exit_code == 0, result.output
    assert "ORD-1" in result.output and "ORD-2" in result.output
    assert "ORD-9" not in result.output  # bob's thread is not alice's
    assert "another principal's thread" in result.output


def test_list_hides_decided_ones_unless_all_and_prints_json(chat_server):
    approval = paused(chat_server)
    approval.status = "approved"
    assert (
        "No pending approvals on thread t-1."
        in approvals("list", "--url", chat_server.url, "--thread-id", "t-1").output
    )
    shown = approvals("list", "--url", chat_server.url, "--thread-id", "t-1", "--all")
    assert "status:      approved" in shown.output
    data = json.loads(
        approvals("list", "--url", chat_server.url, "--thread-id", "t-1", "--all", "--json").output
    )
    assert [a["approval_id"] for a in data["approvals"]] == [approval.approval_id]


def test_listing_someone_elses_thread_is_refused(chat_server):
    paused(chat_server)
    result = approvals("list", "--url", chat_server.url, "--thread-id", "t-1", token="carol-token")
    assert result.exit_code == 1
    assert "HTTP 403" in result.output and "may not see this thread's approvals" in result.output


def test_listing_without_credentials_gives_the_auth_hint(chat_server):
    paused(chat_server)
    result = approvals("list", "--url", chat_server.url, "--thread-id", "t-1", token=None)
    assert result.exit_code == 1
    assert "HTTP 401" in result.output and "GRAPH_AGENTS_CLI_API_KEY" in result.output


# ---------------------------------------------------------------------------
# approve / reject
# ---------------------------------------------------------------------------


def test_approve_shows_the_call_sends_only_the_decision_and_streams_the_run(chat_server):
    approval = paused(chat_server)
    result = approvals(
        "approve", approval.approval_id, "--url", chat_server.url, "--thread-id", "t-1",
        "--comment", "checked with the customer",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    out = result.output
    assert out.index("call:        POST /orders/ORD-1/cancel") < out.index("Approving")
    assert "[tool_result: cancel_order -> cancelled]" in out
    assert "[agent]: Done: ORD-1 is cancelled." in out
    (req,) = decisions(chat_server)
    assert req["body"] == {"decision": "approve", "comment": "checked with the customer"}
    assert chat_server.book.sent == [
        {
            "method": "POST",
            "path": "/orders/ORD-1/cancel",
            "body": {"reason": "duplicate"},
            "query": {},
        }
    ]


def test_reject_sends_nothing(chat_server):
    approval = paused(chat_server)
    result = approvals(
        "reject", approval.approval_id, "--url", chat_server.url, "--thread-id", "t-1"
    )
    assert result.exit_code == 0, result.output
    assert "The cancellation was not approved." in result.output
    assert chat_server.book.sent == []
    assert decisions(chat_server)[0]["body"] == {"decision": "reject"}


def test_a_decision_without_a_thread_finds_the_approval_on_your_threads(chat_server):
    paused(chat_server, "t-other", path="/orders/ORD-5/cancel")
    approval = paused(chat_server, "t-1")
    result = approvals("approve", approval.approval_id, "--url", chat_server.url)
    assert result.exit_code == 0, result.output
    (req,) = decisions(chat_server)
    assert req["path"] == f"/threads/t-1/approvals/{approval.approval_id}"


def test_an_unknown_approval_is_not_found_and_nothing_is_posted(chat_server):
    paused(chat_server)
    result = approvals("approve", "nope", "--url", chat_server.url)
    assert result.exit_code == 1
    assert "No approval nope on your threads" in result.output
    assert "needs --thread-id" in result.output
    assert decisions(chat_server) == []


def test_a_resumed_run_that_pauses_again_prints_the_next_call(chat_server):
    approval = paused(chat_server)
    book = chat_server.book
    book.continuation = lambda a, d: book.pause("t-1", "alice", path="/orders/ORD-2/cancel")
    result = approvals(
        "approve", approval.approval_id, "--url", chat_server.url, "--thread-id", "t-1"
    )
    assert result.exit_code == 0, result.output
    assert "POST /orders/ORD-2/cancel" in result.output
    assert "Awaiting approval by requester" in result.output


# ---------------------------------------------------------------------------
# adversarial
# ---------------------------------------------------------------------------


def test_someone_else_cannot_approve_a_requester_confirmation(chat_server):
    """audra may read the thread (read-across) but is not an approver: 403, nothing sent."""
    approval = paused(chat_server)
    result = approvals(
        "approve", approval.approval_id, "--url", chat_server.url, "--thread-id", "t-1",
        token="auditor-token",
    )  # fmt: skip
    assert result.exit_code == 1
    assert "refused (HTTP 403)" in result.output
    assert "not an allowed approver" in result.output
    assert chat_server.book.sent == [] and approval.status == "pending"


def test_four_eyes_the_requester_cannot_approve_the_role_holder_can(chat_server):
    approval = paused(chat_server, approvers=["role:ops"])
    mine = approvals(
        "approve", approval.approval_id, "--url", chat_server.url, "--thread-id", "t-1"
    )
    assert mine.exit_code == 1 and "refused (HTTP 403)" in mine.output
    assert chat_server.book.sent == []
    ops = approvals(
        "approve", approval.approval_id, "--url", chat_server.url, "--thread-id", "t-1",
        token="bob-token",
    )  # fmt: skip
    assert ops.exit_code == 0, ops.output
    assert len(chat_server.book.sent) == 1
    assert chat_server.book.decisions[-1]["by"] == "bob"


def test_a_replayed_approval_is_refused_before_it_is_posted(chat_server):
    approval = paused(chat_server)
    args = ("approve", approval.approval_id, "--url", chat_server.url, "--thread-id", "t-1")
    assert approvals(*args).exit_code == 0
    again = approvals(*args)
    assert again.exit_code == 1
    assert "is approved, not pending" in again.output
    assert len(decisions(chat_server)) == 1 and len(chat_server.book.sent) == 1


def test_a_replay_the_server_refuses_is_reported_and_sends_nothing_more(chat_server):
    """A listing read before another decision landed: the server's 409 is final."""
    approval = paused(chat_server)
    chat_server.book.stale_listing = True
    args = ("reject", approval.approval_id, "--url", chat_server.url, "--thread-id", "t-1")
    assert approvals(*args).exit_code == 0
    again = approvals(*args)
    assert again.exit_code == 1
    assert "refused (HTTP 409)" in again.output and "already decided" in again.output
    assert chat_server.book.sent == []


def test_two_racing_decisions_one_wins_the_other_gets_409(chat_server):
    approval = paused(chat_server)
    headers = {"Authorization": "Bearer alice-token"}
    outcomes: list = []
    barrier = threading.Barrier(2)

    def decide(decision: str) -> None:
        barrier.wait()
        try:
            list(
                _chat_client.decide_approval(
                    chat_server.url, "t-1", approval.approval_id, decision, headers=headers
                )
            )
            outcomes.append((decision, 200))
        except ChatHTTPError as exc:
            outcomes.append((decision, exc.status_code))

    workers = [threading.Thread(target=decide, args=(d,)) for d in ("approve", "reject")]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
    assert sorted(code for _d, code in outcomes) == [200, 409]
    winner = next(d for d, code in outcomes if code == 200)
    assert len(chat_server.book.sent) == (1 if winner == "approve" else 0)


def test_a_decision_after_expiry_is_refused_and_nothing_is_sent(chat_server):
    approval = paused(chat_server, expires_in=-1)
    chat_server.book.stale_listing = True  # the listing still shows it pending
    result = approvals(
        "approve", approval.approval_id, "--url", chat_server.url, "--thread-id", "t-1"
    )
    assert result.exit_code == 1
    assert "refused (HTTP 410)" in result.output and "expired" in result.output
    assert chat_server.book.sent == []


def test_a_thread_deleted_while_its_call_waits_cannot_be_decided(chat_server):
    approval = paused(chat_server)
    assert chat_server.book.delete_thread("t-1", {"authorization": "Bearer alice-token"}) == 204
    result = approvals(
        "approve", approval.approval_id, "--url", chat_server.url, "--thread-id", "t-1"
    )
    assert result.exit_code == 1
    assert "HTTP 404" in result.output and "deleted thread" in result.output
    assert chat_server.book.sent == []


def test_ids_stay_one_path_segment(chat_server):
    paused(chat_server)
    for bad in ("../../chat", "a/b", "x?decision=approve", "%2e%2e"):
        url = _chat_client.approval_url(chat_server.url, "t-1", bad)
        assert url.startswith(f"{chat_server.url}/threads/t-1/approvals/")
        assert "/" not in url[len(f"{chat_server.url}/threads/t-1/approvals/") :]
        assert "?" not in url
    with pytest.raises(ValueError):
        _chat_client.approval_url(chat_server.url, "t-1", "")
    result = approvals("approve", "../../chat", "--url", chat_server.url, "--thread-id", "t-1")
    assert result.exit_code == 1
    assert not [r for r in chat_server.requests if r["path"] == "/chat"]


# ---------------------------------------------------------------------------
# local server
# ---------------------------------------------------------------------------


@pytest.fixture
def local(monkeypatch, tmp_path: Path, chat_server):
    (tmp_path / ".env").write_text("API_KEY=local-key\nCHECKPOINTER=memory\n")
    cfg = SimpleNamespace(agent_directory="app", runtime="fastapi", checkpointer="memory")
    monkeypatch.setattr(cmd_approvals, "chdir_project_root", lambda *a, **k: None)
    monkeypatch.setattr(cmd_approvals, "read_project_config", lambda *a, **k: cfg)
    monkeypatch.setattr(cmd_approvals, "require_agent_directory", lambda cfg: None)
    port = int(chat_server.url.rsplit(":", 1)[1])
    state = SimpleNamespace(port=None, started=[], stopped=[])
    monkeypatch.setattr(_local_server, "get_server_port", lambda root: state.port)
    monkeypatch.setattr(_local_server, "touch_activity", lambda root: None)

    def ensure(root, agent_dir, **kwargs):
        state.started.append(kwargs)
        return _local_server.ServerInfo(port=port, started=True, pid=77, checkpointer="postgres")

    monkeypatch.setattr(_local_server, "ensure_server", ensure)
    monkeypatch.setattr(
        _local_server, "stop_server", lambda root, pid=None: state.stopped.append(pid)
    )
    state.url_port = port
    return state


def test_locally_the_running_server_and_the_env_api_key_are_used(local, chat_server):
    local.port = local.url_port
    approval = paused(chat_server, requester="shared")
    result = approvals("approve", approval.approval_id, "--thread-id", "t-1", token=None)
    assert result.exit_code == 0, result.output
    (req,) = decisions(chat_server)
    assert req["headers"]["authorization"] == "Bearer local-key"
    assert local.started == [] and local.stopped == []
    # Printed commands carry no --url for a local server.
    listed = approvals("list", "--thread-id", "t-1", "--all", token=None)
    assert "--url" not in listed.output


def test_locally_with_no_server_and_memory_nothing_can_be_pending(local, chat_server):
    listed = approvals("list", token=None)
    assert listed.exit_code == 0
    assert "No local server is running" in listed.output
    decided = approvals("approve", "a1", "--thread-id", "t-1", token=None)
    assert decided.exit_code == 1
    assert "nothing is pending" in decided.output
    assert local.started == []


def test_locally_with_postgres_a_temporary_server_is_started_and_stopped(
    local, chat_server, monkeypatch
):
    monkeypatch.setenv("CHECKPOINTER", "postgres")
    approval = paused(chat_server, requester="shared")
    result = approvals("reject", approval.approval_id, "--thread-id", "t-1", token=None)
    assert result.exit_code == 0, result.output
    assert len(local.started) == 1 and local.stopped == [77]


def test_help_lists_the_subcommands():
    result = CliRunner().invoke(main, ["approvals", "--help"], env=ENV)
    assert result.exit_code == 0
    for sub in ("list", "approve", "reject"):
        assert sub in result.output


def test_locally_under_langgraph_server_a_temporary_server_is_started(
    local, chat_server, monkeypatch
):
    """`langgraph dev` keeps its state across restarts: look there rather than refuse."""
    cfg = SimpleNamespace(agent_directory="app", runtime="langgraph-server", checkpointer="memory")
    monkeypatch.setattr(cmd_approvals, "read_project_config", lambda *a, **k: cfg)
    listed = approvals("list", "--thread-id", "t-1", token=None)
    assert "No local server is running" not in listed.output
    assert len(local.started) == 1 and local.started[0]["runtime"] == "langgraph-server"
    assert local.stopped == [77]


def test_json_listing_escapes_what_the_model_wrote(chat_server):
    paused(chat_server, body={"note": "\x1b[2J\u202e"})
    result = approvals("list", "--url", chat_server.url, "--thread-id", "t-1", "--json")
    assert result.exit_code == 0, result.output
    assert "\x1b" not in result.output and "\\u001b[2J\\u202e" in result.output
    assert json.loads(result.output)["approvals"][0]["body"] == {"note": "\x1b[2J\u202e"}
