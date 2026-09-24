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

"""`create`/`lint` and the scaffolded runtime judge api-policy.yaml identically.

The CLI (``graph_agents_cli._api_policy``) and the template's runtime client
(``app_utils/api_client.py``) carry the same block of rules. These tests keep
the two copies byte-identical and feed the same valid and invalid policies,
and the same calls, to both sides: they must accept and refuse the same
documents with the same error text, allow and refuse the same calls, and
gate the same calls behind a human approval (``gated``).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

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
BEGIN = "# --- BEGIN SHARED API POLICY RULES ---"
END = "# --- END SHARED API POLICY RULES ---"


def _block(text: str) -> str:
    return text[text.index(BEGIN) : text.index(END) + len(END)]


@pytest.fixture(scope="module")
def runtime() -> ModuleType:
    """The template's api_client module, imported from its source (it has no template variables)."""
    spec = importlib.util.spec_from_file_location("template_api_client", TEMPLATE_CLIENT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_shared_block_is_byte_identical() -> None:
    cli_block = _block(Path(cli.__file__).read_text(encoding="utf-8"))
    runtime_block = _block(TEMPLATE_CLIENT.read_text(encoding="utf-8"))
    assert cli_block == runtime_block, (
        "The SHARED API POLICY RULES blocks of graph_agents_cli/_api_policy.py and the "
        "template's app_utils/api_client.py differ; copy the CLI block into the template."
    )


def test_the_runtime_module_renders_unchanged() -> None:
    """cookiecutter renders the file: it must hold no template syntax at all."""
    source = TEMPLATE_CLIENT.read_text(encoding="utf-8")
    env = Environment(undefined=StrictUndefined, keep_trailing_newline=True)
    assert env.from_string(source).render(cookiecutter={}) == source


VALID = [
    "apis:\n  a:\n    base_url_env: A_URL\n    auth: none\n    allowed_methods: [GET, POST]\n",
    (
        "apis:\n  billing:\n    base_url_env: BILLING_API_BASE_URL\n    auth: bearer\n"
        "    token_env: BILLING_API_TOKEN\n    allowed_methods: [get, POST]\n"
        "    allowed_operations:\n      - operationId: listInvoices\n"
        "      - path: /invoices/{invoice_id}\n        methods: [GET]\n"
        "      - operationId: getInvoice\n        path: /invoices/{id}/\n"
        "    denied_operations: []\n    openapi: docs/billing.yaml\n"
        "    timeouts_ms: {connect: 100, read: 200}\n"
        "    pagination: {page_size_param: limit, max_page_size: 50}\n"
    ),
    (
        "apis:\n  me:\n    base_url_env: ME_URL\n    auth: forward\n"
        "    forward_header: X-User-Token\n    allowed_methods: ['*']\n"
        "  other_api_2:\n    base_url_env: O\n    auth: forward\n    allowed_methods: [DELETE]\n"
    ),
    (
        "apis:\n  orders:\n    base_url_env: ORDERS_URL\n    auth: none\n"
        "    allowed_methods: [GET, HEAD, POST, PUT, PATCH, DELETE]\n"
        "    limits: {max_calls_per_run: 20, rate_per_minute: 120}\n"
        "  audit:\n    base_url_env: AUDIT_URL\n    auth: none\n    allowed_methods: [POST]\n"
        "    limits: {rate_per_minute: 1}\n"
    ),
    # approval: the API-level gate, in every shape the schema accepts
    (
        "apis:\n  orders:\n    base_url_env: ORDERS_URL\n    auth: bearer\n"
        "    token_env: ORDERS_TOKEN\n    allowed_methods: [GET, POST, PUT, PATCH, DELETE]\n"
        "    approval:\n      required_for:\n        methods: [POST, PATCH, PUT, DELETE]\n"
        "        operations:\n          - operationId: cancelOrder\n"
        "      approvers: [requester]\n      timeout_s: 900\n"
    ),
    (
        "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: ['*']\n"
        "    approval:\n      required_for: {methods: ['*']}\n"
        "      approvers: [requester, 'role:ops', 'role:finance.approvers']\n"
        "      timeout_s: 30\n"
        "  b:\n    base_url_env: B\n    auth: none\n    allowed_methods: [get, delete]\n"
        "    approval:\n      required_for:\n        operations:\n"
        "          - path: /orders/{order_id}\n            methods: [delete]\n"
        "          - operationId: refund\n            path: /payments/{id}/refund\n"
        "      approvers: ['role:four-eyes']\n      timeout_s: 86400\n"
    ),
    (
        "apis:\n  c:\n    base_url_env: C\n    auth: none\n    allowed_methods: [POST]\n"
        "    approval: {required_for: {methods: [post]}, approvers: [requester, requester]}\n"
    ),
]

INVALID = [
    "",
    "[]\n",
    "apis: []\n",
    "apis: {}\n",
    "apis:\n",
    "product_api:\n  auth: bearer\n",
    "product_api:\n  auth: bearer\napis:\n  a:\n    base_url_env: A\n    auth: none\n"
    "    allowed_methods: [GET, POST]\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET, POST]\nextra: 1\n",
    "apis:\n  Bad-Name:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET, POST]\n",
    "apis:\n  "
    + "a" * 33
    + ":\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET, POST]\n",
    "apis:\n  a: 1\n",
    "apis:\n  a:\n    auth: none\n    allowed_methods: [GET, POST]\n",
    "apis:\n  a:\n    base_url_env: 1A\n    auth: none\n    allowed_methods: [GET, POST]\n",
    "apis:\n  a:\n    base_url_env: A\n    allowed_methods: [GET, POST]\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: forwarded-session\n    allowed_methods: [GET, POST]\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: bearer\n    allowed_methods: [GET, POST]\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    token_env: T\n    allowed_methods: [GET, POST]\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: bearer\n    token_env: T\n"
    "    forward_header: X\n    allowed_methods: [GET, POST]\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: forward\n    forward_header: 'X Y'\n"
    "    allowed_methods: [GET, POST]\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: []\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET, '*']\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [FETCH]\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: GET\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET, POST]\n"
    "    allowed_operations: []\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET, POST]\n"
    "    allowed_operations:\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET, POST]\n"
    "    allowed_operations: [getItem]\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET, POST]\n"
    "    allowed_operations:\n      - methods: [GET]\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET, POST]\n"
    "    allowed_operations:\n      - operationId: get item\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET, POST]\n"
    "    allowed_operations:\n      - path: items/1\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET, POST]\n"
    "    allowed_operations:\n      - path: /items/../admin\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET, POST]\n"
    "    allowed_operations:\n      - path: /items?x=1\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET, POST]\n"
    "    allowed_operations:\n      - path: /items/{bad-name}\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET, POST]\n"
    "    allowed_operations:\n      - path: /x\n        methods: ['*']\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET, POST]\n"
    "    allowed_operations:\n      - path: /x\n        method: [GET]\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET, POST]\n"
    "    denied_operations:\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET, POST]\n    openapi: ''\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET, POST]\n"
    "    timeouts_ms: {connect: 0, write: 5}\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET, POST]\n"
    "    timeouts_ms: {read: true}\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET, POST]\n"
    "    pagination: {page_size_param: limit}\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET, POST]\n"
    "    pagination: {page_size_param: '', max_page_size: -1, cursor: c}\n",
    # limits: optional, but each value an integer >= 1 and nothing else in it
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [POST]\n"
    "    limits: {}\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [POST]\n"
    "    limits: 20\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [POST]\n"
    "    limits: {max_calls_per_run: 0, rate_per_minute: 1.5, per_day: 3}\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [POST]\n"
    "    limits: {max_calls_per_run: true}\n",
    # approval on an operation entry: refused, pointing to the API-level block
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [POST]\n"
    "    allowed_operations:\n      - operationId: createOrder\n        approval: {by: ops}\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [DELETE]\n"
    "    denied_operations:\n      - path: /x\n        approval: false\n",
    *(
        "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [POST, DELETE]\n"
        f"    approval: {approval}\n"
        for approval in [
            # the block itself: a mapping with required_for and approvers, nothing else
            "required",
            "",
            "true",
            "[requester]",
            "{}",
            "{approvers: [requester]}",
            "{required_for: {methods: [POST]}}",
            "{required_for: {methods: [POST]}, approvers: [requester], timeout: 60}",
            "{required_for: {methods: [POST]}, approver: [requester]}",
            # required_for: methods and/or operations, each with the allow-list's rules
            "{required_for: , approvers: [requester]}",
            "{required_for: {}, approvers: [requester]}",
            "{required_for: [POST], approvers: [requester]}",
            "{required_for: {paths: [/x]}, approvers: [requester]}",
            "{required_for: {methods: [POST], path: /x}, approvers: [requester]}",
            "{required_for: {methods: []}, approvers: [requester]}",
            "{required_for: {methods: POST}, approvers: [requester]}",
            "{required_for: {methods: ['*', POST]}, approvers: [requester]}",
            "{required_for: {methods: [FETCH]}, approvers: [requester]}",
            "{required_for: {methods: [null]}, approvers: [requester]}",
            "{required_for: {operations: []}, approvers: [requester]}",
            "{required_for: {operations: }, approvers: [requester]}",
            "{required_for: {operations: [cancelOrder]}, approvers: [requester]}",
            "{required_for: {operations: [{methods: [POST]}]}, approvers: [requester]}",
            "{required_for: {operations: [{operationId: 'cancel order'}]}, approvers: [requester]}",
            "{required_for: {operations: [{path: /x/../admin}]}, approvers: [requester]}",
            "{required_for: {operations: [{path: '/x?y=1'}]}, approvers: [requester]}",
            "{required_for: {operations: [{path: /x, methods: ['*']}]}, approvers: [requester]}",
            "{required_for: {operations: [{path: /x, method: POST}]}, approvers: [requester]}",
            "{required_for: {operations: [{operationId: x, approval: {approvers: [requester]}}]},"
            " approvers: [requester]}",
            # approvers: a non-empty list of requester and role:<name>, spelled exactly
            "{required_for: {methods: [POST]}, approvers: }",
            "{required_for: {methods: [POST]}, approvers: []}",
            "{required_for: {methods: [POST]}, approvers: requester}",
            "{required_for: {methods: [POST]}, approvers: [Requester]}",
            "{required_for: {methods: [POST]}, approvers: [' requester']}",
            "{required_for: {methods: [POST]}, approvers: [admin]}",
            "{required_for: {methods: [POST]}, approvers: ['*']}",
            "{required_for: {methods: [POST]}, approvers: [anyone]}",
            "{required_for: {methods: [POST]}, approvers: ['role:']}",
            "{required_for: {methods: [POST]}, approvers: ['role: ops']}",
            "{required_for: {methods: [POST]}, approvers: ['role:a,b']}",
            "{required_for: {methods: [POST]}, approvers: ['Role:ops']}",
            '{required_for: {methods: [POST]}, approvers: ["role:ops\\n"]}',
            '{required_for: {methods: [POST]}, approvers: ["role:o\\x00ps"]}',
            "{required_for: {methods: [POST]}, approvers: ['role:" + "r" * 257 + "']}",
            "{required_for: {methods: [POST]}, approvers: [null]}",
            "{required_for: {methods: [POST]}, approvers: [{role: ops}]}",
            "{required_for: {methods: [POST]}, approvers: [[requester]]}",
            # timeout_s: an integer from 30 to 86400
            *(
                f"{{required_for: {{methods: [POST]}}, approvers: [requester], timeout_s: {t}}}"
                for t in ("29", "86401", "0", "-900", "true", "900.0", "'900'", "", "1e3")
            ),
        ]
    ),
]


def _runtime_errors(runtime: ModuleType, data: Any) -> list[str]:
    try:
        runtime.ApiPolicy.from_dict(data)
    except runtime.ApiPolicyError as exc:
        assert exc.errors, "the runtime must report the schema errors"
        return exc.errors
    return []


@pytest.mark.parametrize("document", VALID)
def test_valid_policies_are_accepted_by_both(runtime: ModuleType, document: str) -> None:
    data = yaml.safe_load(document)
    assert cli.policy_errors(data) == []
    assert _runtime_errors(runtime, data) == []


@pytest.mark.parametrize("document", INVALID)
def test_invalid_policies_are_refused_by_both_with_the_same_errors(
    runtime: ModuleType, document: str
) -> None:
    data = yaml.safe_load(document)
    cli_errors = cli.policy_errors(data)
    assert cli_errors, "the CLI must refuse this document"
    assert _runtime_errors(runtime, data) == cli_errors


def test_an_operation_level_approval_key_points_to_the_api_level_block(
    runtime: ModuleType,
) -> None:
    api = {"base_url_env": "A", "auth": "none", "allowed_methods": ["POST"]}
    pointer = (
        "not valid on an operation entry; gate the operation with "
        "apis.a.approval.required_for.operations"
    )
    entry = {"operationId": "createOrder", "approval": True}
    for key in ("allowed_operations", "denied_operations"):
        document = {"apis": {"a": {**api, key: [entry]}}}
        assert cli.policy_errors(document) == [f"apis.a.{key}[0].approval: {pointer}"]
        assert _runtime_errors(runtime, document) == cli.policy_errors(document)
    gate = {"required_for": {"operations": [entry]}, "approvers": ["requester"]}
    assert cli.policy_errors({"apis": {"a": {**api, "approval": gate}}}) == [
        f"apis.a.approval.required_for.operations[0].approval: {pointer}"
    ]


def test_approval_errors_name_the_rule() -> None:
    api = {"base_url_env": "A", "auth": "none", "allowed_methods": ["POST"]}

    def errors(approval: Any) -> list[str]:
        return cli.policy_errors({"apis": {"a": {**api, "approval": approval}}})

    assert errors("required") == [
        "apis.a.approval: must be a mapping with required_for and approvers"
    ]
    assert errors({}) == [
        "apis.a.approval.required_for: required (the methods and/or operations it gates)",
        'apis.a.approval.approvers: required (a list of "requester" and/or "role:<name>")',
    ]
    assert errors(
        {
            "required_for": {"methods": ["POST"], "paths": ["/x"]},
            "approvers": ["requester", "role:", "admin", "role:ops"],
            "timeout_s": 29,
            "timeout": 60,
        }
    ) == [
        "apis.a.approval: unknown key 'timeout'",
        "apis.a.approval.required_for: unknown key 'paths'",
        "apis.a.approval.approvers[1]: 'role:' is not an approver (\"requester\", or "
        '"role:<name>" with a role name of 1-256 characters without spaces or commas)',
        "apis.a.approval.approvers[2]: 'admin' is not an approver (\"requester\", or "
        '"role:<name>" with a role name of 1-256 characters without spaces or commas)',
        "apis.a.approval.timeout_s: must be an integer from 30 to 86400 (seconds)",
    ]
    assert errors({"required_for": {"operations": []}, "approvers": []}) == [
        "apis.a.approval.required_for.operations: must not be empty; omit the key when no "
        "operation needs approval",
        'apis.a.approval.approvers: must be a non-empty list of "requester" and/or "role:<name>"',
    ]
    assert errors({"required_for": {"timeout_s": 60}, "approvers": ["requester"]}) == [
        "apis.a.approval.required_for: unknown key 'timeout_s'",
        "apis.a.approval.required_for: needs methods and/or operations",
    ]
    assert errors({"required_for": {"methods": ["*", "POST"]}, "approvers": ["requester"]}) == [
        'apis.a.approval.required_for.methods: "*" must be the only entry when present'
    ]


def test_limits_errors_name_the_rule() -> None:
    api = {"base_url_env": "A", "auth": "none", "allowed_methods": ["PUT"]}
    errors = cli.policy_errors(
        {"apis": {"a": {**api, "limits": {"max_calls_per_run": 0, "per_day": 3}}}}
    )
    assert errors == [
        "apis.a.limits: unknown key 'per_day'",
        "apis.a.limits.max_calls_per_run: must be an integer >= 1",
    ]


def test_files_are_judged_the_same_by_create_and_the_runtime(
    runtime: ModuleType, tmp_path: Path
) -> None:
    for index, document in enumerate([*VALID, *INVALID]):
        path = tmp_path / f"policy-{index}.yaml"
        path.write_text(document, encoding="utf-8")
        try:
            cli.load_policy_document(path)
            cli_errors: list[str] = []
        except cli.ApiPolicyFileError as exc:
            cli_errors = exc.errors
        try:
            runtime.ApiPolicy.load(path)
            runtime_errors: list[str] = []
        except runtime.ApiPolicyError as exc:
            runtime_errors = exc.errors
        assert runtime_errors == cli_errors, document


POLICY = yaml.safe_load(
    "apis:\n"
    "  a:\n"
    "    base_url_env: A\n"
    "    auth: none\n"
    "    allowed_methods: [GET, POST]\n"
    "    allowed_operations:\n"
    "      - operationId: getItem\n"
    "        path: /items/{item_id}\n"
    "      - path: /search\n"
    "        methods: [POST]\n"
    "      - operationId: listThings\n"
    "    denied_operations:\n"
    "      - path: /items/admin\n"
)

CALLS = [
    ("GET", "getItem", "/items/{item_id}"),
    ("GET", "getItem", "/items/42"),
    ("GET", "getItem", "/items/admin"),
    ("GET", "getItem", "/admin"),
    ("GET", "getItem", None),
    ("GET", None, "/items/42"),
    ("POST", None, "/search"),
    ("GET", None, "/search"),
    ("DELETE", "getItem", "/items/1"),
    ("GET", "listThings", None),
    ("GET", "listThings", "/anything"),
    ("post", "listThings", None),
]


@pytest.mark.parametrize(("method", "operation_id", "path"), CALLS)
def test_calls_are_allowed_and_refused_the_same_way(
    runtime: ModuleType, method: str, operation_id: str | None, path: str | None
) -> None:
    reason = cli.refusal_reason(POLICY["apis"]["a"], method, operation_id, path)
    policy = runtime.ApiPolicy.from_dict(POLICY)
    if reason is None:
        policy.check("a", method, operation_id, path)
    else:
        with pytest.raises(runtime.ApiPolicyError) as exc:
            policy.check("a", method, operation_id, path)
        assert reason in str(exc.value)


def test_the_bundled_sample_policy_is_valid() -> None:
    sample = TEMPLATE_CLIENT.parents[2] / "api-policy.yaml"
    env = Environment(undefined=StrictUndefined)
    text = env.from_string(sample.read_text(encoding="utf-8")).render(
        cookiecutter={"project_name": "p", "agent_directory": "app"}
    )
    data = yaml.safe_load(text)
    assert cli.policy_errors(data) == []
    assert list(data["apis"]) == ["orders"]
    # A sample, not a default: read-write methods with an explicit allow-list and a denial.
    orders = data["apis"]["orders"]
    assert orders["allowed_methods"] != ["GET"]
    assert orders["allowed_operations"] and orders["denied_operations"]


# --- repeated keys ------------------------------------------------------------------

DUPLICATE_KEYS = [
    # The narrower rule is read first; plain safe_load would apply the later "*".
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET, POST]\n"
    "    allowed_methods: ['*']\n",
    # A second definition of the same API would silently drop the first one's denial.
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET, POST]\n"
    "    denied_operations:\n      - path: /admin\n"
    "  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET, POST]\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET, POST]\n"
    "apis:\n  b:\n    base_url_env: B\n    auth: none\n    allowed_methods: ['*']\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET, POST]\n"
    "    allowed_operations:\n      - path: /items/{id}\n        path: /admin\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET, POST]\n"
    "    pagination: {page_size_param: limit, max_page_size: 5, max_page_size: 5000}\n",
    # A later approvers list would silently widen who may approve...
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET, POST]\n"
    "    approval:\n      required_for: {methods: [POST]}\n"
    "      approvers: ['role:ops']\n      approvers: [requester]\n",
    # ...and a later required_for would silently drop a gate.
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET, POST]\n"
    "    approval:\n      required_for: {methods: [POST], methods: [GET]}\n"
    "      approvers: [requester]\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET, POST]\n"
    "    approval: {required_for: {methods: [POST]}, approvers: [requester]}\n"
    "    approval: {required_for: {methods: [GET]}, approvers: [requester]}\n",
]


