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

"""`auth: exchange` (RFC 8693) and `forward_audience` in api-policy.yaml, as the CLI checks them.

The schema lives in the SHARED block, so the runtime copy (the template's
`api_client.py`) must answer every case with the same errors; the secrets
and the runtime check are CLI-side (`lint`, `api add`, `create`).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from graph_agents_cli import _api_policy as cli
from graph_agents_cli._api_policy import (
    forward_runtime_problem,
    policy_errors,
    secret_envs,
    summarize,
)

TEMPLATE_CLIENT = (
    Path(cli.__file__).parent / "scaffold/agents/langgraph/app/app_utils/api_client.py"
)


def _runtime_errors(document: Any) -> list[str]:
    """The same document through the runtime copy of the SHARED block."""
    spec = importlib.util.spec_from_file_location("_runtime_api_client", TEMPLATE_CLIENT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return module.policy_errors(document)
    finally:
        sys.modules.pop(spec.name, None)


def _doc(**api: Any) -> dict[str, Any]:
    return {"apis": {"peer": {"base_url_env": "PEER_URL", "allowed_methods": ["GET"], **api}}}


VALID = [
    _doc(auth="exchange", exchange={"audience": "orders"}),
    _doc(
        auth="exchange",
        exchange={
            "audience": "orders",
            "scope": "orders.read orders:cancel",
            "resource": "https://orders.example.com/api",
        },
        forward_header="X-Agent-Token",
    ),
    _doc(auth="exchange", exchange={"audience": "urn:example:orders", "resource": "urn:x:y"}),
    _doc(auth="forward", forward_audience="orders"),
    _doc(auth="forward", forward_audience="orders", forward_header="X-User-Token"),
]

INVALID = [
    (
        _doc(auth="exchange"),
        "apis.peer.exchange: required when auth is exchange (a mapping with the audience the "
        "issuer mints the token for, and optionally scope and resource)",
    ),
    (
        _doc(auth="bearer", token_env="T", exchange={"audience": "x"}),
        "apis.peer.exchange: only valid with auth: exchange",
    ),
    (
        _doc(auth="exchange", exchange="orders"),
        "apis.peer.exchange: must be a mapping with audience, and optionally scope and resource",
    ),
    (
        _doc(auth="exchange", exchange={}),
        "apis.peer.exchange.audience: required (the audience the issuer mints the token for: the target's AUTH_JWT_AUDIENCE)",
    ),
    (
        _doc(auth="exchange", exchange={"audience": ""}),
        "apis.peer.exchange.audience: must be an audience (1-256 characters without spaces, commas or control characters)",
    ),
    (
        _doc(auth="exchange", exchange={"audience": "a,b"}),
        "apis.peer.exchange.audience: must be an audience",
    ),
    (
        _doc(auth="exchange", exchange={"audience": "x", "audiences": ["y"]}),
        "apis.peer.exchange: unknown key 'audiences'",
    ),
    (
        _doc(auth="exchange", exchange={"audience": "x", "scope": ""}),
        "apis.peer.exchange.scope: must be scopes separated by single spaces",
    ),
    (
        _doc(auth="exchange", exchange={"audience": "x", "scope": "a  b"}),
        "apis.peer.exchange.scope: must be scopes",
    ),
    (
        _doc(auth="exchange", exchange={"audience": "x", "scope": 'a "b"'}),
        "apis.peer.exchange.scope: must be scopes",
    ),
    (
        _doc(auth="exchange", exchange={"audience": "x", "resource": "/relative"}),
        "apis.peer.exchange.resource: must be an absolute URI without a fragment (RFC 8707)",
    ),
    (
        _doc(auth="exchange", exchange={"audience": "x", "resource": "https://a/b#frag"}),
        "apis.peer.exchange.resource: must be an absolute URI",
    ),
    (
        _doc(auth="bearer", token_env="T", forward_audience="x"),
        "apis.peer.forward_audience: only valid with auth: forward",
    ),
    (
        _doc(auth="exchange", exchange={"audience": "x"}, forward_audience="x"),
        "apis.peer.forward_audience: only valid with auth: forward",
    ),
    (
        _doc(auth="forward", forward_audience="has space"),
        "apis.peer.forward_audience: must be an audience",
    ),
    (
        _doc(auth="bearer", token_env="T", forward_header="X-A"),
        "apis.peer.forward_header: only valid with auth: forward or exchange",
    ),
    (
        _doc(auth="token"),
        "apis.peer.auth: must be one of none, bearer, forward, exchange (got 'token')",
    ),
]


@pytest.mark.parametrize("document", VALID)
def test_valid_exchange_and_forward_audience(document: dict[str, Any]) -> None:
    assert policy_errors(document) == []
    assert _runtime_errors(document) == []


@pytest.mark.parametrize(("document", "error"), INVALID)
def test_every_error_is_the_same_in_both_copies(document: dict[str, Any], error: str) -> None:
    errors = policy_errors(document)
    assert any(e.startswith(error) for e in errors), errors
    assert _runtime_errors(document) == errors


def test_auth_is_required_naming_every_mode() -> None:
    errors = policy_errors({"apis": {"peer": {"base_url_env": "U", "allowed_methods": ["GET"]}}})
    assert "apis.peer.auth: required (one of none, bearer, forward, exchange)" in errors


# ---------------------------------------------------------------------------
# Secrets and the runtime (CLI side)
# ---------------------------------------------------------------------------

EXCHANGE = {
    "base_url_env": "ORDERS_AGENT_URL",
    "auth": "exchange",
    "exchange": {"audience": "orders"},
    "allowed_methods": ["GET"],
}
FORWARD = {"base_url_env": "ME_URL", "auth": "forward", "allowed_methods": ["GET"]}
AIMED = {**FORWARD, "forward_audience": "me"}
BEARER = {"base_url_env": "B", "auth": "bearer", "token_env": "B_TOKEN", "allowed_methods": ["GET"]}


def test_the_exchange_client_secret_joins_the_secrets_once() -> None:
    document = {"apis": {"b": BEARER, "o": EXCHANGE, "p": {**EXCHANGE, "base_url_env": "P"}}}
    assert secret_envs(summarize(document)) == ["B_TOKEN", "TOKEN_EXCHANGE_CLIENT_SECRET"]
    assert secret_envs(summarize({"apis": {"b": BEARER}})) == ["B_TOKEN"]


def test_exchange_is_refused_under_langgraph_server() -> None:
    both = summarize({"apis": {"o": EXCHANGE, "me": FORWARD}})
    problem = forward_runtime_problem(both, "langgraph-server")
    assert problem is not None
    assert problem.startswith("auth: forward and auth: exchange (apis: o, me) is not supported")
    assert "persists the run context" in problem
    only_forward = forward_runtime_problem(summarize({"apis": {"me": FORWARD}}), "langgraph-server")
    assert only_forward is not None and "forwarded credentials would be stored" in only_forward
    assert forward_runtime_problem(both, "fastapi") is None
