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

"""Unit tests for the auth policy and the product client's policy enforcement.

No network, no model key: the product client is exercised through an httpx
MockTransport and refusals happen before anything is sent.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from {{cookiecutter.agent_directory}}.app_utils import auth as auth_module
from {{cookiecutter.agent_directory}}.app_utils.auth import (
    ACTIONS,
    Principal,
    SharedBearerPolicy,
    authenticate_and_authorize,
    get_policy,
    require,
    reset_policy_cache,
)
from {{cookiecutter.agent_directory}}.app_utils.product_client import (
    PolicyViolation,
    ProductAPIError,
    ProductClient,
    ProductPolicy,
    check_tool_declarations,
    load_policy,
    render_path,
)
from {{cookiecutter.agent_directory}}.policies import build_policy
from {{cookiecutter.agent_directory}}.policies.product_session import NOT_IMPLEMENTED, ProductSessionPolicy


def _request(headers: dict[str, str] | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request(
        {"type": "http", "method": "POST", "path": "/chat", "headers": raw, "query_string": b""}
    )


# --- SharedBearerPolicy -------------------------------------------------------


async def test_shared_bearer_accepts_the_configured_key() -> None:
    policy = SharedBearerPolicy(api_key="s3cret")
    principal = await policy.authenticate(_request({"Authorization": "Bearer s3cret"}))
    assert principal.id == "shared"
    assert principal.permissions == set(ACTIONS)
    assert len(principal.hashed_id()) == 16
    for action in ACTIONS:
        await policy.authorize(principal, action, None)


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": "Basic s3cret"},
        {"Authorization": "Bearer"},
    ],
)
async def test_shared_bearer_rejects_missing_or_wrong_key(headers: dict[str, str]) -> None:
    policy = SharedBearerPolicy(api_key="s3cret")
    with pytest.raises(HTTPException) as exc:
        await policy.authenticate(_request(headers))
    assert exc.value.status_code == 401
    assert exc.value.headers == {"WWW-Authenticate": "Bearer"}


async def test_shared_bearer_fails_closed_without_a_configured_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("API_KEY", raising=False)
    policy = SharedBearerPolicy()
    with pytest.raises(HTTPException) as exc:
        await policy.authenticate(_request({"Authorization": "Bearer anything"}))
    assert exc.value.status_code == 503


async def test_shared_bearer_reads_api_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEY", "from-env")
    principal = await SharedBearerPolicy().authenticate(
        _request({"Authorization": "Bearer from-env"})
    )
    assert principal.id == "shared"


async def test_unknown_action_is_forbidden() -> None:
    policy = SharedBearerPolicy(api_key="k")
    with pytest.raises(HTTPException) as exc:
        await policy.authorize(Principal(id="shared"), "thread.export", None)
    assert exc.value.status_code == 403


# --- ProductSessionPolicy stub: fails closed until implemented ------------------


@pytest.fixture
def product_session(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AUTH_POLICY", "product-session")
    reset_policy_cache()
    yield
    reset_policy_cache()


async def test_product_session_stub_fails_closed_on_every_action(product_session) -> None:
    assert isinstance(build_policy("product-session"), ProductSessionPolicy)
    assert isinstance(get_policy(), ProductSessionPolicy)
    with pytest.raises(HTTPException) as exc:
        await require("chat.send")(_request({"Authorization": "Bearer test-key"}))
    assert exc.value.status_code == 503 and exc.value.detail == NOT_IMPLEMENTED
    for action in ACTIONS:
        with pytest.raises(HTTPException) as exc:
            await authenticate_and_authorize(_request(), action)
        assert exc.value.status_code == 503
    # A stub that only failed authenticate would still be caught here.
    with pytest.raises(HTTPException) as exc:
        await ProductSessionPolicy().authorize(Principal(id="u1"), "chat.send", None)
    assert exc.value.status_code == 503


def test_unknown_policy_name_is_an_error() -> None:
    with pytest.raises(RuntimeError, match="Unknown AUTH_POLICY"):
        build_policy("bogus")


async def test_not_implemented_error_from_a_policy_maps_to_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert auth_module._as_http_exception(NotImplementedError("x")).status_code == 503
    assert auth_module._as_http_exception(NotImplementedError()).detail.startswith("The configured")
    kept = HTTPException(status_code=401)
    assert auth_module._as_http_exception(kept) is kept
    with pytest.raises(ValueError):
        auth_module._as_http_exception(ValueError("not mapped"))

    class _Unimplemented:
        async def authenticate(self, request: Request) -> Principal:
            raise NotImplementedError("consumer policy pending")

        async def authorize(self, principal: Principal, action: str, resource: str | None) -> None:
            raise NotImplementedError

    monkeypatch.setattr(auth_module, "get_policy", lambda: _Unimplemented())
    with pytest.raises(HTTPException) as exc:
        await require("chat.send")(_request())
    assert exc.value.status_code == 503 and "pending" in str(exc.value.detail)
    with pytest.raises(HTTPException) as exc:
        await authenticate_and_authorize(_request(), "card.read")
    assert exc.value.status_code == 503


# --- Product client and policy --------------------------------------------------

POLICY_DOC = {
    "product_api": {
        "base_url_env": "PRODUCT_API_BASE_URL",
        "auth": "bearer",
        "token_env": "PRODUCT_API_TOKEN",
        "allowed_methods": ["GET"],
        "allowed_operations": [
            {"operationId": "getItem"},
            {"path": "/sites/{siteId}/topology", "methods": ["GET"]},
        ],
        "denied_operations": [{"operationId": "getSecret"}],
    }
}


def _policy(tmp_path: Path) -> ProductPolicy:
    import yaml

    p = tmp_path / "product-policy.yaml"
    p.write_text(yaml.safe_dump(POLICY_DOC), encoding="utf-8")
    return ProductPolicy.load(p)


def _transport(calls: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True, "path": request.url.path})

    return httpx.MockTransport(handler)


async def test_refuses_disallowed_method_before_sending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRODUCT_API_BASE_URL", "http://product.test")
    monkeypatch.setenv("PRODUCT_API_TOKEN", "tok")
    calls: list[httpx.Request] = []
    client = ProductClient(_policy(tmp_path), transport=_transport(calls))
    with pytest.raises(PolicyViolation, match="allows only"):
        await client.request("POST", operation_id="getItem", path="/items/1", json_body={"x": 1})
    assert calls == []


async def test_refuses_operation_outside_allow_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRODUCT_API_BASE_URL", "http://product.test")
    monkeypatch.setenv("PRODUCT_API_TOKEN", "tok")
    calls: list[httpx.Request] = []
    client = ProductClient(_policy(tmp_path), transport=_transport(calls))
    with pytest.raises(PolicyViolation, match="not in the allow-list"):
        await client.request("GET", operation_id="listUsers", path="/users")
    with pytest.raises(PolicyViolation, match="denied"):
        await client.request("GET", operation_id="getSecret", path="/secret")
    assert calls == []


async def test_allowed_call_is_sent_with_bearer_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRODUCT_API_BASE_URL", "http://product.test/")
    monkeypatch.setenv("PRODUCT_API_TOKEN", "tok")
    calls: list[httpx.Request] = []
    client = ProductClient(_policy(tmp_path), transport=_transport(calls))
    data = await client.request("GET", operation_id="getItem", path="/items/42")
    assert data == {"ok": True, "path": "/items/42"}
    by_path = await client.request("GET", path="/sites/7/topology")
    assert by_path["path"] == "/sites/7/topology"
    assert [c.headers["authorization"] for c in calls] == ["Bearer tok", "Bearer tok"]
    assert str(calls[0].url) == "http://product.test/items/42"


@pytest.mark.parametrize(
    "item_id", ["1/../../admin", "../admin", "..", ".", "1/extra", "a/b", "a\\b", "", " 1"]
)
async def test_path_params_refuse_traversal_before_sending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, item_id: str
) -> None:
    monkeypatch.setenv("PRODUCT_API_BASE_URL", "http://product.test")
    monkeypatch.setenv("PRODUCT_API_TOKEN", "tok")
    calls: list[httpx.Request] = []
    client = ProductClient(_policy(tmp_path), transport=_transport(calls))
    with pytest.raises(PolicyViolation):
        await client.request(
            "GET", operation_id="getItem", path="/items/{item_id}", path_params={"item_id": item_id}
        )
    assert calls == []


async def test_path_params_are_sent_as_one_encoded_segment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRODUCT_API_BASE_URL", "http://product.test")
    monkeypatch.setenv("PRODUCT_API_TOKEN", "tok")
    calls: list[httpx.Request] = []
    client = ProductClient(_policy(tmp_path), transport=_transport(calls))
    await client.request(
        "GET", operation_id="getItem", path="/items/{item_id}", path_params={"item_id": "42"}
    )
    assert calls[-1].url.raw_path == b"/items/42"
    await client.request(
        "GET", operation_id="getItem", path="/items/{item_id}", path_params={"item_id": "a b+c"}
    )
    assert calls[-1].url.raw_path == b"/items/a%20b%2Bc"
    # A query or fragment inside a value is data, not URL syntax.
    await client.request(
        "GET", operation_id="getItem", path="/items/{item_id}", path_params={"item_id": "a?b=1#f"}
    )
    assert calls[-1].url.raw_path == b"/items/a%3Fb%3D1%23f"
    assert render_path("/sites/{siteId}/topology", {"siteId": "x y"}) == "/sites/x%20y/topology"
    with pytest.raises(PolicyViolation, match="no parameter"):
        render_path("/items/{item_id}", {"item_id": "1", "other": "2"})
    with pytest.raises(PolicyViolation, match="needs value"):
        render_path("/items/{item_id}", {})


@pytest.mark.parametrize(
    "path",
    [
        "/items/1/../../admin",
        "/items/..",
        "/items/%2e%2e",
        "/items/1%2F..%2F..%2Fadmin",
        "/items//1",
        "/admin",
    ],
)
async def test_concrete_paths_are_validated_and_checked_against_the_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """A caller-supplied concrete path can neither traverse nor leave the policy's template."""
    monkeypatch.setenv("PRODUCT_API_BASE_URL", "http://product.test")
    monkeypatch.setenv("PRODUCT_API_TOKEN", "tok")
    doc = json.loads(json.dumps(POLICY_DOC))
    # Pin the path alongside the operationId, as the shipped policy example does.
    doc["product_api"]["allowed_operations"] = [
        {"operationId": "getItem", "path": "/items/{item_id}"}
    ]
    policy = ProductPolicy.from_dict(doc, source=tmp_path / "product-policy.yaml")
    calls: list[httpx.Request] = []
    client = ProductClient(policy, transport=_transport(calls))
    with pytest.raises(PolicyViolation):
        await client.request("GET", operation_id="getItem", path=path)
    assert calls == []
    await client.request("GET", operation_id="getItem", path="/items/1")
    assert calls[-1].url.raw_path == b"/items/1"