@pytest.mark.parametrize("document", DUPLICATE_KEYS)
def test_repeated_keys_are_refused_by_both_with_the_same_error(
    runtime: ModuleType, tmp_path: Path, document: str
) -> None:
    assert yaml.safe_load(document)  # plain YAML would accept it, keeping the last value
    assert cli.parse_policy_yaml(document) == runtime.parse_policy_yaml(document)
    data, errors = cli.parse_policy_yaml(document)
    assert data is None and "found duplicate key" in errors[0]

    path = tmp_path / "api-policy.yaml"
    path.write_text(document, encoding="utf-8")
    with pytest.raises(cli.ApiPolicyFileError) as cli_exc:
        cli.load_policy_document(path)
    with pytest.raises(runtime.ApiPolicyError) as runtime_exc:
        runtime.ApiPolicy.load(path)
    assert runtime_exc.value.errors == cli_exc.value.errors == errors


def test_merge_keys_are_not_repeated_keys(runtime: ModuleType) -> None:
    document = (
        "apis:\n  a: &base\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET, POST]\n"
        "  b:\n    <<: *base\n    base_url_env: B\n"
    )
    for side in (cli, runtime):
        data, errors = side.parse_policy_yaml(document)
        assert errors == []
        assert data["apis"]["b"] == {
            "base_url_env": "B",
            "auth": "none",
            "allowed_methods": ["GET", "POST"],
        }
        assert side.policy_errors(data) == []


