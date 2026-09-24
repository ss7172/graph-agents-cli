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

"""The client-side approval helpers: parsing, safe display, commands, eval matching."""

from __future__ import annotations

import shlex

import pytest

from graph_agents_cli import _chat_client
from graph_agents_cli._approvals import (
    Approval,
    awaiting_lines,
    call_matches,
    decide_commands,
    describe_match,
    json_lines,
    match_problem,
    safe_text,
)


def test_safe_text_escapes_every_control_format_and_separator_character():
    raw = "a\x1b[31mb\x07c\x7fd\x85e\u200bf\u202eg\u2028h\u2029i\ud800j\ue000k"
    text = safe_text(raw)
    assert text == ("a\\x1b[31mb\\x07c\\x7fd\\x85e\\u200bf\\u202eg\\u2028h\\u2029i\\ud800j\\ue000k")
    assert safe_text("Grüße, 注文 ORD-1") == "Grüße, 注文 ORD-1"
    assert safe_text(42) == "42"


def test_json_lines_keep_structure_and_escape_strings():
    lines = json_lines({"b": "x\ny", "a": [1, "\u202e"]})
    assert lines[0] == "{"
    assert '  "a": [' in lines
    assert any('"\\u202e"' in line for line in lines)
    assert '  "b": "x\\ny"' in lines
    assert all("\n" not in line for line in lines)


def test_from_payload_needs_an_id_and_reads_a_listing_row():
    assert Approval.from_payload(None) is None
    assert Approval.from_payload({"api": "orders"}) is None
    row = Approval.from_payload(
        {"id": "a1", "method": "post", "status": "approved", "approvers": ["role:ops"]},
        thread_id="t-9",
    )
    assert row is not None
    assert row.approval_id == "a1" and row.method == "POST" and row.thread_id == "t-9"
    assert not row.pending and not row.requester_may_decide
    pending = Approval.from_payload({"approval_id": "a2", "approvers": ["requester"]})
    assert pending is not None and pending.pending and pending.requester_may_decide


def test_lines_show_the_whole_call():
    approval = Approval.from_payload(
        {
            "approval_id": "a1",
            "thread_id": "t-1",
            "api": "orders",
            "method": "PATCH",
            "path": "/orders/ORD-1",
            "query": {"notify": True},
            "body": {"status": "cancelled"},
            "operation_id": "updateOrder",
            "reason": "update_order",
            "approvers": ["requester", "role:ops"],
            "expires_at": "2026-09-24T12:00:00Z",
        }
    )
    assert approval is not None
    text = "\n".join(approval.lines())
    assert "call:        PATCH /orders/ORD-1 (api orders, operation updateOrder)" in text
    assert '"notify": true' in text and '"status": "cancelled"' in text
    assert "approvers:   requester, role:ops" in text
    assert "expires at:  2026-09-24T12:00:00Z" in text
    no_body = Approval.from_payload({"approval_id": "a2", "method": "DELETE", "path": "/x"})
    assert no_body is not None and "  body:        (none)" in no_body.lines()


def test_decide_commands_quote_ids_and_carry_flags():
    approval = Approval.from_payload({"approval_id": "$(rm -rf ~)", "approvers": ["role:ops"]})
    assert approval is not None
    (command,) = decide_commands(approval, "t 1", " --url https://agent.example")
    assert shlex.split(command) == [
        "graph-agents-cli",
        "approvals",
        "approve",
        "$(rm -rf ~)",
        "--thread-id",
        "t 1",
        "--url",
        "https://agent.example",
    ]
    lines = awaiting_lines(approval, "t 1")
    assert lines[0].startswith("Awaiting approval by role:ops")
    assert "Only another principal can decide it" in lines[-1]


@pytest.mark.parametrize(
    ("match", "problem"),
    [
        ({"operation_id": "cancelOrder"}, None),
        ({"method": "post", "path": "/orders/{id}/cancel"}, None),
        ({"api": "orders", "operation_id": "x", "method": "GET", "path": "/x"}, None),
        ([], "must be an object"),
        ({"api": "orders"}, "needs operation_id"),
        ({"path": "/x"}, "needs operation_id"),
        ({"operation_id": "x", "path": "/x"}, "method and path go together"),
        ({"method": "GET", "path": "/x?y=1"}, "path"),
        ({"operation_id": 5}, "non-empty string"),
    ],
)
def test_match_problem(match, problem):
    found = match_problem(match)
    if problem is None:
        assert found is None
    else:
        assert found is not None and problem in found


def test_call_matches_every_given_key():
    call = {
        "api": "orders",
        "operation_id": "cancelOrder",
        "method": "POST",
        "path": "/orders/7/cancel",
    }
    assert call_matches({"operation_id": "cancelOrder"}, call)
    assert call_matches({"method": "post", "path": "/orders/{id}/cancel"}, call)
    assert not call_matches({"method": "POST", "path": "/orders/{id}"}, call)
    assert not call_matches({"method": "GET", "path": "/orders/{id}/cancel"}, call)
    assert not call_matches({"api": "crm", "operation_id": "cancelOrder"}, call)
    assert not call_matches({"operation_id": "cancelorder"}, call)
    assert not call_matches({"method": "POST", "path": "/orders/{id}/cancel"}, {"method": "POST"})
    assert describe_match({"api": "orders", "method": "post", "path": "/x"}) == "orders: POST /x"


def test_decide_approval_refuses_an_unknown_decision():
    with pytest.raises(ValueError):
        list(_chat_client.decide_approval("http://x", "t", "a", "maybe"))
