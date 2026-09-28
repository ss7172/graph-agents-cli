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

"""`auth: exchange` against an RFC 8693 issuer over HTTP (`fake_issuer.py`, loopback).

* Conformance: what `token_exchange` sends is what the issuer accepts (Basic and
  `client_secret_post` client authentication, audience, scope, resource); the
  token it gets back verifies at the callee as the user presented by this agent
  (`act`); the issuer's refusals (an audience the client may not act for, a
  service's own token, a wrong secret) reach the call as refusals.
* Depth: nested exchanges carry the chain the callee's `AUTH_MAX_DELEGATION_DEPTH`
  reads, hop by hop.
* An issuer that hangs: each call waits at most the deadline, the breaker then
  fails calls at once, and the first call after the window recovers.
* This app as the concierge, end to end over `/chat` with alice's token: the
  exchange happens in the tool call, never while authenticating; once per user
  and API; not at all for a run that calls no such API; for a gated call only
  once the person approved; and neither alice's token nor the exchanged one is
  in the logs or (with `TEST_POSTGRES_DSN`) in any table of the database.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# The environment must be in place before the app (and the graph) is imported.
os.environ.update(
    {
        "MODEL_PROVIDER": "fake",
        "MODEL_NAME": "fake",
        "CHECKPOINTER": "memory",
        "AUTH_POLICY": "shared-bearer",
        "API_KEY": "test-key",
        "APP_ENV": "dev",
        "TRACING_ENABLED": "false",
        "RUNTIME": "fastapi",
        "APP_URL": "http://testserver",
    }
)

import httpx
import jwt
import pytest
from fake_issuer import FakeIssuer
from fastapi import HTTPException
from langchain.tools import ToolRuntime
from langchain_core.tools import tool
from starlette.requests import Request

from {{cookiecutter.agent_directory}}.app_utils.api_client import get_client
from {{cookiecutter.agent_directory}}.app_utils.api_client import (
    reset_policy_cache as reset_api_policy,
)
from {{cookiecutter.agent_directory}}.app_utils.auth import require, reset_policy_cache
from {{cookiecutter.agent_directory}}.app_utils.token_exchange import (
    TokenExchangeError,
    exchanger,
    reset_token_exchange,
)
from {{cookiecutter.agent_directory}}.fast_api_app import app

SECRET = "s3cret"
CLIENTS = {
    "concierge": (SECRET, {"billing", "orders"}),
    "billing": (SECRET, {"orders"}),
    "orders": (SECRET, {"shipping"}),
}


@pytest.fixture(scope="module")
def issuer() -> Iterator[FakeIssuer]:
    server = FakeIssuer(CLIENTS)
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def exchange_env(issuer: FakeIssuer, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeIssuer]:
    """This agent is the concierge client at the issuer; the callee reads the issuer's keys."""
    for name, value in {
        "TOKEN_EXCHANGE_URL": issuer.token_url,
        "TOKEN_EXCHANGE_CLIENT_ID": "concierge",
        "TOKEN_EXCHANGE_CLIENT_SECRET": SECRET,
        "AUTH_POLICY": "jwt",
        "AUTH_JWT_JWKS_URL": issuer.jwks_url,
        "AUTH_JWT_ISSUER": issuer.issuer,
    }.items():
        monkeypatch.setenv(name, value)
    for name in (
        "TOKEN_EXCHANGE_CLIENT_AUTH",
        "TOKEN_EXCHANGE_TIMEOUT_MS",
        "TOKEN_EXCHANGE_FAILURE_TTL_S",
        "AUTH_JWT_PUBLIC_KEY",
        "AUTH_JWT_DIRECT_CLIENTS",
        "AUTH_MAX_DELEGATION_DEPTH",
    ):
        monkeypatch.delenv(name, raising=False)
    issuer.answers.clear()
    issuer.hold.set()
    issuer.expires_in = 300
    issuer.act = True
    reset_token_exchange()
    reset_policy_cache()
    yield issuer
    issuer.hold.set()
    reset_token_exchange()
    reset_policy_cache()


def _request(token: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/a2a/x",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
            "query_string": b"",
            "path_params": {},
        }
    )


