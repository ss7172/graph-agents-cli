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

"""RFC 8693 token exchange (`app_utils.token_exchange`) and the `auth: exchange` calls it serves.

The issuer's token endpoint is an httpx MockTransport (`FakeTokenEndpoint`),
and so is the API the exchanged token is sent to; the clocks are the test's.
Nothing leaves the process. `tests/integration/test_token_exchange_issuer.py`
runs the same exchange against a real (loopback) RFC 8693 issuer.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote

import httpx
import pytest

from {{cookiecutter.agent_directory}}.app_utils import api_client, metrics, token_exchange
from {{cookiecutter.agent_directory}}.app_utils.api_client import (
    APPROVAL_DECISION,
    ApiCallError,
    ApiPolicyError,
    BoundApproval,
    get_client,
    reset_limits,
    reset_policy_cache,
    set_approval_ledger,
    set_outbound_headers,
)
from {{cookiecutter.agent_directory}}.app_utils.limits import SettingsError
from {{cookiecutter.agent_directory}}.app_utils.token_exchange import (
    ACCESS_TOKEN_TYPE,
    GRANT_TYPE,
    JWT_TOKEN_TYPE,
    ExchangeSettings,
    TokenExchangeError,
    compatibility,
    exchange_settings,
    exchanger,
    loop_problem,
    names_no_actor,
    reset_token_exchange,
    startup_problems,
)

TOKEN_URL = "https://issuer.test/realms/agents/token"
CLIENT_ID = "concierge"
CLIENT_SECRET = "s3cret:with/odd chars"
SUBJECT = "alice-own-token.at-concierge"
WALL_START = 1_900_000_000.0

POLICY = """
apis:
  orders_agent:
    base_url_env: ORDERS_AGENT_URL
    auth: exchange
    exchange:
      audience: orders
      scope: "orders.read orders.cancel"
      resource: https://orders.example.com
    allowed_methods: [GET, POST]
    limits: {max_calls_per_run: 3}
    approval:
      required_for:
        operations:
          - {operationId: cancelOrder, path: "/orders/{order_id}/cancel", methods: [POST]}
      approvers: [requester]
  billing_agent:
    base_url_env: BILLING_AGENT_URL
    auth: exchange
    forward_header: X-Agent-Token
    exchange: {audience: billing, allow_actorless: true}
    allowed_methods: [GET]
  weather:
    base_url_env: WEATHER_URL
    auth: bearer
    token_env: WEATHER_TOKEN
    allowed_methods: [GET]
