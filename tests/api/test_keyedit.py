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

"""The comment-preserving editor behind `graph-agents-cli api` (scaffold/utils/keyedit.py)."""

from __future__ import annotations

import re

import pytest
import yaml

from graph_agents_cli.scaffold.utils.keyedit import (
    EditError,
    YamlText,
    env_insert,
    env_remove,
)

POLICY = """\
# Header comment.
apis:
  orders:                          # the orders API
    base_url_env: ORDERS_API_BASE_URL
    auth: none
    allowed_methods: [GET, POST]   # explicit
    allowed_operations:
      - operationId: listOrders    # first
        methods: [GET]
      - operationId: createOrder
        methods: [POST]
    denied_operations: []          # none yet
    timeouts_ms: {connect: 2000, read: 5000}
    # trailing note of orders
  billing:
    base_url_env: BILLING_API_BASE_URL
    auth: none
    allowed_methods: [GET]
"""


def _edited(action) -> str:
    text = YamlText(POLICY)
    action(text)
    return text.text


def test_a_flow_value_is_replaced_in_place_and_its_comment_keeps_its_column() -> None:
    out = _edited(lambda t: t.set(("apis", "orders", "allowed_methods"), ["GET"]))
    line = next(line for line in out.splitlines() if "allowed_methods" in line)
    assert line == "    allowed_methods: [GET]         # explicit"
    old = next(line for line in POLICY.splitlines() if "allowed_methods" in line)
    assert line.index("#") == old.index("#")
    # Nothing else moved.
    assert [x for x in out.splitlines() if "allowed_methods" not in x] == [
        x for x in POLICY.splitlines() if "allowed_methods" not in x
    ]


def test_append_to_a_block_list_keeps_comments() -> None:
    entry = {"operationId": "updateOrder", "path": "/orders/{order_id}", "methods": ["PATCH"]}
    out = _edited(lambda t: t.append(("apis", "orders", "allowed_operations"), entry))
    assert (
        "      - operationId: createOrder\n"
        "        methods: [POST]\n"
        "      - operationId: updateOrder\n"
        "        path: /orders/{order_id}\n"
        "        methods: [PATCH]\n"
        "    denied_operations: []          # none yet\n"
    ) in out
    assert "# first" in out and "# Header comment." in out


def test_an_empty_flow_list_becomes_a_block_list_and_keeps_its_comment() -> None:
    entry = {"operationId": "deleteOrder", "methods": ["DELETE"]}
    out = _edited(lambda t: t.append(("apis", "orders", "denied_operations"), entry))
    assert (
        "    denied_operations:             # none yet\n"
        "      - operationId: deleteOrder\n"
        "        methods: [DELETE]\n"
    ) in out
    assert yaml.safe_load(out)["apis"]["orders"]["denied_operations"] == [entry]


def test_removing_the_last_block_item_leaves_an_empty_list() -> None:
    def action(t: YamlText) -> None:
        t.append(("apis", "orders", "denied_operations"), {"path": "/admin"})
        t.remove_item(("apis", "orders", "denied_operations"), 0)

    out = _edited(action)
    assert yaml.safe_load(out) == yaml.safe_load(POLICY)
    assert "    denied_operations: []          # none yet\n" in out


def test_a_new_key_goes_after_its_neighbour_and_after_trailing_comments() -> None:
    out = _edited(
        lambda t: t.set(("apis", "orders", "limits"), {"max_calls_per_run": 5}, after=None)
    )
    assert (
        "    timeouts_ms: {connect: 2000, read: 5000}\n"
        "    # trailing note of orders\n"
        "    limits: {max_calls_per_run: 5}\n"
        "  billing:\n"
    ) in out
    placed = _edited(
        lambda t: t.set(
            ("apis", "billing", "allowed_operations"),
            [{"path": "/invoices/{id}"}],
            after="allowed_methods",
        )
    )
    assert placed.endswith(
        "    allowed_methods: [GET]\n    allowed_operations:\n      - path: /invoices/{id}\n"
    )


def test_a_new_api_is_written_in_block_style() -> None:
    api = {
        "base_url_env": "CRM_API_BASE_URL",
        "auth": "bearer",
        "token_env": "CRM_API_TOKEN",
        "allowed_methods": ["GET", "HEAD"],
        "limits": {"rate_per_minute": 60},
    }
    out = _edited(lambda t: t.set(("apis", "crm"), api))
    assert out.endswith(
        "  crm:\n"
        "    base_url_env: CRM_API_BASE_URL\n"
        "    auth: bearer\n"
        "    token_env: CRM_API_TOKEN\n"
        "    allowed_methods: [GET, HEAD]\n"
        "    limits: {rate_per_minute: 60}\n"
    )


def test_delete_takes_only_its_own_lines() -> None:
    out = _edited(lambda t: t.delete(("apis", "billing")))
    assert out == POLICY.split("  billing:\n")[0]
    out = _edited(lambda t: t.delete(("apis", "orders", "timeouts_ms")))
    assert "timeouts_ms" not in out and "# trailing note of orders" in out


def test_a_nested_value_in_a_list_item_is_replaced() -> None:
    out = _edited(
        lambda t: t.set(("apis", "orders", "allowed_operations", 0, "methods"), ["GET", "HEAD"])
    )
    assert "      - operationId: listOrders    # first\n        methods: [GET, HEAD]\n" in out


