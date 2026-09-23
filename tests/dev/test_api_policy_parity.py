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
    "apis:\n  a:\n    base_url_env: A_URL\n    auth: none\n    allowed_methods: [GET]\n",
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
]

INVALID = [
    "",
    "[]\n",
    "apis: []\n",
    "apis: {}\n",
    "apis:\n",
    "product_api:\n  auth: bearer\n",
    "product_api:\n  auth: bearer\napis:\n  a:\n    base_url_env: A\n    auth: none\n"
    "    allowed_methods: [GET]\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET]\nextra: 1\n",
    "apis:\n  Bad-Name:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET]\n",
    "apis:\n  " + "a" * 33 + ":\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET]\n",
    "apis:\n  a: 1\n",
    "apis:\n  a:\n    auth: none\n    allowed_methods: [GET]\n",
    "apis:\n  a:\n    base_url_env: 1A\n    auth: none\n    allowed_methods: [GET]\n",
    "apis:\n  a:\n    base_url_env: A\n    allowed_methods: [GET]\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: forwarded-session\n    allowed_methods: [GET]\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: bearer\n    allowed_methods: [GET]\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    token_env: T\n    allowed_methods: [GET]\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: bearer\n    token_env: T\n"
    "    forward_header: X\n    allowed_methods: [GET]\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: forward\n    forward_header: 'X Y'\n"
    "    allowed_methods: [GET]\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: []\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET, '*']\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [FETCH]\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: GET\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET]\n"
    "    allowed_operations: []\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET]\n"
    "    allowed_operations:\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET]\n"
    "    allowed_operations: [getItem]\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET]\n"
    "    allowed_operations:\n      - methods: [GET]\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET]\n"
    "    allowed_operations:\n      - operationId: get item\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET]\n"
    "    allowed_operations:\n      - path: items/1\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET]\n"
    "    allowed_operations:\n      - path: /items/../admin\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET]\n"
    "    allowed_operations:\n      - path: /items?x=1\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET]\n"
    "    allowed_operations:\n      - path: /items/{bad-name}\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET]\n"
    "    allowed_operations:\n      - path: /x\n        methods: ['*']\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET]\n"
    "    allowed_operations:\n      - path: /x\n        method: [GET]\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET]\n"
    "    denied_operations:\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET]\n    openapi: ''\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET]\n"
    "    timeouts_ms: {connect: 0, write: 5}\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET]\n"
    "    timeouts_ms: {read: true}\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET]\n"
    "    pagination: {page_size_param: limit}\n",
    "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [GET]\n"
    "    pagination: {page_size_param: '', max_page_size: -1, cursor: c}\n",
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
    assert list(data["apis"]) == ["example"]