"""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _jwt(claims: Any) -> str:
    """A JWS-shaped token with these claims; the exchanger never verifies it (the callee does)."""
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    return f"{header}.{_b64(json.dumps(claims).encode())}.{_b64(b'signature')}"


def exchanged(n: int, audience: str) -> str:
    """The token `FakeTokenEndpoint` issues for its `n`-th request, for `audience`: a JWT
    naming this agent in `act`, as an issuer that follows RFC 8693 mints it."""
    return _jwt({"sub": "alice", "aud": audience, "n": n, "act": {"sub": CLIENT_ID}})


class Clock:
    """Both clocks the exchanger reads: monotonic (`mono`) and wall (`wall`, epoch seconds)."""

    def __init__(self) -> None:
        self.now = 0.0

    def mono(self) -> float:
        return self.now

    def wall(self) -> float:
        return WALL_START + self.now


class FakeTokenEndpoint:
    """An RFC 8693 token endpoint: records every request, answers from `answers` or issues.

    An issued token is `exchanged(<n>, <audience>)` (a JWT naming this agent in `act`),
    `Bearer`, an access token, with
    `expires_in`. `delay` holds each answer that long (asyncio time); `hang` never
    answers.
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.answers: list[httpx.Response] = []
        self.expires_in: int | None = 300
        self.delay = 0.0
        self.hang = False

    @property
    def calls(self) -> int:
        return len(self.requests)

    async def handler(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.requests.append({"url": str(request.url), "headers": request.headers, "form": form})
        if self.hang:
            await asyncio.sleep(3600)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.answers:
            return self.answers.pop(0)
        body: dict[str, Any] = {
            "access_token": exchanged(len(self.requests), str(form.get("audience"))),
            "issued_token_type": ACCESS_TOKEN_TYPE,
            "token_type": "Bearer",
        }
        if self.expires_in is not None:
            body["expires_in"] = self.expires_in
        return httpx.Response(200, json=body)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def endpoint(clock: Clock, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeTokenEndpoint]:
    server = FakeTokenEndpoint()
    for name, value in {
        "TOKEN_EXCHANGE_URL": TOKEN_URL,
        "TOKEN_EXCHANGE_CLIENT_ID": CLIENT_ID,
        "TOKEN_EXCHANGE_CLIENT_SECRET": CLIENT_SECRET,
    }.items():
        monkeypatch.setenv(name, value)
    for name in (
        "APP_ENV",
        "A2A_NAME",
        "AUTH_JWT_AUDIENCE",
        "TOKEN_EXCHANGE_CLIENT_AUTH",
        "TOKEN_EXCHANGE_SUBJECT_TOKEN_TYPE",
        "TOKEN_EXCHANGE_TIMEOUT_MS",
        "TOKEN_EXCHANGE_MAX_TTL_S",
        "TOKEN_EXCHANGE_FAILURE_TTL_S",
        "TOKEN_EXCHANGE_CACHE_MAX",
        "TOKEN_EXCHANGE_ALLOW_HTTP",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_token_exchange(clock=clock.mono, wall=clock.wall, transport=server.transport)
    yield server
    reset_token_exchange()


def _count(api: str, outcome: str) -> float:
    value = metrics.REGISTRY.get_sample_value(
        "agent_token_exchanges_total", {"api": api, "outcome": outcome}
    )
    return value or 0.0


async def _token(api: str = "orders_agent", subject: str = SUBJECT, **kwargs: Any) -> str:
    kwargs.setdefault("audience", "orders")
    return await exchanger().token(api, subject, **kwargs)


# ---------------------------------------------------------------------------
# The request (RFC 8693 section 2.1, RFC 6749 section 2.3.1)
# ---------------------------------------------------------------------------


async def test_the_request_is_an_rfc_8693_exchange_with_basic_client_auth(
    endpoint: FakeTokenEndpoint,
) -> None:
    token = await _token(scope="orders.read", resource="https://orders.example.com")
    assert token == exchanged(1, "orders")
    [sent] = endpoint.requests
    assert sent["url"] == TOKEN_URL
    assert sent["form"] == {
        "grant_type": GRANT_TYPE,
        "subject_token": SUBJECT,
        "subject_token_type": ACCESS_TOKEN_TYPE,
        "requested_token_type": ACCESS_TOKEN_TYPE,
        "audience": "orders",
        "scope": "orders.read",
        "resource": "https://orders.example.com",
    }
    scheme, _, encoded = sent["headers"]["authorization"].partition(" ")
    client, _, secret = base64.b64decode(encoded).decode().partition(":")
    assert scheme == "Basic"
    # Each part form-encoded before base64 (RFC 6749 section 2.3.1).
    assert (unquote(client), unquote(secret)) == (CLIENT_ID, CLIENT_SECRET)
    assert secret != CLIENT_SECRET


async def test_client_secret_post_and_the_jwt_subject_token_type(
    endpoint: FakeTokenEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOKEN_EXCHANGE_CLIENT_AUTH", "client_secret_post")
    monkeypatch.setenv("TOKEN_EXCHANGE_SUBJECT_TOKEN_TYPE", JWT_TOKEN_TYPE)
    await _token()
    [sent] = endpoint.requests
    assert "authorization" not in sent["headers"]
    assert sent["form"]["client_id"] == CLIENT_ID
    assert sent["form"]["client_secret"] == CLIENT_SECRET
    assert sent["form"]["subject_token_type"] == JWT_TOKEN_TYPE
    assert "scope" not in sent["form"] and "resource" not in sent["form"]


async def test_redirects_are_not_followed(endpoint: FakeTokenEndpoint) -> None:
    endpoint.answers.append(httpx.Response(302, headers={"Location": "https://evil.test/token"}))
    with pytest.raises(TokenExchangeError, match=r"token issuer unavailable \(HTTP 302\)"):
        await _token()
    assert endpoint.calls == 1


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------


async def test_single_flight_shares_one_exchange(endpoint: FakeTokenEndpoint) -> None:
    endpoint.delay = 0.05
    before = _count("sf_api", "issued"), _count("sf_api", "cached")
    tokens = await asyncio.gather(*(_token("sf_api") for _ in range(100)))
    assert endpoint.calls == 1
    assert set(tokens) == {exchanged(1, "orders")}
    assert _count("sf_api", "issued") - before[0] == 1
    assert _count("sf_api", "cached") - before[1] == 99


async def test_cache_is_per_subject_token(endpoint: FakeTokenEndpoint) -> None:
    """T6: a token exchanged for one user's token is never handed to another's call."""
    first = await _token(subject="alice-token")
    second = await _token(subject="bob-token")
    assert first != second and endpoint.calls == 2
    assert await _token(subject="alice-token") == first
    assert await _token(subject="bob-token") == second
    assert endpoint.calls == 2
    assert [r["form"]["subject_token"] for r in endpoint.requests] == ["alice-token", "bob-token"]


async def test_audience_pinned(endpoint: FakeTokenEndpoint) -> None:
    """T5: each API gets a token minted for its own audience (and scope and resource)."""
    orders = await _token(audience="orders")
    inventory = await _token("inventory_agent", audience="inventory")
    narrower = await _token(audience="orders", scope="orders.read")
    assert len({orders, inventory, narrower}) == 3
    assert [r["form"]["audience"] for r in endpoint.requests] == ["orders", "inventory", "orders"]
    assert await _token(audience="orders") == orders
    assert endpoint.calls == 3


@pytest.mark.parametrize(
    ("expires_in", "subject_left", "kept_for"),
    [
        (600, None, 270),  # the TOKEN_EXCHANGE_MAX_TTL_S cap (300) less the 30 s margin
        (100, None, 70),  # the issuer's expires_in less the margin
        (None, None, 30),  # expires_in absent reads as 60
        (300, 90, 60),  # never past the subject token's own expiry
        (300, 34, 0),  # kept under 5 s: used once, not kept
        (20, None, 0),
    ],
)
async def test_ttl_caps(
    endpoint: FakeTokenEndpoint,
    clock: Clock,
    expires_in: int | None,
    subject_left: int | None,
    kept_for: int,
) -> None:
    endpoint.expires_in = expires_in
    exp = clock.wall() + subject_left if subject_left is not None else None
    first = await _token(subject_expires_at=exp)
    if kept_for:
        clock.now += kept_for - 1
        assert await _token(subject_expires_at=exp) == first
        assert endpoint.calls == 1
        clock.now += 2
    await _token(subject_expires_at=exp)
    assert endpoint.calls == 2


async def test_a_lower_ttl_cap_setting(
    endpoint: FakeTokenEndpoint, clock: Clock, monkeypatch
) -> None:
    monkeypatch.setenv("TOKEN_EXCHANGE_MAX_TTL_S", "60")
    first = await _token()
    clock.now += 29
    assert await _token() == first
    clock.now += 2
    await _token()
    assert endpoint.calls == 2


async def test_the_cache_is_bounded(endpoint: FakeTokenEndpoint, monkeypatch) -> None:
    monkeypatch.setenv("TOKEN_EXCHANGE_CACHE_MAX", "2")
    for user in ("a", "b", "c"):
        await _token(subject=user)
    assert exchanger().cached() == 2
    await _token(subject="c")  # the most recent ones are kept
    await _token(subject="a")  # the oldest went
    assert endpoint.calls == 4


async def test_a_subject_token_about_to_expire_is_not_exchanged(
    endpoint: FakeTokenEndpoint, clock: Clock
) -> None:
    with pytest.raises(
        TokenExchangeError, match="the caller's token has expired; nothing was sent"
    ):
        await _token(subject_expires_at=clock.wall() + 10)
    assert endpoint.calls == 0
    await _token(subject_expires_at=clock.wall() + 11)
    assert endpoint.calls == 1


# ---------------------------------------------------------------------------
# Failures: refusals, the breaker, timeouts, unusable answers
# ---------------------------------------------------------------------------


async def test_a_refusal_is_remembered_for_the_failure_ttl(
    endpoint: FakeTokenEndpoint, clock: Clock
) -> None:
    endpoint.answers.append(
        httpx.Response(400, json={"error": "invalid_target", "error_description": SUBJECT})
    )
    before = _count("refused_api", "refused")
    with pytest.raises(TokenExchangeError) as exc:
        await _token("refused_api")
    assert str(exc.value) == (
        "token exchange for API 'refused_api' was refused (invalid_target); nothing was sent."
    )
    assert SUBJECT not in str(exc.value)
    clock.now += 9
    with pytest.raises(TokenExchangeError, match="invalid_target"):
        await _token("refused_api")
    assert endpoint.calls == 1  # remembered, not asked again
    assert _count("refused_api", "refused") - before == 2
    await _token("other_user_api", subject="bob")  # another key is not refused
    clock.now += 2
    assert await _token("refused_api") == exchanged(3, "orders")


async def test_a_refusal_without_an_error_code_names_the_status(
    endpoint: FakeTokenEndpoint,
) -> None:
    endpoint.answers.append(httpx.Response(403, text="<html>no " + SUBJECT + "</html>"))
    with pytest.raises(TokenExchangeError, match=r"was refused \(HTTP 403\)"):
        await _token()


async def test_breaker_fails_fast(endpoint: FakeTokenEndpoint, clock: Clock) -> None:
    """T7: three failures in a row open the breaker; calls then fail at once."""
    endpoint.answers.extend(httpx.Response(503) for _ in range(3))
    for user in ("a", "b", "c"):
        with pytest.raises(TokenExchangeError, match=r"token issuer unavailable \(HTTP 503\)"):
            await _token("breaker_api", subject=user)
    before = _count("breaker_api", "circuit_open")
    with pytest.raises(TokenExchangeError) as exc:
        await _token("breaker_api", subject="d")
    assert str(exc.value) == (
        "token issuer unavailable (retry in 10 s); nothing was sent to API 'breaker_api'."
    )
    assert exc.value.outcome == "circuit_open"
    assert endpoint.calls == 3  # the issuer was left alone
    assert _count("breaker_api", "circuit_open") - before == 1
    clock.now += 10  # half open: one call probes, and closes it
    assert await _token("breaker_api", subject="d") == exchanged(4, "orders")
    await _token("breaker_api", subject="e")
    assert endpoint.calls == 5


async def test_a_failed_probe_reopens_the_breaker(
    endpoint: FakeTokenEndpoint, clock: Clock
) -> None:
    endpoint.answers.extend(httpx.Response(500) for _ in range(4))
    for user in ("a", "b", "c"):
        with pytest.raises(TokenExchangeError):
            await _token(subject=user)
    clock.now += 10
    with pytest.raises(TokenExchangeError, match=r"HTTP 500"):
        await _token(subject="probe")  # the probe fails
    with pytest.raises(TokenExchangeError, match=r"retry in 10 s"):
        await _token(subject="next")  # open again, at once
    assert endpoint.calls == 4


async def test_while_the_probe_runs_other_calls_fail_fast(
    endpoint: FakeTokenEndpoint, clock: Clock
) -> None:
    endpoint.answers.extend(httpx.Response(502) for _ in range(3))
    for user in ("a", "b", "c"):
        with pytest.raises(TokenExchangeError):
            await _token(subject=user)
    clock.now += 10
    endpoint.delay = 0.05
    probe = asyncio.create_task(_token(subject="probe"))
    await asyncio.sleep(0.01)
    with pytest.raises(TokenExchangeError, match=r"retry in \d+ s"):
        await _token(subject="other")
    assert await probe == exchanged(4, "orders")
    assert endpoint.calls == 4


async def test_a_refusal_does_not_count_against_the_breaker(endpoint: FakeTokenEndpoint) -> None:
    endpoint.answers.extend(
        [httpx.Response(503), httpx.Response(503), httpx.Response(400, json={"error": "x"})]
    )
    endpoint.answers.append(httpx.Response(503))
    for user in ("a", "b", "c", "d"):
        with pytest.raises(TokenExchangeError):
            await _token(subject=user)
    await _token(subject="e")  # the issuer answered in between: not three failures in a row
    assert endpoint.calls == 5


async def test_timeout(endpoint: FakeTokenEndpoint, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKEN_EXCHANGE_TIMEOUT_MS", "100")
    endpoint.hang = True
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(TokenExchangeError, match=r"token issuer unavailable \(timed out\)"):
        await _token()
    assert loop.time() - started < 1.0


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=["a list"]),
        httpx.Response(200, json={"token_type": "Bearer", "expires_in": 60}),
        httpx.Response(200, json={"access_token": "t", "token_type": "N_A"}),
        httpx.Response(200, json={"access_token": "t", "token_type": "DPoP"}),
        httpx.Response(200, json={"access_token": "t", "token_type": "Bearer", "expires_in": "60"}),
        httpx.Response(200, json={"access_token": "t", "token_type": "Bearer", "expires_in": 0}),
        httpx.Response(200, json={"access_token": "t", "token_type": "Bearer", "expires_in": True}),
        httpx.Response(
            200,
            json={"access_token": "t", "token_type": "Bearer", "issued_token_type": JWT_TOKEN_TYPE},
        ),
        httpx.Response(200, json={"access_token": "t" * 16_385, "token_type": "Bearer"}),
        httpx.Response(200, content=b"{" + b" " * 70_000 + b"}"),
    ],
)
async def test_an_unusable_answer_is_not_used(
    endpoint: FakeTokenEndpoint, answer: httpx.Response
) -> None:
    endpoint.answers.append(answer)
    with pytest.raises(TokenExchangeError, match=r"unusable issuer response"):
        await _token()


async def test_the_token_type_is_case_insensitive(endpoint: FakeTokenEndpoint) -> None:
    token = exchanged(1, "orders")
    endpoint.answers.append(
        httpx.Response(200, json={"access_token": token, "token_type": "bearer", "expires_in": 60})
    )
    assert await _token() == token


async def test_not_configured_sends_nothing(
    endpoint: FakeTokenEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TOKEN_EXCHANGE_CLIENT_SECRET")
    monkeypatch.delenv("TOKEN_EXCHANGE_URL")
    with pytest.raises(TokenExchangeError) as exc:
        await _token()
    assert str(exc.value) == (
        "API 'orders_agent' uses auth: exchange, but TOKEN_EXCHANGE_URL, "
        "TOKEN_EXCHANGE_CLIENT_SECRET are not set; nothing was sent."
    )
    assert endpoint.calls == 0


# ---------------------------------------------------------------------------
# Logs and metrics carry no token material
# ---------------------------------------------------------------------------


async def test_no_token_in_logs(
    endpoint: FakeTokenEndpoint, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    issued = await _token(subject=SUBJECT)
    endpoint.answers.append(
        httpx.Response(400, json={"error": "invalid_grant", "error_description": SUBJECT})
    )
    with pytest.raises(TokenExchangeError):
        await _token(subject=SUBJECT + "-2")
    endpoint.answers.extend(httpx.Response(500, text=SUBJECT) for _ in range(3))
    for user in ("x", "y", "z"):
        with pytest.raises(TokenExchangeError):
            await _token(subject=SUBJECT + user)
    text = caplog.text + "".join(str(r.__dict__) for r in caplog.records)
    assert "token exchange for orders_agent (audience orders): issued" in caplog.text
    assert "refused (invalid_grant)" in caplog.text
    assert "the issuer failed 3 times in a row" in caplog.text
    for secret in (
        SUBJECT,
        issued,
        CLIENT_SECRET,
        hashlib.sha256(SUBJECT.encode()).hexdigest(),
        hashlib.sha256(SUBJECT.encode()).hexdigest()[:16],
    ):
        assert secret not in text
    rendered = metrics.render()[0].decode()
    assert SUBJECT not in rendered and issued not in rendered


def _issue(endpoint: FakeTokenEndpoint, token: str) -> None:
    endpoint.answers.append(
        httpx.Response(200, json={"access_token": token, "token_type": "Bearer", "expires_in": 300})
    )


NO_ACTOR = "names no actor"

# Tokens that name no actor as far as the calling agent can tell (`names_no_actor`).
ACTORLESS_TOKENS = [
    _jwt({"sub": "alice", "aud": "orders", "azp": CLIENT_ID}),  # no act: Keycloak's shape
    "opaque-token",  # not a JWT
    "h.p.s.i.t",  # encrypted (JWE)
    "h..s",
    "h.***.s",  # not base64url
    f"h.{_b64(b'not json')}.s",
    f"h.{_b64(bytes([0x80, 0x81]))}.s",  # not UTF-8
    f"h.{_b64(b'[1, 2]')}.s",  # not an object
    f"h.{_b64(b'[' * 12_000)}.s",  # nested too deep to read
]
ACTORLESS_IDS = [
    "no-act",
    "opaque",
    "jwe",
    "empty-payload",
    "not-base64url",
    "not-json",
    "not-utf8",
    "not-an-object",
    "too-deep",
]


def _no_actor_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records if r.levelno >= logging.WARNING and NO_ACTOR in r.getMessage()
    ]


@pytest.mark.parametrize("token", ACTORLESS_TOKENS, ids=ACTORLESS_IDS)
async def test_an_issued_token_that_names_no_actor_is_refused(
    endpoint: FakeTokenEndpoint, caplog: pytest.LogCaptureFixture, token: str
) -> None:
    """An issuer whose exchanged tokens carry no `act` (Keycloak's standard token exchange), or
    that mints tokens this agent cannot read: the called agent would take them for the user's
    own, and could let this agent decide the user's approvals. Refused (fail closed); the
    refusal is remembered like the issuer's own, and the token is never logged."""
    caplog.set_level(logging.DEBUG)
    before = _count("orders_agent", "no_actor"), _count("orders_agent", "issued")
    _issue(endpoint, token)
    with pytest.raises(TokenExchangeError) as exc:
        await _token(subject="alice")
    assert exc.value.outcome == "no_actor"
    message = str(exc.value)
    assert "the issuer's token names no actor (no act claim, or not a readable JWT)" in message
    assert "nothing was sent" in message and "exchange.allow_actorless: true" in message
    assert "AUTH_JWT_DIRECT_CLIENTS" in message
    assert token not in message
    assert _count("orders_agent", "no_actor") - before[0] == 1
    assert _count("orders_agent", "issued") - before[1] == 0
    assert exchanger().cached() == 0
    # Remembered for the failure TTL: the issuer is not asked again, and the breaker is shut.
    with pytest.raises(TokenExchangeError) as again:
        await _token(subject="alice")
    assert again.value.outcome == "no_actor"
    assert endpoint.calls == 1
    assert _count("orders_agent", "no_actor") - before[0] == 2
    refused = (
        "token exchange for orders_agent (audience orders): refused (the token names no actor)"
    )
    assert refused in caplog.text
    logged = caplog.text + "".join(str(r.__dict__) for r in caplog.records)
    assert token not in logged
    if token.count(".") == 2 and len(token.split(".")[1]) > 3:
        assert token.split(".")[1] not in logged
    assert _no_actor_warnings(caplog) == []  # a refusal, not a warning


async def test_the_refusal_of_a_token_naming_no_actor_is_forgotten_after_the_ttl(
    endpoint: FakeTokenEndpoint, clock: Clock
) -> None:
    _issue(endpoint, _jwt({"sub": "alice", "aud": "orders"}))
    with pytest.raises(TokenExchangeError):
        await _token(subject="alice")
    clock.now += token_exchange.DEFAULT_FAILURE_TTL_S + 1
    # The issuer now names the agent (say, after its admin fixed the mapper).
    assert await _token(subject="alice") == exchanged(2, "orders")
    assert endpoint.calls == 2


@pytest.mark.parametrize("token", ACTORLESS_TOKENS, ids=ACTORLESS_IDS)
async def test_an_api_that_allows_it_takes_a_token_naming_no_actor_and_warns_once(
    endpoint: FakeTokenEndpoint, caplog: pytest.LogCaptureFixture, token: str
) -> None:
    """`exchange.allow_actorless: true`: the token is used; one warning per process says what
    the called agent must set, naming this agent's client; the token is never logged."""
    caplog.set_level(logging.DEBUG)
    _issue(endpoint, token)
    assert await _token(subject="alice", allow_actorless=True) == token
    [record] = _no_actor_warnings(caplog)
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert "the token the issuer minted for orders_agent (audience orders)" in message
    assert "(no act claim)" in message and "exchange.allow_actorless lets it through" in message
    assert "AUTH_JWT_DIRECT_CLIENTS" in message and f"client:{CLIENT_ID}" in message
    logged = caplog.text + "".join(str(r.__dict__) for r in caplog.records)
    assert token not in logged
    # Once per process: more such tokens, for other users and APIs, add no warning.
    _issue(endpoint, _jwt({"sub": "bob", "aud": "orders", "azp": CLIENT_ID}))
    _issue(endpoint, _jwt({"sub": "bob", "aud": "billing"}))
    await _token(subject="bob", allow_actorless=True)
    await _token("billing_agent", subject="bob", audience="billing", allow_actorless=True)
    assert endpoint.calls == 3
    assert _no_actor_warnings(caplog) == [record]


async def test_a_token_kept_for_an_api_that_allows_it_never_reaches_one_that_does_not(
    endpoint: FakeTokenEndpoint,
) -> None:
    """Two APIs with the same audience, scope and resource share nothing when only one opts in:
    the token kept for the one that allows a token naming no actor is not the other's."""
    actless = _jwt({"sub": "alice", "aud": "orders"})
    _issue(endpoint, actless)
    assert await _token("lenient_api", subject="alice", allow_actorless=True) == actless
    assert await _token("lenient_api", subject="alice", allow_actorless=True) == actless
    assert endpoint.calls == 1  # kept for the lenient API
    _issue(endpoint, actless)
    with pytest.raises(TokenExchangeError) as exc:
        await _token("strict_api", subject="alice")  # its own exchange, refused
    assert exc.value.outcome == "no_actor"
    assert endpoint.calls == 2
    # ... and the strict API's refusal does not reach the lenient one.
    assert await _token("lenient_api", subject="alice", allow_actorless=True) == actless
    assert endpoint.calls == 2


async def test_calls_sharing_an_exchange_all_get_the_refusal(endpoint: FakeTokenEndpoint) -> None:
    endpoint.delay = 0.05
    _issue(endpoint, _jwt({"sub": "alice", "aud": "orders"}))
    before = _count("sf_actorless", "no_actor")
    results = await asyncio.gather(
        *(_token("sf_actorless", subject="alice") for _ in range(20)), return_exceptions=True
    )
    assert endpoint.calls == 1
    assert all(isinstance(r, TokenExchangeError) and r.outcome == "no_actor" for r in results)
    assert _count("sf_actorless", "no_actor") - before == 20


@pytest.mark.parametrize(
    ("token", "claim"),
    [
        (_jwt({"sub": "alice", "act": {"sub": CLIENT_ID}}), None),  # the issuer names the agent
        (_jwt({"sub": "alice", "act": None}), None),  # present: the callee refuses it (401)
        (_jwt({"sub": "alice", "ext": {"actor": {"sub": CLIENT_ID}}}), "ext.actor"),
        (_jwt({"sub": "alice", "ext.actor": {"sub": CLIENT_ID}}), "ext.actor"),
    ],
)
async def test_a_token_that_names_its_actor_is_used_without_a_warning(
    endpoint: FakeTokenEndpoint,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    token: str,
    claim: str | None,
) -> None:
    if claim is not None:
        monkeypatch.setenv("AUTH_JWT_ACTOR_CLAIM", claim)
    else:
        monkeypatch.delenv("AUTH_JWT_ACTOR_CLAIM", raising=False)
    caplog.set_level(logging.DEBUG)
    _issue(endpoint, token)
    assert await _token() == token
    assert _no_actor_warnings(caplog) == []


async def test_a_failure_while_looking_at_the_token_refuses_it(
    endpoint: FakeTokenEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(token: str, claim: str = "act") -> bool:
        raise RuntimeError("a bug while reading the claims")

    monkeypatch.setattr(token_exchange, "names_no_actor", broken)
    _issue(endpoint, _jwt({"sub": "alice", "act": {"sub": CLIENT_ID}}))
    with pytest.raises(TokenExchangeError) as exc:
        await _token()
    assert exc.value.outcome == "no_actor"  # fail closed


def test_names_no_actor() -> None:
    assert names_no_actor(_jwt({"sub": "alice", "azp": "concierge"})) is True
    assert names_no_actor(_jwt({"sub": "alice", "act": {"sub": "concierge"}})) is False
    assert names_no_actor(_jwt({"sub": "alice", "act": None})) is False  # jwt refuses it (401)
    # AUTH_JWT_ACTOR_CLAIM's claim, a dotted path, read as jwt reads it.
    assert names_no_actor(_jwt({"act": {"sub": "concierge"}}), "ext.actor") is True
    assert names_no_actor(_jwt({"ext": {"actor": {"sub": "c"}}}), "ext.actor") is False
    assert names_no_actor(_jwt({"ext.actor": {"sub": "c"}}), "ext.actor") is False
    assert names_no_actor(_jwt({"ext": "flat"}), "ext.actor") is True
    # A token this agent cannot read names no actor as far as it can tell.
    for unreadable in ACTORLESS_TOKENS[1:]:
        assert names_no_actor(unreadable) is True


def test_the_actor_claim_is_the_jwt_policys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTH_JWT_ACTOR_CLAIM", raising=False)
    assert token_exchange.actor_claim() == "act"
    monkeypatch.setenv("AUTH_JWT_ACTOR_CLAIM", "")  # jwt's 0.2 reading: the caller still asks
    assert token_exchange.actor_claim() == "act"
    monkeypatch.setenv("AUTH_JWT_ACTOR_CLAIM", " ext.actor ")
    assert token_exchange.actor_claim() == "ext.actor"


async def test_the_no_actor_check_reads_the_configured_actor_claim(
    endpoint: FakeTokenEndpoint, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_JWT_ACTOR_CLAIM", "ext.actor")
    _issue(endpoint, _jwt({"sub": "alice", "act": {"sub": CLIENT_ID}}))
    with pytest.raises(TokenExchangeError) as exc:
        await _token()
    assert "(no ext.actor claim, or not a readable JWT)" in str(exc.value)
    caplog.set_level(logging.WARNING)
    _issue(endpoint, _jwt({"sub": "alice", "act": {"sub": CLIENT_ID}}))
    await _token("lenient_api", allow_actorless=True)
    [record] = _no_actor_warnings(caplog)
    assert "(no ext.actor claim)" in record.getMessage()


def test_settings_repr_hides_the_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKEN_EXCHANGE_CLIENT_SECRET", CLIENT_SECRET)
    assert CLIENT_SECRET not in repr(exchange_settings())


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "allowed"),
    [
        ({"TOKEN_EXCHANGE_URL": "https://issuer.example.com/token"}, True),
        ({"TOKEN_EXCHANGE_URL": "http://issuer.example.com/token"}, False),
        ({"TOKEN_EXCHANGE_URL": "http://issuer.example.com/token", "APP_ENV": "dev"}, True),
        ({"TOKEN_EXCHANGE_URL": "http://127.0.0.1:8080/token"}, True),
        ({"TOKEN_EXCHANGE_URL": "http://localhost/token"}, True),
        ({"TOKEN_EXCHANGE_URL": "http://[::1]:9/token"}, True),
        (
            {
                "TOKEN_EXCHANGE_URL": "http://keycloak.auth.svc/token",
                "TOKEN_EXCHANGE_ALLOW_HTTP": "true",
            },
            True,
        ),
        ({"TOKEN_EXCHANGE_URL": "ftp://issuer.example.com/token"}, False),
        ({"TOKEN_EXCHANGE_URL": "https://user:pw@issuer.example.com/token"}, False),
        ({"TOKEN_EXCHANGE_URL": "https://issuer.example.com/token#x"}, False),
    ],
)
def test_the_https_rule(env: dict[str, str], allowed: bool) -> None:
    if allowed:
        assert exchange_settings(env).url == env["TOKEN_EXCHANGE_URL"]
    else:
        with pytest.raises(SettingsError, match="TOKEN_EXCHANGE_URL"):
            exchange_settings(env)


@pytest.mark.parametrize(
    "env",
    [
        {"TOKEN_EXCHANGE_CLIENT_AUTH": "private_key_jwt"},
        {"TOKEN_EXCHANGE_SUBJECT_TOKEN_TYPE": "urn:ietf:params:oauth:token-type:id_token"},
        {"TOKEN_EXCHANGE_TIMEOUT_MS": "abc"},
        {"TOKEN_EXCHANGE_TIMEOUT_MS": "10"},
        {"TOKEN_EXCHANGE_MAX_TTL_S": "301"},
        {"TOKEN_EXCHANGE_MAX_TTL_S": "0"},
        {"TOKEN_EXCHANGE_FAILURE_TTL_S": "-1"},
        {"TOKEN_EXCHANGE_CACHE_MAX": "0"},
    ],
)
def test_a_bad_setting_is_a_settings_error(env: dict[str, str]) -> None:
    with pytest.raises(SettingsError, match=next(iter(env))):
        exchange_settings(env)


def test_the_defaults() -> None:
    settings = exchange_settings({})
    assert settings == ExchangeSettings()
    assert (settings.timeout_s, settings.max_ttl_s, settings.failure_ttl_s) == (2.0, 300, 10)
    assert settings.cache_max == 10_000 and settings.client_auth == "client_secret_basic"
    assert settings.missing() == [
        "TOKEN_EXCHANGE_URL",
        "TOKEN_EXCHANGE_CLIENT_ID",
        "TOKEN_EXCHANGE_CLIENT_SECRET",
    ]


# ---------------------------------------------------------------------------
# The compatibility matrix and the startup checks
# ---------------------------------------------------------------------------

EXCHANGE_API = {"auth": "exchange", "exchange": {"audience": "orders"}}
FORWARD_API = {"auth": "forward"}
AIMED_FORWARD_API = {"auth": "forward", "forward_audience": "orders"}


@pytest.mark.parametrize(
    ("apis", "auth_policy", "runtime", "problem", "warning"),
    [
        ({"p": EXCHANGE_API}, "jwt", "fastapi", None, None),
        ({"p": EXCHANGE_API}, "custom", "fastapi", None, None),
        ({"p": EXCHANGE_API}, "shared-bearer", "fastapi", "shared-bearer has no user token", None),
        ({"p": EXCHANGE_API}, "jwt", "langgraph-server", "persists the run context", None),
        ({"p": FORWARD_API}, "custom", "fastapi", None, None),
        ({"p": AIMED_FORWARD_API}, "jwt", "fastapi", None, None),
        ({"p": FORWARD_API}, "jwt", "fastapi", None, "without forward_audience"),
        ({"p": FORWARD_API}, "shared-bearer", "fastapi", None, "no user credential to forward"),
        ({"p": FORWARD_API}, "custom", "langgraph-server", None, "carries no credentials"),
        (
            {"p": {"auth": "bearer"}, "q": {"auth": "none"}},
            "shared-bearer",
            "langgraph-server",
            None,
            None,
        ),
    ],
)
def test_the_compatibility_matrix(
    apis: dict[str, Any], auth_policy: str, runtime: str, problem: str | None, warning: str | None
) -> None:
    problems, warnings = compatibility(auth_policy, runtime, apis)
    assert [problem in p for p in problems] == ([True] if problem else [])
    assert [warning in w for w in warnings] == ([True] if warning else [])


@pytest.fixture
def policy_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "api-policy.yaml"
    path.write_text(POLICY, encoding="utf-8")
    monkeypatch.setenv("API_POLICY_PATH", str(path))
    for name in ("RUNTIME", "LANGGRAPH_SERVER", "LANGSERVE_GRAPHS", "APP_ENV"):
        monkeypatch.delenv(name, raising=False)
    reset_policy_cache()
    yield path
    reset_policy_cache()


def test_startup_problems_name_the_missing_settings(
    policy_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("TOKEN_EXCHANGE_URL", "TOKEN_EXCHANGE_CLIENT_ID", "TOKEN_EXCHANGE_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    [problem] = startup_problems("jwt")
    assert problem.startswith("auth: exchange (apis: orders_agent, billing_agent) needs ")
    assert "TOKEN_EXCHANGE_URL, TOKEN_EXCHANGE_CLIENT_ID, TOKEN_EXCHANGE_CLIENT_SECRET" in problem
    monkeypatch.setenv("TOKEN_EXCHANGE_URL", TOKEN_URL)
    monkeypatch.setenv("TOKEN_EXCHANGE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("TOKEN_EXCHANGE_CLIENT_SECRET", CLIENT_SECRET)
    assert startup_problems("jwt") == []
    assert len(startup_problems("shared-bearer")) == 1
    monkeypatch.setenv("RUNTIME", "langgraph-server")
    assert "persists the run context" in startup_problems("jwt")[0]


def test_check_startup_refuses_outside_dev_and_logs_under_dev(
    policy_env: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from {{cookiecutter.agent_directory}}.app_utils import auth

    monkeypatch.setenv("AUTH_POLICY", "shared-bearer")
    monkeypatch.setenv("API_KEY", "k")
    for name in ("TOKEN_EXCHANGE_URL", "TOKEN_EXCHANGE_CLIENT_ID", "TOKEN_EXCHANGE_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    auth.reset_policy_cache()
    try:
        with pytest.raises(RuntimeError) as exc:
            auth.check_startup()
        assert str(exc.value).startswith(
            "api-policy.yaml cannot work with this configuration, refusing to start: "
        )
        assert "shared-bearer has no user token to exchange" in str(exc.value)
        assert "needs TOKEN_EXCHANGE_URL" in str(exc.value)
        monkeypatch.setenv("APP_ENV", "dev")
        with caplog.at_level(logging.ERROR):
            auth.check_startup()
        assert "APP_ENV=dev: starting anyway" in caplog.text
    finally:
        auth.reset_policy_cache()


def test_a_forward_api_that_cannot_work_is_only_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """0.2 projects keep starting: such an API's calls already fail with 'no credential'."""
    from {{cookiecutter.agent_directory}}.app_utils import auth

    path = tmp_path / "api-policy.yaml"
    path.write_text(
        "apis:\n  me:\n    base_url_env: ME_URL\n    auth: forward\n    allowed_methods: [GET]\n"
    )
    monkeypatch.setenv("API_POLICY_PATH", str(path))
    monkeypatch.setenv("AUTH_POLICY", "shared-bearer")
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.delenv("APP_ENV", raising=False)
    reset_policy_cache()
    auth.reset_policy_cache()
    try:
        with caplog.at_level(logging.WARNING):
            auth.check_startup()
        assert "no user credential to forward" in caplog.text
    finally:
        auth.reset_policy_cache()
        reset_policy_cache()


# ---------------------------------------------------------------------------
# `auth: exchange` calls (api_client)
# ---------------------------------------------------------------------------


@dataclass
class _Context:
    principal_id: str = "alice"
    attributes: dict[str, Any] = field(default_factory=dict)


def _context(subject: str | None = SUBJECT, chain: tuple[str, ...] = (), **extra: Any) -> _Context:
    credentials: dict[str, Any] = {}
    if subject is not None:
        credentials = {
            "@subject_token": subject,
            "@subject_aud": ("concierge",),
            "@subject_exp": WALL_START + 3600,
        }
    attributes: dict[str, Any] = {"credentials": credentials, **extra}
    if chain:
        attributes["@actor"] = {"id": chain[0], "chain": list(chain), "client": chain[0]}
    return _Context(attributes=attributes)


class Api:
    """The APIs the exchanged token goes to: records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            # An upstream that echoes the credential it was sent back in its error body.
            return httpx.Response(
                self.status, text=f"denied: {request.headers.get('authorization')}"
            )
        return httpx.Response(200, json={"ok": True})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def api(policy_env: Path, endpoint: FakeTokenEndpoint, monkeypatch: pytest.MonkeyPatch) -> Api:
    monkeypatch.setenv("ORDERS_AGENT_URL", "https://orders.test")
    monkeypatch.setenv("BILLING_AGENT_URL", "https://billing.test")
    monkeypatch.setenv("WEATHER_URL", "https://weather.test")
    monkeypatch.setenv("WEATHER_TOKEN", "weather-key")
    reset_limits()
    set_outbound_headers(None)
    return Api()


async def test_an_exchange_api_gets_a_token_minted_for_its_audience(
    api: Api, endpoint: FakeTokenEndpoint
) -> None:
    orders = get_client("orders_agent", context=_context(), transport=api.transport)
    assert await orders.get("/orders", operation_id="listOrders") == {"ok": True}
    [sent] = api.requests
    assert sent.headers["authorization"] == f"Bearer {exchanged(1, 'orders')}"
    [asked] = endpoint.requests
    assert asked["form"]["audience"] == "orders"
    assert asked["form"]["scope"] == "orders.read orders.cancel"
    assert asked["form"]["resource"] == "https://orders.example.com"
    assert asked["form"]["subject_token"] == SUBJECT
    billing = get_client("billing_agent", context=_context(), transport=api.transport)
    await billing.get("/balance")
    assert api.requests[1].headers["x-agent-token"] == f"Bearer {exchanged(2, 'billing')}"
    assert "authorization" not in api.requests[1].headers
    await orders.get("/orders")  # kept: no new exchange
    assert endpoint.calls == 2 and len(api.requests) == 3


async def test_the_tool_cannot_set_the_credential_header(api: Api) -> None:
    orders = get_client("orders_agent", context=_context(), transport=api.transport)
    prepared = orders._prepare(
        "GET", "/orders", None, None, None, None, {"Authorization": "Bearer forged", "X-Tool": "1"}
    )
    assert prepared.tool_headers == [("x-tool", "1")]
    assert "authorization" not in prepared.headers
    await orders.get("/orders", headers={"Authorization": "Bearer forged"})
    assert api.requests[0].headers["authorization"] == f"Bearer {exchanged(1, 'orders')}"


async def test_no_user_token_sends_nothing(api: Api, endpoint: FakeTokenEndpoint) -> None:
    orders = get_client("orders_agent", context=_context(subject=None), transport=api.transport)
    with pytest.raises(ApiCallError) as exc:
        await orders.get("/orders")
    assert str(exc.value) == (
        "API 'orders_agent' uses auth: exchange, but this run has no user token to exchange "
        "(shared-bearer, or a run resumed by another principal); nothing was sent."
    )
    assert endpoint.calls == 0 and api.requests == []


async def test_no_exchange_on_a_refused_call(api: Api, endpoint: FakeTokenEndpoint) -> None:
    orders = get_client("orders_agent", context=_context(), transport=api.transport)
    with pytest.raises(ApiPolicyError, match="not in allowed_methods"):
        await orders.delete("/orders/7")
    with pytest.raises(ApiPolicyError, match=r"only inside an agent run"):
        # Gated, and outside a run nothing can pause it: refused before any exchange.
        await orders.post(
            "/orders/{order_id}/cancel", operation_id="cancelOrder", path_params={"order_id": "7"}
        )
    assert endpoint.calls == 0
    for _ in range(3):
        await orders.get("/orders")
    with pytest.raises(ApiPolicyError, match="max_calls_per_run"):
        await orders.get("/orders")
    assert endpoint.calls == 1 and len(api.requests) == 3


async def test_no_exchange_while_paused_and_one_after_the_approval(
    api: Api, endpoint: FakeTokenEndpoint
) -> None:
    """The exchange runs after the gate: none while a person decides, one once they approve."""
    from typing import TypedDict

    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command

    class Ledger:
        def __init__(self) -> None:
            self.used: list[str] = []

        async def consume(self, approval_id: str, call_hash: str, thread_id: Any = None) -> None:
            self.used.append(approval_id)

        async def bound_approvals(self, **kwargs: Any) -> list[BoundApproval]:
            return []

    class State(TypedDict):
        result: str

    async def node(state: State) -> dict[str, str]:
        client = get_client("orders_agent", context=_context(), transport=api.transport)
        data = await client.post(
            "/orders/{order_id}/cancel", operation_id="cancelOrder", path_params={"order_id": "7"}
        )
        return {"result": str(data)}

    builder = StateGraph(State)
    builder.add_node("cancel", node)
    builder.add_edge(START, "cancel")
    builder.add_edge("cancel", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t1"}}
    ledger = Ledger()
    set_approval_ledger(ledger)
    try:
        await graph.ainvoke({"result": ""}, config=config)
        [interrupt] = (await graph.aget_state(config)).interrupts
        assert interrupt.value["path"] == "/orders/7/cancel"
        assert endpoint.calls == 0 and api.requests == []  # paused: nothing exchanged
        decision = {
            "type": APPROVAL_DECISION,
            "decision": "approve",
            "approval_id": "a1",
            "api": "orders_agent",
            "method": "POST",
            "path": "/orders/7/cancel",
            "call_hash": interrupt.value["call_hash"],
            "approvers": interrupt.value["approvers"],
            "decide_with": "direct",
            "relayers": [],
        }
        done = await graph.ainvoke(Command(resume={interrupt.id: decision}), config=config)
        assert done["result"] == "{'ok': True}"
        assert endpoint.calls == 1 and ledger.used == ["a1"]
        [sent] = api.requests
        assert sent.headers["authorization"] == f"Bearer {exchanged(1, 'orders')}"
    finally:
        set_approval_ledger(None)


async def test_the_exchanged_token_is_redacted_in_error_bodies(api: Api) -> None:
    api.status = 403
    orders = get_client("orders_agent", context=_context(), transport=api.transport)
    with pytest.raises(ApiCallError) as exc:
        await orders.get("/orders")
    assert exc.value.status_code == 403
    assert exc.value.body == "denied: <redacted>"
    assert exchanged(1, "orders") not in str(exc.value)


async def test_an_issuer_refusal_sends_nothing(
    api: Api, endpoint: FakeTokenEndpoint, caplog: pytest.LogCaptureFixture
) -> None:
    endpoint.answers.append(httpx.Response(400, json={"error": "invalid_target"}))
    orders = get_client("orders_agent", context=_context(), transport=api.transport)
    with caplog.at_level(logging.WARNING), pytest.raises(ApiCallError) as exc:
        await orders.get("/orders")
    assert str(exc.value) == (
        "token exchange for API 'orders_agent' was refused (invalid_target); nothing was sent."
    )
    assert "api call not sent: orders_agent GET <concrete path>: token exchange refused" in (
        caplog.text
    )
    assert api.requests == []


async def test_a_token_naming_no_actor_sends_nothing(
    api: Api, endpoint: FakeTokenEndpoint, caplog: pytest.LogCaptureFixture
) -> None:
    _issue(endpoint, _jwt({"sub": "alice", "aud": "orders", "azp": CLIENT_ID}))
    orders = get_client("orders_agent", context=_context(), transport=api.transport)
    with caplog.at_level(logging.WARNING), pytest.raises(ApiCallError) as exc:
        await orders.get("/orders")
    assert str(exc.value).startswith(
        "token exchange for API 'orders_agent': the issuer's token names no actor"
    )
    not_sent = "api call not sent: orders_agent GET <concrete path>: exchanged token names no actor"
    assert not_sent in caplog.text
    assert api.requests == []


async def test_an_api_that_allows_it_is_sent_a_token_naming_no_actor(
    api: Api, endpoint: FakeTokenEndpoint
) -> None:
    """billing_agent sets `exchange.allow_actorless: true` (its agent sets AUTH_JWT_DIRECT_CLIENTS)."""
    actless = _jwt({"sub": "alice", "aud": "billing", "azp": CLIENT_ID})
    _issue(endpoint, actless)
    billing = get_client("billing_agent", context=_context(), transport=api.transport)
    assert await billing.get("/balance") == {"ok": True}
    assert api.requests[0].headers["x-agent-token"] == f"Bearer {actless}"


async def test_trace_headers_go_to_exchange_apis(api: Api) -> None:
    """The owner's decision (2026-09-28): `auth: exchange` APIs receive them, `bearer` ones not."""
    set_outbound_headers(
        lambda: {"X-Request-ID": "req-7", "traceparent": "00-" + "1" * 32 + "-" + "2" * 16 + "-01"}
    )
    try:
        orders = get_client("orders_agent", context=_context(), transport=api.transport)
        await orders.get("/orders")
        weather = get_client("weather", transport=api.transport)
        await weather.get("/today")
    finally:
        set_outbound_headers(None)
    to_orders, to_weather = api.requests
    assert to_orders.headers["x-request-id"] == "req-7" and "traceparent" in to_orders.headers
    assert "x-request-id" not in to_weather.headers and "traceparent" not in to_weather.headers


# ---------------------------------------------------------------------------
# Loops
# ---------------------------------------------------------------------------


def test_loop_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A2A_NAME", "billing")
    monkeypatch.setenv("AUTH_JWT_AUDIENCE", "billing-api, billing.example.com")
    assert loop_problem("orders", ("concierge",)) is None
    assert loop_problem("billing", ()) == "calling billing would call this agent itself (billing)"
    assert loop_problem("billing.example.com", ()) is not None
    assert loop_problem("concierge", ("concierge",)) == (
        "calling concierge would loop back through the delegation chain "
        "(concierge -> billing -> concierge)"
    )
    assert loop_problem("concierge", ("client:concierge",)) is not None  # an issuer without act
    assert loop_problem("gateway", ("concierge", "gateway")) == (
        "calling gateway would loop back through the delegation chain "
        "(gateway -> concierge -> billing -> gateway)"
    )


async def test_a_call_that_would_loop_is_refused_before_anything(
    api: Api, endpoint: FakeTokenEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    looping = get_client(
        "orders_agent", context=_context(chain=("orders",)), transport=api.transport
    )
    with pytest.raises(ApiPolicyError) as exc:
        await looping.post(
            "/orders/{order_id}/cancel", operation_id="cancelOrder", path_params={"order_id": "7"}
        )
    assert "would loop back through the delegation chain" in str(exc.value)
    monkeypatch.setenv("A2A_NAME", "orders")
    itself = get_client("orders_agent", context=_context(), transport=api.transport)
    with pytest.raises(ApiPolicyError, match="would call this agent itself"):
        await itself.get("/orders")
    assert endpoint.calls == 0 and api.requests == []
    monkeypatch.delenv("A2A_NAME")
    other = get_client(
        "orders_agent", context=_context(chain=("billing",)), transport=api.transport
    )
    await other.get("/orders")
    assert endpoint.calls == 1


def test_the_exchange_module_is_the_one_the_client_uses() -> None:
    assert api_client.get_client.__module__ == api_client.__name__
    assert token_exchange.exchanger() is exchanger()