# --- denials fail closed, paths are compared normalised -------------------------------

DENIALS = yaml.safe_load(
    "apis:\n"
    "  d:\n"
    "    base_url_env: D\n"
    "    auth: none\n"
    "    allowed_methods: ['*']\n"
    "    denied_operations:\n"
    "      - operationId: deleteOrder\n"
    "        path: /orders/{order_id}\n"
    "        methods: [DELETE]\n"
    "      - operationId: deleteItem\n"
    "        methods: [DELETE]\n"
    "      - path: /admin/{section}\n"
    "      - operationId: purge\n"
    "        path: /items/{id}/purge\n"
)

DENIAL_CALLS = [
    # (method, operation_id, path, refused, unnamed field named in the reason)
    ("DELETE", "deleteItem", "/items/1", True, None),
    ("DELETE", "DeleteItem", "/items/1", True, None),
    ("DELETE", None, "/items/1", True, "operation_id"),
    ("DELETE", "", "/items/1", True, "operation_id"),
    # A denial by operationId alone knows only that label.
    ("DELETE", "archiveItem", "/items/1", False, None),
    ("GET", None, "/items/1", False, None),
    ("GET", "getAdmin", None, True, "path"),
    ("GET", "x", "/admin/1", True, None),
    ("GET", "x", "/admin/1/", True, None),
    ("GET", "x", "/ADMIN/1", True, None),
    ("GET", "x", "/%61dmin/1", True, None),
    ("GET", "x", "/%41DMIN/1", True, None),
    ("GET", "x", "/admin/{section}", True, None),
    ("GET", "x", "/administrator/1", False, None),
    # A denial pinning a path holds on the wire: the call's label does not matter...
    ("POST", None, "/items/1/purge", True, None),
    ("POST", "purge", "/items/1/purge", True, None),
    ("POST", "other", "/items/1/purge", True, None),
    # ...and its operationId is one more way to match, not a requirement.
    ("POST", "purge", "/items/1", True, None),
    ("POST", "other", "/items/1", False, None),
    ("POST", "other", None, True, "path"),
    # The auditor's case: `api deny orders deleteOrder` with a spec, relabelled calls.
    ("DELETE", "deleteOrder", "/orders/1", True, None),
    ("DELETE", "cancelOrder", "/orders/1", True, None),
    ("DELETE", "deleteOrdr", "/orders/{order_id}", True, None),
    ("DELETE", None, "/orders/1", True, None),
    ("DELETE", "cancelOrder", "/ORDERS/1/", True, None),
    ("DELETE", "cancelOrder", None, True, "path"),
    # A denial pinning a path does not refuse an unnamed call to another path.
    ("POST", None, "/carts/1", False, None),
    ("GET", "getOrder", "/orders/1", False, None),
    ("GET", "deleteOrder", "/orders/1", False, None),
]