async def test_bearer_without_token_is_a_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRODUCT_API_BASE_URL", "http://product.test")
    monkeypatch.delenv("PRODUCT_API_TOKEN", raising=False)
    client = ProductClient(_policy(tmp_path), transport=_transport([]))
    with pytest.raises(ProductAPIError, match="PRODUCT_API_TOKEN"):
        await client.request("GET", operation_id="getItem", path="/items/1")


async def test_forwarded_session_sends_the_callers_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRODUCT_API_BASE_URL", "http://product.test")
    doc = json.loads(json.dumps(POLICY_DOC))
    doc["product_api"]["auth"] = "forwarded-session"
    policy = ProductPolicy.from_dict(doc, source=tmp_path / "product-policy.yaml")
    calls: list[httpx.Request] = []
    principal = Principal(
        id="u1", attributes={"session_cookie": "sid=abc", "session_token": "tok123"}
    )
    client = ProductClient(policy, transport=_transport(calls)).for_principal(principal)
    await client.request("GET", operation_id="getItem", path="/items/1")
    assert calls[0].headers["cookie"] == "sid=abc"
    assert calls[0].headers["x-session-token"] == "tok123"


async def test_no_policy_file_means_unrestricted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRODUCT_POLICY_PATH", str(tmp_path / "missing.yaml"))
    policy = load_policy()
    assert not policy.declared
    policy.check("DELETE", operation_id="anything", path="/x")  # no exception


def test_shipped_tools_declare_allowed_calls(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    assert check_tool_declarations(policy=policy) == []


def test_lint_check_reports_disallowed_tool_declarations(tmp_path: Path) -> None:
    doc = json.loads(json.dumps(POLICY_DOC))
    doc["product_api"]["allowed_operations"] = [{"operationId": "somethingElse"}]
    policy = ProductPolicy.from_dict(doc, source=tmp_path / "product-policy.yaml")
    violations = check_tool_declarations(policy=policy)
    assert any("product_lookup" in v and "getItem" in v for v in violations)
