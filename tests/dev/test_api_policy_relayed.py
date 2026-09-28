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

"""How an approval rule's requester decides (`decide_with`, `relayers`), on both copies.

The CLI (`graph_agents_cli._api_policy`) and the template's runtime client carry the same
rules; both must refuse the same documents with the same text and bind the same gates.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from graph_agents_cli import _api_policy as cli

TEMPLATE_CLIENT = (
    Path(cli.__file__).resolve().parent
    / "scaffold"
    / "agents"
    / "langgraph"
    / "app"
    / "app_utils"
    / "api_client.py"
)


@pytest.fixture(scope="module", params=["cli", "runtime"])
def rules(request: pytest.FixtureRequest) -> ModuleType:
    if request.param == "cli":
        return cli
    spec = importlib.util.spec_from_file_location("template_api_client_relayed", TEMPLATE_CLIENT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _policy(**rule: Any) -> dict[str, Any]:
    approval = {"required_for": {"methods": ["POST"]}, "approvers": ["requester"], **rule}
    return {
        "apis": {
            "orders": {
                "base_url_env": "ORDERS_URL",
                "auth": "none",
                "allowed_methods": ["GET", "POST"],
                "approval": approval,
            }
        }
    }


def test_the_default_is_direct(rules: ModuleType) -> None:
    assert rules.policy_errors(_policy()) == []
    gate = rules.gated(_policy()["apis"]["orders"], "POST", "createOrder", "/orders")
    assert (gate.decide_with, gate.relayers) == ("direct", ())
    assert rules.policy_errors(_policy(decide_with="direct")) == []


def test_relayed_needs_relayers_and_binds_them(rules: ModuleType) -> None:
    policy = _policy(decide_with="relayed", relayers=["concierge", "billing"])
    assert rules.policy_errors(policy) == []
    gate = rules.gated(policy["apis"]["orders"], "POST", "createOrder", "/orders")
    assert (gate.decide_with, gate.relayers) == ("relayed", ("concierge", "billing"))
    assert gate.deciders() == (
        frozenset({"requester"}),
        "relayed",
        frozenset({"concierge", "billing"}),
    )


@pytest.mark.parametrize(
    ("rule", "error"),
    [
        (
            {"decide_with": "step_up"},
            "apis.orders.approval.decide_with: step_up is not supported yet (direct or relayed)",
        ),
        (
            {"decide_with": "any"},
            "apis.orders.approval.decide_with: must be direct or relayed (got 'any')",
        ),
        (
            {"decide_with": "relayed"},
            "apis.orders.approval.relayers: required with decide_with: relayed (the agents, by "
            "actor id, that may deliver the requester's decision)",
        ),
        (
            {"relayers": ["concierge"]},
            "apis.orders.approval.relayers: only valid with decide_with: relayed",
        ),
        (
            {"decide_with": "relayed", "relayers": []},
            "apis.orders.approval.relayers: must be a non-empty list of agent (actor) ids",
        ),
        (
            {"decide_with": "relayed", "relayers": ["two words"]},
            "apis.orders.approval.relayers[0]: 'two words' is not an agent id (1-256 characters "
            "without spaces, commas or control characters)",
        ),
        (
            {"decide_with": "relayed", "relayers": ["a"], "approvers": ["role:ops"]},
            "apis.orders.approval.decide_with: relayed needs requester in approvers (role "
            "approvers always decide with their own direct credentials, never relayed)",
        ),
    ],
)
def test_bad_decide_with_is_refused_with_the_same_text(
    rules: ModuleType, rule: dict[str, Any], error: str
) -> None:
    assert rules.policy_errors(_policy(**rule)) == [error]


def test_rules_that_decide_differently_conflict_like_other_approvers(rules: ModuleType) -> None:
    """A call the first rule covers only because it names no operation id is refused when a
    later rule, with the same approvers but relayed, also covers it: it could be either."""
    api = {
        "base_url_env": "ORDERS_URL",
        "auth": "none",
        "allowed_methods": ["POST"],
        "approval": [
            {
                "required_for": {"operations": [{"operationId": "cancelOrder"}]},
                "approvers": ["requester"],
            },
            {
                "required_for": {"methods": ["POST"]},
                "approvers": ["requester"],
                "decide_with": "relayed",
                "relayers": ["concierge"],
            },
        ],
    }
    assert rules.policy_errors({"apis": {"orders": api}}) == []
    with pytest.raises(rules.ApprovalRuleConflict) as exc:
        rules.gated(api, "POST", None, "/orders/7/cancel")
    assert "(approved by requester; relayed by concierge) also covers it" in str(exc.value)
    named = rules.gated(api, "POST", "cancelOrder", "/orders/7/cancel")
    assert named.index == 0 and named.decide_with == "direct"
    # The same rules, both direct: one gate, so no conflict.
    same = {**api, "approval": [api["approval"][0], {**api["approval"][1]}]}
    same["approval"][1].pop("decide_with")
    same["approval"][1].pop("relayers")
    assert rules.gated(same, "POST", None, "/orders/7/cancel").index == 0


def test_the_cli_names_relayed_gates() -> None:
    api = _policy(decide_with="relayed", relayers=["concierge"])["apis"]["orders"]
    gate = cli.gated(api, "POST", "createOrder", "/orders")
    assert cli.describe_gate(gate).startswith("requester; relayed by concierge (approval.")
    assert cli.gate_payload(gate)["relayers"] == ["concierge"]
    direct = cli.gated(_policy()["apis"]["orders"], "POST", "createOrder", "/orders")
    assert "decide_with" not in cli.gate_payload(direct)  # as before 0.3
    assert cli.effective_approval(api)["decide_with"] == "relayed"
    conflicting = {
        **api,
        "approval": [
            {"required_for": {"operations": [{"operationId": "x"}]}, "approvers": ["requester"]},
            api["approval"],
        ],
    }
    assert cli.rule_conflicts(conflicting) == [(0, [{"operationId": "x"}], [1])]