@pytest.mark.parametrize(("method", "operation_id", "path", "refused", "unnamed"), DENIAL_CALLS)
def test_denials_fail_closed_the_same_way(
    runtime: ModuleType,
    method: str,
    operation_id: str | None,
    path: str | None,
    refused: bool,
    unnamed: str | None,
) -> None:
    api = DENIALS["apis"]["d"]
    reason = cli.refusal_reason(api, method, operation_id, path)
    assert reason == runtime.refusal_reason(api, method, operation_id, path)
    assert (reason is not None) is refused, reason
    if unnamed:
        assert f"the call names no {unnamed}" in reason
    elif reason:
        assert "the call names no" not in reason


@pytest.mark.parametrize(
    ("template", "path", "ignore_case", "expected"),
    [
        ("/items/{id}", "/items/1/", False, True),
        ("/items/{id}/", "/items/1", False, True),
        ("/items", "/items/", False, True),
        ("/", "/", False, True),
        ("/items/{id}", "/%69tems/1", False, True),
        # An encoded slash stays encoded: one segment here (the client refuses it separately).
        ("/items/{id}", "/items/a%2fb", False, True),
        ("/items/a%2fb", "/items/a%2Fb", False, True),
        ("/items/{id}", "/ITEMS/1", False, False),
        ("/items/{id}", "/ITEMS/1", True, True),
        ("/items/{id}", "/items//", False, False),
    ],
)
def test_paths_are_compared_normalised_by_both(
    runtime: ModuleType, template: str, path: str, ignore_case: bool, expected: bool
) -> None:
    assert cli.path_matches(template, path, ignore_case=ignore_case) is expected
    assert runtime.path_matches(template, path, ignore_case=ignore_case) is expected