def test_unsafe_shapes_are_refused_and_nothing_changes() -> None:
    anchored = "base: &b [GET]\napis:\n  a: {allowed_methods: *b}\n"
    text = YamlText(anchored)
    with pytest.raises(EditError):
        text.set(("base",), ["POST"])  # the alias would change too
    assert text.text == anchored
    with pytest.raises(EditError):
        YamlText("- a\n- b\n")  # not a mapping
    multiline = "apis:\n  a: {x: 1,\n    y: 2}\n"
    with pytest.raises(EditError):
        YamlText(multiline).set(("apis", "a", "z"), 3)
    with pytest.raises(EditError):
        YamlText("a:\n  b: 1\n").delete(("a", "b"))  # the only key of its mapping


ALIASED = """\
apis:
  orders: &o
    base_url_env: X
    auth: none
    allowed_methods: [GET]
  billing: *o
"""


@pytest.mark.parametrize(
    "edit",
    [
        # The auditor's case: widening orders would widen billing, which repeats it.
        lambda t: t.set(("apis", "orders", "allowed_methods"), ["GET", "DELETE"]),
        lambda t: t.set(("apis", "billing", "allowed_methods"), ["GET", "DELETE"]),
        lambda t: t.set(("apis", "orders", "limits"), {"max_calls_per_run": 5}),
        lambda t: t.append(("apis", "billing", "denied_operations"), {"path": "/x"}),
        lambda t: t.delete(("apis", "orders", "auth")),
    ],
)
def test_an_edit_that_reaches_an_aliased_mapping_is_refused(edit) -> None:
    """Checked against an unshared copy: the change must land at its one key only."""
    text = YamlText(ALIASED)
    with pytest.raises(EditError, match="through a YAML alias"):
        edit(text)
    assert text.text == ALIASED


def test_merged_values_are_refused_but_unrelated_keys_stay_editable() -> None:
    merged = (
        "apis:\n  a: &base\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET]\n"
        "  b:\n    <<: *base\n    timeouts_ms: {read: 100}\n  c:\n    base_url_env: C\n"
        "    auth: none\n"
        "    allowed_methods: [GET]\n"
    )
    text = YamlText(merged)
    with pytest.raises(EditError):
        text.set(("apis", "a", "allowed_methods"), ["GET", "POST"])  # b merges it
    assert text.text == merged
    # c shares nothing: it is edited as usual, anchors elsewhere notwithstanding.
    text.set(("apis", "c", "allowed_methods"), ["GET", "POST"])
    assert yaml.safe_load(text.text)["apis"]["c"]["allowed_methods"] == ["GET", "POST"]
    assert yaml.safe_load(text.text)["apis"]["b"]["allowed_methods"] == ["GET"]
    with pytest.raises(EditError, match="recursive"):
        YamlText("a: &r\n  self: *r\n")


BLOCK_METHODS = """\
apis:
  orders:
    allowed_methods:
      - GET     # reads
      # the write the agent needs
      - POST    # create
      - HEAD
    auth: none
"""


def test_a_block_list_of_scalars_keeps_the_comments_of_the_items_that_stay() -> None:
    text = YamlText(BLOCK_METHODS)
    text.set(("apis", "orders", "allowed_methods"), ["GET", "POST", "PUT"])
    assert text.text == BLOCK_METHODS.replace("      - HEAD\n", "      - PUT\n")
    # An item that goes takes the comment lines above it; the order is the new one.
    text.set(("apis", "orders", "allowed_methods"), ["PUT", "GET"])
    assert text.text == (
        "apis:\n  orders:\n    allowed_methods:\n"
        "      - PUT\n"
        "      - GET     # reads\n"
        "    auth: none\n"
    )
    crlf = YamlText(BLOCK_METHODS.replace("\n", "\r\n"))
    crlf.set(("apis", "orders", "allowed_methods"), ["GET", "PATCH"])
    assert "      - GET     # reads\r\n      - PATCH\r\n" in crlf.text
    assert not re.search(r"[^\r]\n", crlf.text)


def test_crlf_files_stay_crlf() -> None:
    text = YamlText(POLICY.replace("\n", "\r\n"))
    text.set(("apis", "orders", "limits"), {"rate_per_minute": 1})
    assert "\r\n" in text.text and not re.search(r"[^\r]\n", text.text)


ENV = """\
# --- Auth ----
API_KEY=
# --- Outbound APIs (app/app_utils/api_client.py, api-policy.yaml) ----
# No api-policy.yaml is declared, so every outbound API call is refused. Declare
# the APIs tools may call with `graph-agents-cli api add` (see README.md).
# Path of the policy file; defaults to ./api-policy.yaml.
# API_POLICY_PATH=api-policy.yaml

# --- Limits ----
RUN_TIMEOUT_S=300
"""


def test_env_insert_goes_into_its_section_and_drops_the_stale_note() -> None:
    out = env_insert(
        ENV,
        ["# crm (auth: none)", "CRM_API_BASE_URL=http://localhost:9000"],
        section=re.compile(r"^# --- Outbound APIs"),
        before=re.compile(r"^# Path of the policy file"),
        drop=[
            "# No api-policy.yaml is declared, so every outbound API call is refused. Declare",
            "# the APIs tools may call with `graph-agents-cli api add` (see README.md).",
        ],
    )
    assert (
        "# --- Outbound APIs (app/app_utils/api_client.py, api-policy.yaml) ----\n"
        "# crm (auth: none)\n"
        "CRM_API_BASE_URL=http://localhost:9000\n"
        "# Path of the policy file; defaults to ./api-policy.yaml.\n"
    ) in out
    assert "No api-policy.yaml" not in out
    back = env_remove(out, ["CRM_API_BASE_URL"], comments=["# crm (auth: none)"])
    assert "CRM_API_BASE_URL" not in back and "# crm" not in back
    with pytest.raises(EditError, match="already set"):
        env_insert(ENV, ["API_KEY=x"])