async def _at(
    monkeypatch: pytest.MonkeyPatch, audience: str, token: str, allowed: str, depth: int = 3
) -> Any:
    """The principal the agent `audience` (jwt, the issuer's keys) sees for `token`."""
    monkeypatch.setenv("AUTH_JWT_AUDIENCE", audience)
    monkeypatch.setenv("AUTH_ALLOWED_ACTORS", allowed)
    monkeypatch.setenv("AUTH_MAX_DELEGATION_DEPTH", str(depth))
    reset_policy_cache()
    return await require("a2a.invoke")(_request(token))


def _as(client: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKEN_EXCHANGE_CLIENT_ID", client)


def _claims(token: str) -> dict[str, Any]:
    return jwt.decode(token, options={"verify_signature": False})


# ---------------------------------------------------------------------------
# Conformance
# ---------------------------------------------------------------------------


async def test_the_exchanged_token_is_the_user_presented_by_this_agent(
    exchange_env: FakeIssuer, monkeypatch: pytest.MonkeyPatch
) -> None:
    issuer = exchange_env
    alice = issuer.login("alice", audience="concierge")
    token = await exchanger().token(
        "orders_agent",
        alice,
        audience="orders",
        scope="orders.read",
        resource="https://orders.example.com",
        subject_expires_at=_claims(alice)["exp"],
    )
    assert issuer.requests[-1]["authorization"].startswith("Basic ")
    claims = _claims(token)
    assert (claims["aud"], claims["azp"], claims["act"]) == (
        "orders",
        "concierge",
        {"sub": "concierge"},
    )
    assert (claims["scope"], claims["resource"]) == ("orders.read", "https://orders.example.com")
    principal = await _at(monkeypatch, "orders", token, allowed="concierge")
    assert principal.id == "alice"
    assert principal.actor.id == "concierge" and principal.actor.chain == ("concierge",)
    # A token minted for orders is refused at billing (the audience is pinned).
    with pytest.raises(HTTPException) as refused:
        await _at(monkeypatch, "billing", token, allowed="concierge")
    assert refused.value.status_code == 401


async def test_client_secret_post_conforms(
    exchange_env: FakeIssuer, monkeypatch: pytest.MonkeyPatch
) -> None:
    issuer = exchange_env
    monkeypatch.setenv("TOKEN_EXCHANGE_CLIENT_AUTH", "client_secret_post")
    token = await exchanger().token(
        "billing_agent", issuer.login("bob", audience="concierge"), audience="billing"
    )
    sent = issuer.requests[-1]
    assert sent["authorization"] == ""
    assert (sent["form"]["client_id"], sent["form"]["client_secret"]) == ("concierge", SECRET)
    assert _claims(token)["act"] == {"sub": "concierge"}


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        ("audience", "invalid_target"),  # the client may not act for that audience
        ("service", "invalid_grant"),  # a service's own token is never exchanged
        ("secret", "invalid_client"),  # a wrong client secret
        ("foreign", "invalid_grant"),  # a token the issuer did not sign
    ],
)
async def test_the_issuers_refusals_reach_the_call(
    exchange_env: FakeIssuer, monkeypatch: pytest.MonkeyPatch, setup: str, code: str
) -> None:
    issuer = exchange_env
    subject, audience = issuer.login("alice", audience="concierge"), "orders"
    if setup == "audience":
        audience = "shipping"
    elif setup == "service":
        subject = issuer.login("concierge", audience="concierge", client="concierge")
    elif setup == "secret":
        monkeypatch.setenv("TOKEN_EXCHANGE_CLIENT_SECRET", "wrong")
    else:
        subject = FakeIssuer(CLIENTS).login("alice", audience="concierge")
    with pytest.raises(TokenExchangeError) as exc:
        await exchanger().token("orders_agent", subject, audience=audience)
    assert str(exc.value) == (
        f"token exchange for API 'orders_agent' was refused ({code}); nothing was sent."
    )