def test_allows_still_need_the_named_fields(runtime: ModuleType) -> None:
    """The fail-closed rule is for denials: an allow must be shown, so an unnamed field fails it."""
    api = {
        "allowed_methods": ["GET"],
        "allowed_operations": [{"operationId": "getItem"}, {"path": "/items/{id}"}],
    }
    for side in (cli, runtime):
        assert side.refusal_reason(api, "GET", None, "/other") == "not in allowed_operations"
        assert side.refusal_reason(api, "GET", "getItem", "/other") is None
        assert side.refusal_reason(api, "GET", None, "/items/1/") is None
        assert side.refusal_reason(api, "GET", None, "/ITEMS/1") == "not in allowed_operations"


# --- approval gates: the same calls wait for a human on both sides ----------------------

GATES = yaml.safe_load(
    "apis:\n"
    "  g:\n"
    "    base_url_env: G\n"
    "    auth: none\n"
    "    allowed_methods: ['*']\n"
    "    approval:\n"
    "      required_for:\n"
    "        methods: [DELETE]\n"
    "        operations:\n"
    "          - operationId: cancelOrder\n"
    "            path: /orders/{order_id}/cancel\n"
    "            methods: [POST]\n"
    "          - operationId: refund\n"
    "          - path: /admin/{section}\n"
    "      approvers: [requester, 'role:ops']\n"
    "      timeout_s: 120\n"
    "  every:\n"
    "    base_url_env: E\n"
    "    auth: none\n"
    "    allowed_methods: [GET, POST]\n"
    "    approval: {required_for: {methods: ['*']}, approvers: ['role:four-eyes']}\n"
    "  open:\n"
    "    base_url_env: O\n"
    "    auth: none\n"
    "    allowed_methods: [GET, POST]\n"
)

