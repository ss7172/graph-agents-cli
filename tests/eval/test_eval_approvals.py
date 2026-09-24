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

"""Eval cases that reach gated calls: instructions, generate, traces and checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from conftest import read_traces, sse

from graph_agents_cli._chat_client import ChatHTTPError
from graph_agents_cli.eval import _paths
from graph_agents_cli.eval._client import run_case
from graph_agents_cli.eval._common import EvalConfigError, write_json_file
from graph_agents_cli.eval.checks import check_approvals, check_no_approvals, run_checks
from graph_agents_cli.eval.cmd_generate import cmd_generate
from graph_agents_cli.eval.dataset import parse_case

CANCEL = {
    "approval_id": "a-1",
    "api": "orders",
    "method": "POST",
    "path": "/orders/ORD-1/cancel",
    "query": {},
    "body": {"reason": "duplicate"},
    "operation_id": "cancelOrder",
    "reason": "cancel_order",
    "approvers": ["requester"],
    "expires_at": "2026-09-24T12:00:00Z",
}


def paused(approval: dict[str, Any] = CANCEL, thread_id: str = "t-1") -> list[Any]:
    return [
        sse("message.start", {"thread_id": thread_id, "run_id": "r-1"}),
        sse("message.delta", {"text": "Cancelling. "}),
        sse("tool.call", {"id": "c1", "name": "cancel_order", "args": {"order_id": "ORD-1"}}),
        sse(
            "message.end",
            {
                "thread_id": thread_id,
                "run_id": "r-1",
                "usage": {"input_tokens": 10, "output_tokens": 2},
                "latency_ms": 30,
                "status": "awaiting_approval",
                "approval": approval,
            },
        ),
    ]


def resumed(approved: bool, thread_id: str = "t-1") -> list[Any]:
    return [
        sse("message.start", {"thread_id": thread_id, "run_id": "r-2"}),
        sse(
            "tool.result",
            {
                "id": "c1",
                "name": "cancel_order",
                "result": "cancelled" if approved else "not approved",
                "is_error": not approved,
            },
        ),
        sse("message.delta", {"text": "Done." if approved else "Not approved."}),
        sse(
            "message.end",
            {
                "thread_id": thread_id,
                "run_id": "r-2",
                "usage": {"input_tokens": 4, "output_tokens": 3},
                "latency_ms": 12,
                "status": "ok",
            },
        ),
    ]


class FakeDecide:
    """Replacement for ``_chat_client.decide_approval``."""

    def __init__(self, refuse: int | None = None, then: Any = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.refuse = refuse
        self.then = then

    def __call__(self, base_url, thread_id, approval_id, decision, **kwargs):
        self.calls.append(
            {
                "base_url": base_url,
                "thread_id": thread_id,
                "approval_id": approval_id,
                "decision": decision,
                **kwargs,
            }
        )
        if self.refuse:
            raise ChatHTTPError(self.refuse, '{"detail": "no"}', "http://x")
        if callable(self.then):
            yield from self.then(len(self.calls), decision)
            return
        yield from resumed(decision == "approve", thread_id)


@pytest.fixture
def fake_decide(monkeypatch: pytest.MonkeyPatch):
    def _install(**kwargs: Any) -> FakeDecide:
        decide = FakeDecide(**kwargs)
        monkeypatch.setattr("graph_agents_cli._chat_client.decide_approval", decide)
        return decide

    return _install


def case(approvals: Any = None, expect: dict[str, Any] | None = None, **extra: Any):
    raw: dict[str, Any] = {"id": "cancel", "messages": [{"role": "user", "content": "cancel it"}]}
    if approvals is not None:
        raw["approvals"] = approvals
    if expect is not None:
        raw["expect"] = expect
    raw.update(extra)
    return parse_case(raw)


APPROVE_CANCEL = [{"decision": "approve", "match": {"operation_id": "cancelOrder"}}]


# ---------------------------------------------------------------------------
# dataset
# ---------------------------------------------------------------------------


def test_valid_instructions_and_expectations_parse() -> None:
    parsed = case(
        [
            {"decision": "approve", "match": {"operation_id": "cancelOrder"}},
            {"decision": "reject", "match": {"api": "orders", "method": "post", "path": "/o/{id}"}},
        ],
        expect={"approvals": [{"match": {"operation_id": "cancelOrder"}, "status": "approved"}]},
    )
    assert parsed.approvals[1]["decision"] == "reject"
    assert parsed.expect["approvals"][0]["status"] == "approved"
    assert case().approvals == []


@pytest.mark.parametrize(
    ("approvals", "expect", "message"),
    [
        ({"decision": "approve"}, None, "'approvals' must be a list"),
        (["approve"], None, "approvals[0] must be an object"),
        ([{"decision": "yes", "match": {"operation_id": "x"}}], None, "decision must be one of"),
        ([{"decision": "approve"}], None, "approvals[0].match: must be an object"),
        ([{"decision": "approve", "match": {}}], None, "must be an object"),
        ([{"decision": "approve", "match": {"method": "POST"}}], None, "needs operation_id"),
        (
            [{"decision": "approve", "match": {"operation_id": "x", "method": "POST"}}],
            None,
            "method and path go together",
        ),
        (
            [{"decision": "approve", "match": {"method": "FETCH", "path": "/x"}}],
            None,
            "unknown HTTP method",
        ),
        ([{"decision": "approve", "match": {"method": "GET", "path": "x"}}], None, "path must be"),
        ([{"decision": "approve", "match": {"operationId": "x"}}], None, "unknown key(s)"),
        ([{"decision": "approve", "match": {"operation_id": ""}}], None, "non-empty string"),
        (
            [{"decision": "approve", "match": {"operation_id": "x"}, "always": True}],
            None,
            "unknown key(s) always",
        ),
        (None, {"approvals": {"match": {}}}, "expect.approvals must be a list"),
        (None, {"approvals": [{"status": "approved"}]}, "must be an object"),
        (
            None,
            {"approvals": [{"match": {"operation_id": "x"}, "status": "sent"}]},
            "status must be one of gated, approved, rejected",
        ),
        (None, {"approvals": [{"match": {"operation_id": "x"}, "who": 1}]}, "unknown key(s) who"),
        (None, {"no_approvals": "yes"}, "no_approvals must be true or false"),
        (
            None,
            {"no_approvals": True, "approvals": [{"match": {"operation_id": "x"}}]},
            "conflict",
        ),
    ],
)
def test_invalid_instructions_and_expectations_are_config_errors(approvals, expect, message):
    with pytest.raises(EvalConfigError) as excinfo:
        case(approvals, expect)
    assert message in str(excinfo.value)


# ---------------------------------------------------------------------------
# generate: resolving gates
# ---------------------------------------------------------------------------


def test_an_approve_instruction_approves_and_the_continuation_is_one_turn(
    fake_chat, fake_decide
) -> None:
    fake_chat({"cancel it": paused()})
    decide = fake_decide()
    trace = run_case("http://x", case(APPROVE_CANCEL), headers={"Authorization": "Bearer e"})
    assert trace["status"] == "ok", trace["error"]
    assert trace["response"] == "Cancelling. Done."
    (call,) = trace["tool_calls"]
    assert call["name"] == "cancel_order" and call["result"] == "cancelled"
    assert trace["usage"] == {"input_tokens": 14, "output_tokens": 5}
    assert trace["latency_ms"] == 42 and trace["run_id"] == "r-2"
    (decision,) = decide.calls
    assert decision["decision"] == "approve" and decision["approval_id"] == "a-1"
    assert decision["thread_id"] == "t-1" and decision["comment"] == "eval case cancel"
    assert decision["headers"] == {"Authorization": "Bearer e"}
    assert trace["approvals"] == [
        {
            "approval_id": "a-1",
            "api": "orders",
            "method": "POST",
            "path": "/orders/ORD-1/cancel",
            "operation_id": "cancelOrder",
            "approvers": ["requester"],
            "match": "cancelOrder",
            "decision": "approve",
            "status": "approved",
            "error": None,
        }
    ]


def test_a_reject_instruction_rejects_and_the_first_match_wins(fake_chat, fake_decide) -> None:
    fake_chat({"cancel it": paused()})
    decide = fake_decide()
    instructions = [
        {"decision": "reject", "match": {"method": "POST", "path": "/orders/{id}/cancel"}},
        {"decision": "approve", "match": {"operation_id": "cancelOrder"}},
    ]
    trace = run_case("http://x", case(instructions), headers={})
    assert trace["status"] == "ok"
    assert decide.calls[0]["decision"] == "reject"
    assert trace["approvals"][0]["status"] == "rejected"
    assert trace["tool_calls"][0]["is_error"] is True


def test_an_unexpected_gate_is_a_case_error_and_nothing_is_decided(fake_chat, fake_decide) -> None:
    fake_chat({"cancel it": paused()})
    decide = fake_decide()
    other = [{"decision": "approve", "match": {"operation_id": "refundOrder"}}]
    for instructions in (None, other):
        trace = run_case("http://x", case(instructions), headers={})
        assert trace["status"] == "error"
        assert "unexpected approval gate on POST /orders/ORD-1/cancel" in trace["error"]
        assert "add one" in trace["error"]
        assert trace["approvals"][0]["status"] == "unexpected"
    assert decide.calls == []


def test_an_api_in_the_match_narrows_it(fake_chat, fake_decide) -> None:
    fake_chat({"cancel it": paused()})
    fake_decide()
    wrong_api = [{"decision": "approve", "match": {"api": "crm", "operation_id": "cancelOrder"}}]
    assert run_case("http://x", case(wrong_api), headers={})["status"] == "error"


@pytest.mark.parametrize(
    ("code", "status"),
    [(403, "forbidden"), (404, "not_found"), (409, "not_pending"), (410, "expired")],
)
def test_a_refused_decision_is_a_case_error(fake_chat, fake_decide, code, status) -> None:
    fake_chat({"cancel it": paused()})
    fake_decide(refuse=code)
    trace = run_case("http://x", case(APPROVE_CANCEL), headers={})
    assert trace["status"] == "error"
    assert f"was refused (HTTP {code}" in trace["error"]
    assert trace["approvals"][0]["status"] == status
    if code == 403:
        assert "GRAPH_AGENTS_CLI_APPROVER_API_KEY" in trace["error"]


def test_several_gates_in_one_turn_and_a_bound_on_them(fake_chat, fake_decide) -> None:
    fake_chat({"cancel it": paused()})
    second = dict(CANCEL, approval_id="a-2", path="/orders/ORD-2/cancel")

    def then(n: int, decision: str):
        return paused(second) if n == 1 else resumed(True)

    fake_decide(then=then)
    trace = run_case("http://x", case(APPROVE_CANCEL), headers={})
    assert trace["status"] == "ok"
    assert [a["approval_id"] for a in trace["approvals"]] == ["a-1", "a-2"]

    fake_decide(then=lambda n, d: paused(dict(CANCEL, approval_id=f"a-{n + 1}")))
    looping = run_case("http://x", case(APPROVE_CANCEL), headers={})
    assert looping["status"] == "error" and "more than 20 gated calls" in looping["error"]


def test_a_pause_without_an_approval_id_is_an_error(fake_chat, fake_decide) -> None:
    fake_chat({"cancel it": paused({"api": "orders"})})
    fake_decide()
    trace = run_case("http://x", case(APPROVE_CANCEL), headers={})
    assert trace["status"] == "error" and "without an approval id" in trace["error"]


def _write_dataset(project: Path, cases: list[dict[str, Any]]) -> None:
    write_json_file(project / _paths.DEFAULT_INPUT_DATASET, {"cases": cases})


def test_generate_records_approvals_and_uses_the_approver_credential(
    project: Path, runner: CliRunner, fake_chat, fake_decide, monkeypatch
) -> None:
    _write_dataset(
        project,
        [
            {
                "id": "cancel",
                "messages": [{"role": "user", "content": "cancel it"}],
                "approvals": APPROVE_CANCEL,
                "expect": {
                    "approvals": [{"match": {"operation_id": "cancelOrder"}, "status": "approved"}]
                },
            },
            {"id": "surprise", "messages": [{"role": "user", "content": "surprise"}]},
        ],
    )
    fake_chat({"cancel it": paused(), "surprise": paused(thread_id="t-2")})
    decide = fake_decide()
    monkeypatch.setenv("GRAPH_AGENTS_CLI_APPROVER_API_KEY", "ops-key")
    result = runner.invoke(
        cmd_generate,
        ["--url", "http://agent.example", "--header", "Authorization: Bearer eval-key"],
        catch_exceptions=False,
    )
    assert result.exit_code == 2, result.output
    out = " ".join(result.output.split())
    assert "1 case(s) approve gated calls (cancel)" in out
    assert "surprise: error: unexpected approval gate" in out
    traces = {t["case_id"]: t for t in read_traces(project)["traces"]}
    assert traces["cancel"]["status"] == "ok"
    assert traces["cancel"]["approvals"][0]["status"] == "approved"
    assert traces["surprise"]["approvals"][0]["status"] == "unexpected"
    # The decision went as the approver; the chat as the eval identity.
    assert decide.calls[0]["headers"]["Authorization"] == "Bearer ops-key"


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


def record(status: str, **fields: Any) -> dict[str, Any]:
    return {
        "approval_id": "a",
        "api": "orders",
        "method": "POST",
        "path": "/orders/ORD-1/cancel",
        "operation_id": "cancelOrder",
        "status": status,
        **fields,
    }


def test_check_approvals_matches_status_and_distinct_gates() -> None:
    by_id = {"operation_id": "cancelOrder"}
    by_path = {"method": "post", "path": "/orders/{id}/cancel"}
    assert check_approvals([{"match": by_id, "status": "approved"}], [record("approved")])[0]
    assert check_approvals([{"match": by_path}], [record("rejected")])[0]  # gated = any outcome
    passed, reason = check_approvals([{"match": by_id, "status": "approved"}], [record("rejected")])
    assert not passed and "to be approved" in reason and "(rejected)" in reason
    two = [{"match": by_id}, {"match": by_id}]
    assert not check_approvals(two, [record("approved")])[0]
    assert check_approvals(two, [record("approved"), record("rejected")])[0]
    assert not check_approvals([{"match": by_id}], [])[0]
    assert check_no_approvals([])[0]
    passed, reason = check_no_approvals([record("unexpected")])
    assert not passed and "cancelOrder POST /orders/ORD-1/cancel (unexpected)" in reason


def test_run_checks_reads_the_final_turn_or_every_turn() -> None:
    trace = {
        "response": "ok",
        "approvals": [],
        "turns": [
            {"response": "a", "approvals": [record("rejected")]},
            {"response": "ok", "approvals": []},
        ],
    }
    expect = case(
        expect={"approvals": [{"match": {"operation_id": "cancelOrder"}, "status": "rejected"}]}
    ).expect
    assert run_checks(expect, trace)["approvals"]["passed"] is False
    expect_all = dict(expect, scope="all_turns")
    assert run_checks(expect_all, trace)["approvals"]["passed"] is True
    none = case(expect={"no_approvals": True}).expect
    assert run_checks(none, trace)["no_approvals"]["passed"] is True
    assert run_checks(dict(none, scope="all_turns"), trace)["no_approvals"]["passed"] is False