async def test_an_issuer_that_names_no_actor(
    exchange_env: FakeIssuer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Some issuers (Keycloak's standard token exchange among them) put no `act` in an
    exchanged token, only `azp`: the callee then tells the agent by its client."""
    issuer = exchange_env
    issuer.act = False
    token = await exchanger().token(
        "orders_agent", issuer.login("alice", audience="concierge"), audience="orders"
    )
    assert "act" not in _claims(token) and _claims(token)["azp"] == "concierge"
    # Without AUTH_JWT_DIRECT_CLIENTS such a token reads as alice's own: list the sign-in
    # clients, and the concierge presenting it is `client:concierge`.
    monkeypatch.setenv("AUTH_JWT_DIRECT_CLIENTS", "web")
    principal = await _at(monkeypatch, "orders", token, allowed="client:concierge")
    assert principal.id == "alice" and principal.actor.id == "client:concierge"
    own = issuer.login("alice", audience="orders")  # alice's own sign-in (client web)
    assert (await _at(monkeypatch, "orders", own, allowed="client:concierge")).actor is None


async def test_the_issuers_lifetime_is_capped(
    exchange_env: FakeIssuer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An answer without `expires_in` is kept 30 s (60 s less the margin); a long one 270 s."""
    issuer = exchange_env
    now = [0.0]
    reset_token_exchange(clock=lambda: now[0])
    alice = issuer.login("alice", audience="concierge", ttl_s=7200)
    for expires_in, kept in ((None, 30), (3600, 270)):
        exchanger().clear()
        issuer.expires_in = expires_in
        first = await exchanger().token("orders_agent", alice, audience="orders")
        now[0] += kept - 1
        assert await exchanger().token("orders_agent", alice, audience="orders") == first
        now[0] += 2
        assert await exchanger().token("orders_agent", alice, audience="orders") != first


# ---------------------------------------------------------------------------
# Depth across real exchanges
# ---------------------------------------------------------------------------


async def test_the_delegation_depth_holds_across_real_exchanges(
    exchange_env: FakeIssuer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """concierge -> billing -> orders passes at depth 2; one more hop is 401 at the callee."""
    issuer = exchange_env
    at_concierge = issuer.login("alice", audience="concierge")
    at_billing = await exchanger().token("billing_agent", at_concierge, audience="billing")
    _as("billing", monkeypatch)
    at_orders = await exchanger().token("orders_agent", at_billing, audience="orders")
    principal = await _at(monkeypatch, "orders", at_orders, allowed="billing", depth=2)
    assert principal.id == "alice" and principal.actor.chain == ("billing", "concierge")
    _as("orders", monkeypatch)
    at_shipping = await exchanger().token("shipping_agent", at_orders, audience="shipping")
    with pytest.raises(HTTPException) as exc:
        await _at(monkeypatch, "shipping", at_shipping, allowed="orders", depth=2)
    assert (exc.value.status_code, exc.value.detail) == (
        401,
        "Invalid bearer token: delegation too deep.",
    )
    principal = await _at(monkeypatch, "shipping", at_shipping, allowed="orders", depth=3)
    assert principal.actor.chain == ("orders", "billing", "concierge")


# ---------------------------------------------------------------------------
# An issuer that hangs
# ---------------------------------------------------------------------------


async def test_an_issuer_that_hangs(
    exchange_env: FakeIssuer, monkeypatch: pytest.MonkeyPatch
) -> None:
    issuer = exchange_env
    monkeypatch.setenv("TOKEN_EXCHANGE_TIMEOUT_MS", "300")
    monkeypatch.setenv("TOKEN_EXCHANGE_FAILURE_TTL_S", "1")
    issuer.hold.clear()
    for user in ("u1", "u2", "u3"):
        started = time.monotonic()
        with pytest.raises(TokenExchangeError, match=r"unavailable \(timed out\)"):
            await exchanger().token(
                "orders_agent", issuer.login(user, audience="concierge"), audience="orders"
            )
        assert time.monotonic() - started < 1.0
    started = time.monotonic()
    with pytest.raises(TokenExchangeError, match=r"retry in 1 s"):
        await exchanger().token(
            "orders_agent", issuer.login("u4", audience="concierge"), audience="orders"
        )
    assert time.monotonic() - started < 0.05
    issuer.hold.set()
    await asyncio.sleep(1.1)
    token = await exchanger().token(
        "orders_agent", issuer.login("u5", audience="concierge"), audience="orders"
    )
    assert _claims(token)["sub"] == "u5"


# ---------------------------------------------------------------------------
# This app as the concierge, end to end
# ---------------------------------------------------------------------------

POLICY = """
apis:
  orders_agent:
    base_url_env: ORDERS_AGENT_URL
    auth: exchange
    exchange: {audience: orders}
    allowed_methods: [GET, POST]
    approval:
      required_for:
        operations:
          - {operationId: cancelOrder, path: "/orders/{order_id}/cancel", methods: [POST]}
      approvers: [requester]
      timeout_s: 60
"""

# What the orders agent received: the verified claims of each bearer, and the tokens.
RECEIVED: list[dict[str, Any]] = []
TOKENS: list[str] = []
ISSUER: list[FakeIssuer] = []


def _orders_backend(request: httpx.Request) -> httpx.Response:
    token = request.headers["authorization"].removeprefix("Bearer ")
    claims = jwt.decode(
        token,
        ISSUER[0].key.public_key(),
        algorithms=["RS256"],
        audience="orders",
        issuer=ISSUER[0].issuer,
    )
    RECEIVED.append({"path": request.url.path, "method": request.method, **claims})
    TOKENS.append(token)
    return httpx.Response(200, json={"status": "shipped"})


@tool
async def check_orders(runtime: ToolRuntime[Any]) -> str:
    """Check the caller's orders at the orders agent."""
    client = get_client(
        "orders_agent", context=runtime.context, transport=httpx.MockTransport(_orders_backend)
    )
    return json.dumps(await client.get("/orders/status"))


@tool
async def cancel_order(order_id: str, runtime: ToolRuntime[Any]) -> str:
    """Cancel an order by its id."""
    client = get_client(
        "orders_agent", context=runtime.context, transport=httpx.MockTransport(_orders_backend)
    )
    data = await client.post(
        "/orders/{order_id}/cancel", operation_id="cancelOrder", path_params={"order_id": order_id}
    )
    return json.dumps(data)


ADMIN_DSN = os.environ.get("TEST_POSTGRES_DSN", "")


@pytest.fixture(params=["memory", "postgres"])
async def database(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[str | None]:
    if request.param == "memory":
        yield None
        return
    if not ADMIN_DSN:
        pytest.skip("TEST_POSTGRES_DSN is not set")
    import psycopg

    name = f"gac_test_{uuid.uuid4().hex[:12]}"
    async with await psycopg.AsyncConnection.connect(ADMIN_DSN, autocommit=True) as admin:
        await admin.execute(f'CREATE DATABASE "{name}"')
    dsn = urlsplit(ADMIN_DSN)._replace(path=f"/{name}").geturl()
    monkeypatch.setenv("CHECKPOINTER", "postgres")
    monkeypatch.setenv("POSTGRES_DSN", dsn)
    yield dsn
    async with await psycopg.AsyncConnection.connect(ADMIN_DSN, autocommit=True) as admin:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
async def concierge(
    database: str | None,
    exchange_env: FakeIssuer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_test_tools: Any,
) -> AsyncIterator[httpx.AsyncClient]:
    policy = tmp_path / "api-policy.yaml"
    policy.write_text(POLICY, encoding="utf-8")
    monkeypatch.setenv("API_POLICY_PATH", str(policy))
    monkeypatch.setenv("ORDERS_AGENT_URL", "https://orders.test")
    monkeypatch.setenv("AUTH_JWT_AUDIENCE", "concierge")
    monkeypatch.delenv("AUTH_ALLOWED_ACTORS", raising=False)
    reset_policy_cache()
    reset_api_policy()
    RECEIVED.clear()
    TOKENS.clear()
    ISSUER[:] = [exchange_env]
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", timeout=30
        ) as client:
            started = asyncio.get_running_loop().time()
            while (await client.get("/ready")).status_code != 200:  # postgres: the schema
                assert asyncio.get_running_loop().time() - started < 30, "not ready"
                await asyncio.sleep(0.1)
            client.use_tools = use_test_tools  # type: ignore[attr-defined]
            client.dsn = database  # type: ignore[attr-defined]
            yield client
    reset_policy_cache()
    reset_api_policy()


def _events(text: str) -> list[tuple[str, dict[str, Any]]]:
    events, event = [], None
    for line in text.splitlines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:") and event:
            events.append((event, json.loads(line[5:].strip())))
            event = None
    return events


async def _chat(client: httpx.AsyncClient, token: str, message: str) -> list[tuple[str, dict]]:
    r = await client.post(
        "/chat", json={"message": message}, headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200, r.text
    return _events(r.text)


async def test_one_exchange_per_user_and_api_in_the_tool_call_only(
    concierge: httpx.AsyncClient,
) -> None:
    issuer = ISSUER[0]
    concierge.use_tools(check_orders)  # type: ignore[attr-defined]
    alice = issuer.login(f"alice-{uuid.uuid4().hex[:6]}", audience="concierge")
    bob = issuer.login(f"bob-{uuid.uuid4().hex[:6]}", audience="concierge")
    before = len(issuer.exchanges)
    # Authenticating alone never exchanges (the subject token is kept, not used).
    listed = await concierge.get("/threads", headers={"Authorization": f"Bearer {alice}"})
    assert listed.status_code == 200 and len(issuer.exchanges) == before
    # A run that calls no exchange API exchanges nothing.
    await _chat(concierge, alice, "Hello there")
    assert len(issuer.exchanges) == before and RECEIVED == []
    for token in (alice, alice, bob, alice):
        events = await _chat(concierge, token, "Check my orders please")
        assert any(name == "tool.result" for name, _ in events), events
    assert len(RECEIVED) == 4
    assert len(issuer.exchanges) - before == 2  # the distinct (user, API) pairs
    first = RECEIVED[0]
    assert first["sub"] == _claims(alice)["sub"]
    assert (first["aud"], first["azp"], first["act"]) == (
        "orders",
        "concierge",
        {"sub": "concierge"},
    )
    assert {r["sub"] for r in RECEIVED} == {_claims(alice)["sub"], _claims(bob)["sub"]}


async def test_a_gated_call_exchanges_only_after_the_person_approves(
    concierge: httpx.AsyncClient,
) -> None:
    issuer = ISSUER[0]
    concierge.use_tools(cancel_order)  # type: ignore[attr-defined]
    alice = issuer.login(f"alice-{uuid.uuid4().hex[:6]}", audience="concierge")
    before = len(issuer.exchanges)
    events = await _chat(concierge, alice, "Cancel the order for 7")
    end = events[-1][1]
    assert end["status"] == "awaiting_approval", events
    assert len(issuer.exchanges) == before and RECEIVED == []  # paused: nothing exchanged
    approval = end["approval"]
    decided = await concierge.post(
        f"/threads/{end['thread_id']}/approvals/{approval['approval_id']}",
        json={"decision": "approve"},
        headers={"Authorization": f"Bearer {alice}"},
    )
    assert decided.status_code == 200, decided.text
    assert [(r["method"], r["path"]) for r in RECEIVED] == [("POST", "/orders/7/cancel")]
    assert len(issuer.exchanges) - before == 1


async def test_no_token_at_rest(
    concierge: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Neither the user's token nor the exchanged one is logged or stored (T5)."""
    issuer = ISSUER[0]
    caplog.set_level(logging.DEBUG)
    concierge.use_tools(check_orders)  # type: ignore[attr-defined]
    alice = issuer.login(f"alice-{uuid.uuid4().hex[:6]}", audience="concierge")
    await _chat(concierge, alice, "Check my orders please")
    concierge.use_tools(cancel_order)  # type: ignore[attr-defined]
    end = (await _chat(concierge, alice, "Cancel the order for 7"))[-1][1]
    await concierge.post(
        f"/threads/{end['thread_id']}/approvals/{end['approval']['approval_id']}",
        json={"decision": "approve"},
        headers={"Authorization": f"Bearer {alice}"},
    )
    assert len(TOKENS) == 2
    secrets = {alice, *TOKENS, SECRET}
    logged = caplog.text + "".join(str(record.__dict__) for record in caplog.records)
    assert [s for s in secrets if s in logged] == []
    dsn = concierge.dsn  # type: ignore[attr-defined]
    if dsn is None:
        return
    import psycopg

    found: list[str] = []
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        tables = await (
            await conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            )
        ).fetchall()
        assert tables, "the app created no tables"
        for (table,) in tables:
            for row in await (await conn.execute(f'SELECT * FROM "{table}"')).fetchall():
                for value in row:
                    data = (
                        bytes(value)
                        if isinstance(value, bytes | memoryview)
                        else json.dumps(value, default=str).encode()
                    )
                    found.extend(table for s in secrets if s.encode() in data)
    assert found == []