GATE_CALLS = [
    # (method, operation_id, path, gated, a fragment of the rule that gates it)
    ("DELETE", "deleteItem", "/items/1", True, "approval.required_for.methods ['DELETE']"),
    ("delete", None, "/items/1", True, "approval.required_for.methods ['DELETE']"),
    ("GET", "getOrder", "/orders/1", False, None),
    ("POST", "cancelOrder", "/orders/1/cancel", True, "operations (operationId=cancelOrder"),
    # A path gate holds on the wire, whatever the call is labelled or however it is spelled...
    ("POST", "archiveOrder", "/orders/1/cancel", True, "operationId=cancelOrder"),
    ("POST", None, "/orders/1/cancel", True, "operationId=cancelOrder"),
    ("POST", "x", "/ORDERS/1/CANCEL/", True, "operationId=cancelOrder"),
    ("POST", "x", "/%6Frders/1/cancel", True, "operationId=cancelOrder"),
    ("POST", "x", "/orders/{order_id}/cancel", True, "operationId=cancelOrder"),
    # ...for the methods it pins.
    ("GET", "cancelOrder", "/orders/1/cancel", False, None),
    ("POST", "x", "/orders/1/cancelled", False, None),
    # Its label gates too, and a call that leaves out what the gate knows it by is gated.
    ("POST", "cancelOrder", "/elsewhere", True, "operationId=cancelOrder"),
    ("POST", "cancelOrder", None, True, "operationId=cancelOrder"),
    ("POST", "archiveOrder", None, True, "the call names no path, so it cannot be ruled out"),
    ("POST", "refund", "/payments/1/refund", True, "operationId=refund"),
    ("POST", "Refund", "/payments/1", True, "operationId=refund"),
    ("POST", None, "/payments/1", True, "names no operation_id, so it cannot be ruled out"),
    ("PUT", None, "/items/1", True, "names no operation_id, so it cannot be ruled out"),
    ("POST", "createPayment", "/payments", False, None),
    ("GET", "x", "/admin/users", True, "path=/admin/{section}"),
    ("GET", "x", "/Admin/users/", True, "path=/admin/{section}"),
    ("GET", "x", "/administrator/users", False, None),
    ("GET", "x", "/admin", False, None),
]


