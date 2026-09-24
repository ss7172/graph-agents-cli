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
documents with the same error text, and allow and refuse the same calls.
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
    # approval: reserved for a later release, refused (never silently accepted)
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [POST]\n"
    "    approval: required\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [POST]\n"
    "    allowed_operations:\n      - operationId: createOrder\n        approval: {by: ops}\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [DELETE]\n"
    "    denied_operations:\n      - path: /x\n        approval: false\n",
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


def test_the_approval_key_is_refused_with_the_planned_message() -> None:
    api = {"base_url_env": "A", "auth": "none", "allowed_methods": ["POST"]}
    reserved = "approval gates are not supported yet (planned); remove the approval key"
    assert cli.policy_errors({"apis": {"a": {**api, "approval": "required"}}}) == [
        f"apis.a.approval: {reserved}"
    ]
    entry = {"operationId": "createOrder", "approval": True}
    assert cli.policy_errors({"apis": {"a": {**api, "allowed_operations": [entry]}}}) == [
        f"apis.a.allowed_operations[0].approval: {reserved}"
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