def _gate_fields(gate: Any) -> tuple[Any, ...] | None:
    return None if gate is None else (gate.approvers, gate.timeout_s, gate.rule)


@pytest.mark.parametrize(("method", "operation_id", "path", "is_gated", "fragment"), GATE_CALLS)
def test_gates_are_found_the_same_way(
    runtime: ModuleType,
    method: str,
    operation_id: str | None,
    path: str | None,
    is_gated: bool,
    fragment: str | None,
) -> None:
    api = GATES["apis"]["g"]
    gate = cli.gated(api, method, operation_id, path)
    assert _gate_fields(gate) == _gate_fields(runtime.gated(api, method, operation_id, path))
    policy = runtime.ApiPolicy.from_dict(GATES)
    assert _gate_fields(gate) == _gate_fields(policy.gate("g", method, operation_id, path))
    assert (gate is not None) is is_gated, gate
    if gate is not None:
        assert gate.approvers == ("requester", "role:ops")
        assert gate.timeout_s == 120
        assert fragment in gate.rule
    # Gated or not, the policy allows every one of these calls.
    assert cli.refusal_reason(api, method, operation_id, path) is None


def test_gate_defaults_and_apis_without_a_gate(runtime: ModuleType) -> None:
    for side in (cli, runtime):
        every = side.gated(GATES["apis"]["every"], "get", None, "/anything")
        assert _gate_fields(every) == (
            ("role:four-eyes",),
            side.DEFAULT_APPROVAL_TIMEOUT_S,
            "approval.required_for.methods ['*']",
        )
        assert side.DEFAULT_APPROVAL_TIMEOUT_S == 900
        assert side.gated(GATES["apis"]["open"], "POST", "createOrder", "/orders") is None
        assert side.gated(POLICY["apis"]["a"], "POST", None, "/search") is None


GATE_EVERYTHING = {
    "required_for": {
        "methods": ["*"],
        "operations": [{"path": "/{a}"}, {"operationId": "anything"}],
    },
    "approvers": ["requester", "role:anyone"],
    "timeout_s": 86400,
}


@pytest.mark.parametrize(
    ("api", "calls"),
    [
        (POLICY["apis"]["a"], [c[:3] for c in CALLS]),
        (DENIALS["apis"]["d"], [c[:3] for c in DENIAL_CALLS]),
        (GATES["apis"]["open"], [("DELETE", "x", "/x"), ("PUT", None, "/y"), ("GET", "z", None)]),
    ],
)
def test_approval_never_widens_access(
    runtime: ModuleType, api: dict[str, Any], calls: list[tuple[Any, ...]]
) -> None:
    """A gate on every call changes no allow or refusal, on either side: denials still win."""
    with_gate = {**api, "approval": GATE_EVERYTHING}
    assert cli.policy_errors({"apis": {"x": with_gate}}) == []
    policy = runtime.ApiPolicy.from_dict({"apis": {"x": with_gate}})
    refused = 0
    for method, operation_id, path in calls:
        reason = cli.refusal_reason(api, method, operation_id, path)
        assert cli.refusal_reason(with_gate, method, operation_id, path) == reason
        assert runtime.refusal_reason(with_gate, method, operation_id, path) == reason
        if reason is None:
            policy.check("x", method, operation_id, path)
        else:
            refused += 1
            with pytest.raises(runtime.ApiPolicyError) as exc:
                policy.check("x", method, operation_id, path)
            assert reason in str(exc.value) and "approval" not in str(exc.value)
        # The gate itself is there for every call, allowed or not.
        assert cli.gated(with_gate, method, operation_id, path) is not None
    assert refused, "each case refuses some call"


def test_the_runtime_client_refuses_a_gated_call_before_sending(
    runtime: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outside an agent run nothing can pause for a decision: a gated call fails closed.

    Inside a run the client pauses it for approval instead; the template's own
    tests (`tests/unit/test_approval_ledger.py`, `tests/integration/test_approvals.py`)
    cover the pause, the decision and the resume.
    """
    import asyncio

    import httpx

    sent: list[httpx.Request] = []
    transport = httpx.MockTransport(
        lambda request: sent.append(request) or httpx.Response(200, json={"ok": True})
    )
    monkeypatch.setenv("G", "http://g.test")
    monkeypatch.setenv("O", "http://o.test")
    policy = runtime.ApiPolicy.from_dict(GATES)
    gated_client = runtime.ApiClient(policy, "g", transport=transport)
    open_client = runtime.ApiClient(policy, "open", transport=transport)

    async def calls() -> None:
        with pytest.raises(runtime.ApiPolicyError) as exc:
            await gated_client.post(
                "/orders/{order_id}/cancel",
                operation_id="cancelOrder",
                path_params={"order_id": "7"},
                json_body={"reason": "asked"},
            )
        assert "needs human approval (requester, role:ops)" in str(exc.value)
        assert "nothing was sent" in str(exc.value)
        assert exc.value.reason.startswith("approval required by approval.required_for")
        # Gated by the template although the rendered path is spelled otherwise.
        with pytest.raises(runtime.ApiPolicyError, match="needs human approval"):
            await gated_client.delete("/items/{item_id}", path_params={"item_id": "1"})
        assert await gated_client.get("/items/1", operation_id="getItem") == {"ok": True}
        assert await open_client.post("/orders", operation_id="createOrder") == {"ok": True}

    asyncio.run(calls())
    assert [(r.method, r.url.path) for r in sent] == [("GET", "/items/1"), ("POST", "/orders")]
