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

"""The `jwt` auth policy: verification, key handling, settings and both runtimes' wiring.

Keys are generated locally (RSA, EC P-256 and P-384, Ed25519); the JWKS is
served by a throwaway HTTP server on 127.0.0.1. No other network access.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from fastapi import HTTPException
from starlette.requests import Request

from {{cookiecutter.agent_directory}}.app_utils.auth import (
    ACTIONS,
    JwksCache,
    JwksUnavailable,
    JwtPolicy,
    JwtSettings,
    build_sdk_auth,
    check_startup,
    get_policy,
    require,
    reset_policy_cache,
)

ISSUER = "https://issuer.test/"
AUDIENCE = "agent-api"


# --- keys, tokens and requests -------------------------------------------------


def _rsa() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


RSA_A = _rsa()
RSA_B = _rsa()
EC_P256 = ec.generate_private_key(ec.SECP256R1())
EC_P384 = ec.generate_private_key(ec.SECP384R1())
ED25519 = ed25519.Ed25519PrivateKey.generate()


def _pem(private_key: Any) -> str:
    return (
        private_key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


def _jwk(private_key: Any, kid: str, **extra: Any) -> dict[str, Any]:
    public = private_key.public_key()
    if isinstance(public, rsa.RSAPublicKey):
        data = jwt.algorithms.RSAAlgorithm.to_jwk(public, as_dict=True)
    elif isinstance(public, ec.EllipticCurvePublicKey):
        data = jwt.algorithms.ECAlgorithm.to_jwk(public, as_dict=True)
    else:
        data = jwt.algorithms.OKPAlgorithm.to_jwk(public, as_dict=True)
    return {**data, "kid": kid, "use": "sig", **extra}


def _claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": "user-1",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "nbf": now,
        "exp": now + 300,
        "roles": ["viewer", "support"],
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def _token(key: Any, alg: str = "RS256", kid: str | None = "a", **claims: Any) -> str:
    headers = {"kid": kid} if kid is not None else None
    return jwt.encode(_claims(**claims), key, algorithm=alg, headers=headers)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _forge(header: dict[str, Any], claims: dict[str, Any], secret: bytes | None = None) -> str:
    """A token signed with HMAC-SHA256 over `secret` (or unsigned when None)."""
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    if secret is None:
        return signing_input + "."
    signature = hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(signature)}"


def _request(token: str | None = None, scheme: str = "Bearer") -> Request:
    headers = [(b"authorization", f"{scheme} {token}".encode())] if token is not None else []
    return Request(
        {"type": "http", "method": "POST", "path": "/chat", "headers": headers, "query_string": b""}
    )


def _settings(**env: str) -> JwtSettings:
    base = {
        "APP_ENV": "prod",
        "AUTH_JWT_ISSUER": ISSUER,
        "AUTH_JWT_AUDIENCE": AUDIENCE,
        "AUTH_JWT_PUBLIC_KEY": _pem(RSA_A),
    }
    base.update(env)
    return JwtSettings.from_env({k: v for k, v in base.items() if v is not None})


async def _reject(policy: JwtPolicy, token: str | None, reason: str | None = None) -> HTTPException:
    with pytest.raises(HTTPException) as exc:
        await policy.authenticate(_request(token))
    assert exc.value.status_code == 401, exc.value.detail
    assert exc.value.headers and exc.value.headers["WWW-Authenticate"].startswith("Bearer")
    if reason is not None:
        assert reason in str(exc.value.detail), exc.value.detail
    return exc.value


# --- a JWKS served from a local HTTP server ----------------------------------


class JwksServer:
    def __init__(self) -> None:
        self.keys: list[dict[str, Any]] = []
        self.status = 200
        self.body: bytes | None = None
        self.hits = 0
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                server.hits += 1
                body = server.body or json.dumps({"keys": server.keys}).encode()
                self.send_response(server.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        class QuietServer(ThreadingHTTPServer):
            def handle_error(self, request: Any, client_address: Any) -> None:
                pass  # a client that stops reading an oversized body is expected

        self.httpd = QuietServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/.well-known/jwks.json"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def jwks() -> Iterator[JwksServer]:
    server = JwksServer()
    server.keys = [_jwk(RSA_A, "a")]
    try:
        yield server
    finally:
        server.close()


def _jwks_policy(server: JwksServer, **env: str) -> JwtPolicy:
    return JwtPolicy(
        _settings(AUTH_JWT_PUBLIC_KEY=None, AUTH_JWT_JWKS_URL=server.url, **env)  # type: ignore[arg-type]
    )


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# --- verification ---------------------------------------------------------------


async def test_rs256_token_from_a_jwks_gives_the_principal_and_roles(jwks: JwksServer) -> None:
    policy = _jwks_policy(jwks)
    assert policy.startup_problems() == []
    principal = await policy.authenticate(_request(_token(RSA_A)))
    assert principal.id == "user-1"
    assert principal.roles == ["viewer", "support"]
    assert principal.permissions == set(ACTIONS)
    assert principal.attributes == {}
    for action in ACTIONS:
        await policy.authorize(principal, action, None)
    with pytest.raises(HTTPException) as exc:
        await policy.authorize(principal, "thread.export", None)
    assert exc.value.status_code == 403


async def test_ec_and_eddsa_tokens_verify_against_a_static_pem_key() -> None:
    es = JwtPolicy(_settings(AUTH_JWT_PUBLIC_KEY=_pem(EC_P256), AUTH_JWT_ALGORITHMS="ES256"))
    assert (await es.authenticate(_request(_token(EC_P256, "ES256")))).id == "user-1"
    ed = JwtPolicy(_settings(AUTH_JWT_PUBLIC_KEY=_pem(ED25519), AUTH_JWT_ALGORITHMS="EdDSA"))
    assert (await ed.authenticate(_request(_token(ED25519, "EdDSA")))).id == "user-1"
    # The default allow-list is RS256,ES256; a PEM with escaped newlines (as
    # some secret stores deliver it) is accepted too.
    escaped = _pem(RSA_A).replace("\n", "\\n")
    rs = JwtPolicy(_settings(AUTH_JWT_PUBLIC_KEY=escaped))
    assert (await rs.authenticate(_request(_token(RSA_A)))).id == "user-1"


async def test_ec_token_from_a_jwks(jwks: JwksServer) -> None:
    jwks.keys = [_jwk(RSA_A, "a"), _jwk(EC_P256, "e1")]
    policy = _jwks_policy(jwks)
    assert (await policy.authenticate(_request(_token(EC_P256, "ES256", kid="e1")))).id == "user-1"


@pytest.mark.parametrize(
    ("claims", "reason"),
    [
        ({"exp": int(time.time()) - 3600}, "token expired"),
        ({"nbf": int(time.time()) + 3600}, "token not yet valid"),
        ({"iat": int(time.time()) + 3600}, "token not yet valid"),
        ({"aud": "someone-else"}, "wrong audience"),
        ({"aud": None}, "missing required claim"),
        ({"iss": "https://evil.test/"}, "wrong issuer"),
        ({"iss": None}, "missing required claim"),
        ({"exp": None}, "missing required claim"),
        ({"sub": None}, "missing principal claim"),
        ({"sub": ""}, "invalid principal claim"),
        ({"sub": "a\x00b"}, "invalid principal claim"),
        ({"sub": "x" * 300}, "invalid principal claim"),
        ({"sub": True}, "invalid token"),  # PyJWT: `sub` must be a string
    ],
)
async def test_claim_checks_reject_with_a_bearer_challenge(
    claims: dict[str, Any], reason: str
) -> None:
    policy = JwtPolicy(_settings())
    error = await _reject(policy, _token(RSA_A, **claims), reason)
    assert 'error="invalid_token"' in error.headers["WWW-Authenticate"]


async def test_leeway_accepts_small_clock_skew() -> None:
    policy = JwtPolicy(_settings(AUTH_JWT_LEEWAY_S="120"))
    now = int(time.time())
    token = _token(RSA_A, exp=now - 60, nbf=now + 60, iat=now + 60)
    assert (await policy.authenticate(_request(token))).id == "user-1"
    strict = JwtPolicy(_settings(AUTH_JWT_LEEWAY_S="0"))
    await _reject(strict, token)


async def test_several_audiences_and_an_audience_list_in_the_token() -> None:
    policy = JwtPolicy(_settings(AUTH_JWT_AUDIENCE="other, agent-api"))
    token = _token(RSA_A, aud=["unrelated", AUDIENCE])
    assert (await policy.authenticate(_request(token))).id == "user-1"


async def test_missing_or_non_bearer_credentials_get_a_plain_challenge() -> None:
    policy = JwtPolicy(_settings())
    for request in (_request(None), _request(_token(RSA_A), scheme="Basic"), _request("")):
        with pytest.raises(HTTPException) as exc:
            await policy.authenticate(request)
        assert exc.value.status_code == 401
        assert exc.value.headers == {"WWW-Authenticate": "Bearer"}


@pytest.mark.parametrize(
    ("token", "reason"),
    [
        ("not-a-jwt", "malformed token"),
        ("a.b.c", "malformed token"),
        ("x" * 20_000, "token too large"),
    ],
)
async def test_malformed_tokens(token: str, reason: str) -> None:
    await _reject(JwtPolicy(_settings()), token, reason)


# --- algorithm confusion ------------------------------------------------------------


async def test_alg_none_is_never_accepted() -> None:
    policy = JwtPolicy(_settings())
    for alg in ("none", "None", "NONE"):
        await _reject(policy, _forge({"alg": alg, "kid": "a"}, _claims()), "algorithm not allowed")
    # ...not even when the allow-list tries to include it.
    settings = _settings(AUTH_JWT_ALGORITHMS="RS256,none")
    assert any("never include 'none'" in p for p in settings.problems)


async def test_hs256_signed_with_the_public_key_is_refused() -> None:
    """The classic confusion: HMAC over the (public) PEM text the server trusts."""
    pem = _pem(RSA_A).encode()
    forged = _forge({"alg": "HS256", "typ": "JWT", "kid": "a"}, _claims(), secret=pem)
    await _reject(JwtPolicy(_settings()), forged, "algorithm not allowed")
    # Even with HS256 allowed, a token is checked with AUTH_JWT_SECRET only.
    secret = "s" * 40
    policy = JwtPolicy(
        _settings(
            AUTH_JWT_ALGORITHMS="RS256,HS256", AUTH_JWT_ALLOW_HS="true", AUTH_JWT_SECRET=secret
        )
    )
    assert policy.startup_problems() == []
    await _reject(policy, forged, "invalid signature")
    assert (await policy.authenticate(_request(_token(secret, "HS256")))).id == "user-1"


async def test_a_token_is_only_checked_with_a_key_of_its_own_family(jwks: JwksServer) -> None:
    # RS256 token, but only ES256 is allowed.
    es_only = JwtPolicy(_settings(AUTH_JWT_PUBLIC_KEY=_pem(EC_P256), AUTH_JWT_ALGORITHMS="ES256"))
    await _reject(es_only, _token(RSA_A), "algorithm not allowed")
    # ES256 header over an EC key on the wrong curve.
    both = JwtPolicy(
        _settings(AUTH_JWT_PUBLIC_KEY=_pem(EC_P384), AUTH_JWT_ALGORITHMS="ES256,ES384")
    )
    await _reject(both, _token(EC_P256, "ES256"))
    # A JWKS entry whose kid matches but whose type does not.
    jwks.keys = [_jwk(RSA_A, "shared-kid")]
    policy = _jwks_policy(jwks)
    await _reject(policy, _token(EC_P256, "ES256", kid="shared-kid"), "unknown signing key")
    # A JWK pinned to RS512 does not verify an RS256 token.
    jwks.keys = [_jwk(RSA_A, "a", alg="RS512")]
    await _reject(_jwks_policy(jwks), _token(RSA_A), "unknown signing key")


async def test_symmetric_and_encryption_keys_in_a_jwks_are_ignored(jwks: JwksServer) -> None:
    secret = b"k" * 40
    jwks.keys = [
        {"kty": "oct", "kid": "sym", "k": _b64(secret), "alg": "HS256"},
        _jwk(RSA_A, "enc", use="enc"),
        _jwk(RSA_A, "a"),
    ]
    policy = _jwks_policy(jwks, AUTH_JWT_ALGORITHMS="RS256")
    forged = _forge({"alg": "HS256", "kid": "sym"}, _claims(), secret=secret)
    await _reject(policy, forged, "algorithm not allowed")
    await _reject(policy, _token(RSA_A, kid="enc"), "unknown signing key")
    assert (await policy.authenticate(_request(_token(RSA_A, kid="a")))).id == "user-1"


async def test_keys_named_or_embedded_in_the_token_are_never_used(jwks: JwksServer) -> None:
    attacker = _rsa()
    policy = _jwks_policy(jwks)
    for header in (
        {"kid": "a", "jwk": _jwk(attacker, "a")},
        {"kid": "a", "jku": "https://evil.test/jwks.json"},
        {"kid": "a", "x5u": "https://evil.test/cert.pem"},
    ):
        token = jwt.encode(_claims(), attacker, algorithm="RS256", headers=header)
        await _reject(policy, token, "invalid signature")
    await _reject(policy, _token(attacker, kid="../../etc/passwd"), "unknown signing key")
    assert jwks.hits == 1  # the attacker's URLs were never fetched


async def test_a_redirecting_jwks_url_is_not_followed(jwks: JwksServer) -> None:
    jwks.status = 302
    with pytest.raises(HTTPException) as exc:
        await _jwks_policy(jwks).authenticate(_request(_token(RSA_A)))
    assert exc.value.status_code == 503


async def test_unsupported_critical_header_is_refused() -> None:
    token = jwt.encode(_claims(), RSA_A, algorithm="RS256", headers={"kid": "a", "crit": ["exp"]})
    # Refused whichever layer notices first (PyJWT's header check or the policy's).
    await _reject(JwtPolicy(_settings()), token)


# --- JWKS caching and rotation --------------------------------------------------


async def test_the_jwks_is_fetched_once_and_cached(jwks: JwksServer) -> None:
    policy = _jwks_policy(jwks)
    for _ in range(5):
        await policy.authenticate(_request(_token(RSA_A)))
    assert jwks.hits == 1


async def test_an_unknown_kid_refetches_once_and_picks_up_a_rotated_key(jwks: JwksServer) -> None:
    clock = FakeClock()
    policy = _jwks_policy(jwks)
    policy.jwks = JwksCache(jwks.url, 300, clock=clock)
    await policy.authenticate(_request(_token(RSA_A)))
    assert jwks.hits == 1
    # The issuer rotates to key b; a token signed with b triggers one refetch.
    jwks.keys = [_jwk(RSA_A, "a"), _jwk(RSA_B, "b")]
    clock.now += 31
    assert (await policy.authenticate(_request(_token(RSA_B, kid="b")))).id == "user-1"
    assert jwks.hits == 2
    # Unknown kids right after that do not reach the issuer (rate limit).
    for kid in ("x1", "x2", "x3"):
        await _reject(policy, _token(RSA_B, kid=kid), "unknown signing key")
    assert jwks.hits == 2
    # After the interval, one more refetch is allowed.
    clock.now += 31
    await _reject(policy, _token(RSA_B, kid="x4"), "unknown signing key")
    assert jwks.hits == 3


async def test_concurrent_tokens_with_a_rotated_key_share_one_refetch(jwks: JwksServer) -> None:
    """Every request carrying the new key id waits for the one refetch and uses its result."""
    clock = FakeClock()
    policy = _jwks_policy(jwks)
    policy.jwks = JwksCache(jwks.url, 300, clock=clock)
    await policy.authenticate(_request(_token(RSA_A)))
    jwks.keys = [_jwk(RSA_A, "a"), _jwk(RSA_B, "b")]
    clock.now += 31
    token = _token(RSA_B, kid="b")
    results = await asyncio.gather(
        *(policy.authenticate(_request(token)) for _ in range(8)), return_exceptions=True
    )
    assert [getattr(r, "id", r) for r in results] == ["user-1"] * 8
    assert jwks.hits == 2
    # Random unknown key ids at once still cost the issuer at most one fetch.
    clock.now += 31
    results = await asyncio.gather(
        *(policy.authenticate(_request(_token(RSA_B, kid=f"x{i}"))) for i in range(8)),
        return_exceptions=True,
    )
    assert all(isinstance(r, HTTPException) and r.status_code == 401 for r in results)
    assert jwks.hits == 3


async def test_a_hanging_issuer_never_stalls_requests_the_cached_keys_can_verify() -> None:
    """Stale while revalidate: expired keys keep serving while one bounded refresh runs."""
    clock = FakeClock()
    cache = JwksCache(
        "https://issuer.test/jwks", 300, timeout_s=0.1, stale_grace_s=600, clock=clock
    )
    fetches = 0

    async def fetch() -> list[dict[str, Any]]:
        nonlocal fetches
        fetches += 1
        if fetches == 1:
            return [_jwk(RSA_A, "a")]
        await asyncio.sleep(3600)  # the issuer stops answering
        return []

    cache._fetch = fetch  # type: ignore[method-assign]
    assert [k["kid"] for k in await cache.keys()] == ["a"]
    clock.now += 301  # expired, within the grace period
    started = time.monotonic()
    for _ in range(20):
        assert [k["kid"] for k in await cache.keys()] == ["a"]
    assert time.monotonic() - started < 0.05  # none of them waited for the issuer
    await asyncio.sleep(0)
    assert fetches == 2  # one background refresh, not one per request
    await cache.idle()  # it gives up at its deadline (2 x timeout_s)
    assert [k["kid"] for k in await cache.keys()] == ["a"]
    assert fetches == 2  # rate-limited: no new fetch yet, the cached keys still serve
    clock.now += 600  # past the grace period: requests now wait for a (bounded) fetch
    started = time.monotonic()
    with pytest.raises(JwksUnavailable):
        await cache.keys()
    assert 0.15 < time.monotonic() - started < 2 and fetches == 3


async def test_a_token_without_kid_needs_an_unambiguous_key(jwks: JwksServer) -> None:
    policy = _jwks_policy(jwks)
    assert (await policy.authenticate(_request(_token(RSA_A, kid=None)))).id == "user-1"
    jwks.keys = [_jwk(RSA_A, "a"), _jwk(RSA_B, "b")]
    other = _jwks_policy(jwks)
    await _reject(other, _token(RSA_A, kid=None), "unknown signing key")


async def test_an_unreachable_jwks_serves_stale_keys_then_fails_closed(jwks: JwksServer) -> None:
    clock = FakeClock()
    policy = _jwks_policy(jwks)
    policy.jwks = JwksCache(jwks.url, 300, stale_grace_s=600, clock=clock)
    await policy.authenticate(_request(_token(RSA_A)))
    jwks.status = 500
    clock.now += 400  # cache expired, refresh fails: the last good keys still verify
    assert (await policy.authenticate(_request(_token(RSA_A)))).id == "user-1"
    await policy.jwks.idle()  # the refresh ran in the background
    assert jwks.hits == 2
    assert (await policy.authenticate(_request(_token(RSA_A)))).id == "user-1"
    clock.now += 600  # beyond the grace period: 503, never "no auth"
    with pytest.raises(HTTPException) as exc:
        await policy.authenticate(_request(_token(RSA_A)))
    assert exc.value.status_code == 503
    jwks.status = 200
    clock.now += 31
    assert (await policy.authenticate(_request(_token(RSA_A)))).id == "user-1"


async def test_a_stalled_fetch_has_a_deadline_and_a_waiter_leaving_does_not_cancel_it() -> None:
    async def stall() -> list[dict[str, Any]]:
        await asyncio.sleep(3600)
        return []

    cache = JwksCache("https://issuer.test/jwks", 300, timeout_s=0.05)
    cache._fetch = stall  # type: ignore[method-assign]
    with pytest.raises(JwksUnavailable):
        await cache.keys()  # gives up after 2 x timeout_s
    shared = JwksCache("https://issuer.test/jwks", 300)
    fetches = 0

    async def slow() -> list[dict[str, Any]]:
        nonlocal fetches
        fetches += 1
        await asyncio.sleep(0.1)
        return [_jwk(RSA_A, "a")]

    shared._fetch = slow  # type: ignore[method-assign]
    leaving = asyncio.ensure_future(shared.keys())
    await asyncio.sleep(0.02)
    leaving.cancel()  # the client went away
    with pytest.raises(asyncio.CancelledError):
        await leaving
    # The fetch carries on for the requests still waiting, which share it.
    assert [k["kid"] for k in await shared.keys()] == ["a"] and fetches == 1


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b'{"keys": "nope"}',
        b'{"keys": []}',
        b"[]",
        b'{"keys": [' + b"1," * 600_000 + b"1]}",
    ],
)
async def test_a_bad_jwks_response_is_not_used(jwks: JwksServer, body: bytes) -> None:
    jwks.body = body
    policy = _jwks_policy(jwks)
    with pytest.raises(HTTPException) as exc:
        await policy.authenticate(_request(_token(RSA_A)))
    assert exc.value.status_code == 503


# --- settings ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "problem"),
    [
        ({"AUTH_JWT_ALGORITHMS": "none"}, "never include 'none'"),
        ({"AUTH_JWT_ALGORITHMS": "RS256,XS999"}, "unsupported algorithm"),
        ({"AUTH_JWT_ALGORITHMS": ","}, "lists no algorithm"),
        ({"AUTH_JWT_ALGORITHMS": "HS256"}, "AUTH_JWT_ALLOW_HS=true"),
        ({"AUTH_JWT_ALGORITHMS": "HS256", "AUTH_JWT_ALLOW_HS": "true"}, "needs AUTH_JWT_SECRET"),
        (
            {
                "AUTH_JWT_ALGORITHMS": "HS256",
                "AUTH_JWT_ALLOW_HS": "true",
                "AUTH_JWT_SECRET": "short",
            },
            "at least 32 bytes",
        ),
        (
            {
                "AUTH_JWT_ALGORITHMS": "HS256",
                "AUTH_JWT_ALLOW_HS": "true",
                "AUTH_JWT_SECRET": _pem(RSA_A),
            },
            "not a PEM key",
        ),
        ({"AUTH_JWT_PUBLIC_KEY": ""}, "no verification key"),
        ({"AUTH_JWT_JWKS_URL": "https://issuer.test/jwks"}, "not both"),
        (
            {"AUTH_JWT_PUBLIC_KEY": "-----BEGIN PUBLIC KEY-----\nnope\n-----END PUBLIC KEY-----"},
            "not a PEM",
        ),
        (
            {"AUTH_JWT_PUBLIC_KEY": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----"},
            "private key",
        ),
        ({"AUTH_JWT_ALGORITHMS": "ES256"}, "verifies none of"),
        ({"AUTH_JWT_ISSUER": ""}, "AUTH_JWT_ISSUER is required"),
        ({"AUTH_JWT_AUDIENCE": " , "}, "AUTH_JWT_AUDIENCE is required"),
        ({"AUTH_JWT_LEEWAY_S": "soon"}, "whole number"),
        ({"AUTH_JWT_LEEWAY_S": "-5"}, "between 0 and 600"),
        ({"AUTH_JWT_JWKS_CACHE_S": "0"}, "between 1 and 86400"),
        ({"AUTH_JWT_ROLES_CLAIM": "realm_access..roles"}, "dotted path"),
    ],
)
def test_settings_problems(env: dict[str, str], problem: str) -> None:
    problems = _settings(**env).problems
    assert any(problem in p for p in problems), problems
    # A problem never quotes key material.
    assert not any("BEGIN" in p for p in problems)


@pytest.mark.parametrize(
    ("url", "extra", "ok"),
    [
        ("https://issuer.test/jwks", {}, True),
        ("http://issuer.test/jwks", {}, False),
        ("http://issuer.test/jwks", {"AUTH_JWT_JWKS_ALLOW_HTTP": "true"}, True),
        ("http://issuer.test/jwks", {"APP_ENV": "dev"}, True),
        ("http://127.0.0.1:9/jwks", {}, True),
        ("http://localhost:9/jwks", {}, True),
        ("ftp://issuer.test/jwks", {}, False),
        ("https://user:pw@issuer.test/jwks", {}, False),
        ("issuer.test/jwks", {}, False),
    ],
)
def test_jwks_url_must_be_https_outside_dev(url: str, extra: dict[str, str], ok: bool) -> None:
    problems = _settings(AUTH_JWT_PUBLIC_KEY=None, AUTH_JWT_JWKS_URL=url, **extra).problems  # type: ignore[arg-type]
    assert (problems == []) is ok, problems


def test_issuer_and_audience_are_optional_only_under_dev() -> None:
    dev = _settings(APP_ENV="dev", AUTH_JWT_ISSUER="", AUTH_JWT_AUDIENCE="")
    assert dev.problems == []
    assert len(dev.warnings) == 2
    for app_env in ("prod", "staging", ""):
        assert (
            len(_settings(APP_ENV=app_env, AUTH_JWT_ISSUER="", AUTH_JWT_AUDIENCE="").problems) == 2
        )


async def test_unchecked_issuer_and_audience_under_dev_accept_any() -> None:
    policy = JwtPolicy(_settings(APP_ENV="dev", AUTH_JWT_ISSUER="", AUTH_JWT_AUDIENCE=""))
    token = _token(RSA_A, iss="https://anyone.test/", aud="anything")
    assert (await policy.authenticate(_request(token))).id == "user-1"


def test_algorithm_names_are_normalised() -> None:
    settings = _settings(AUTH_JWT_ALGORITHMS="rs256, RS256 ,ps256")
    assert settings.algorithms == ("RS256", "PS256") and settings.problems == []


# --- claims -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("claims", "setting", "roles"),
    [
        ({"realm_access": {"roles": ["admin", "user"]}}, "realm_access.roles", ["admin", "user"]),
        ({"scope": "read write,admin  read"}, "scope", ["read", "write", "admin"]),
        ({"https://example.com/roles": ["ops"]}, "https://example.com/roles", ["ops"]),
        ({"roles": "solo"}, "roles", ["solo"]),
        (
            {"roles": ["ok", 7, None, {"x": 1}, " spaced ", "a,b", "bad\nrole"]},
            "roles",
            ["ok", "spaced"],
        ),
        ({"roles": {"not": "a list"}}, "roles", []),
        ({}, "roles", []),
        ({"realm_access": "flat"}, "realm_access.roles", []),
    ],
)
def test_roles_claim_forms(claims: dict[str, Any], setting: str, roles: list[str]) -> None:
    policy = JwtPolicy(_settings(AUTH_JWT_ROLES_CLAIM=setting))
    principal = policy.principal_from_claims({"sub": "u", **claims})
    assert principal.roles == roles


async def test_principal_claim_setting() -> None:
    policy = JwtPolicy(_settings(AUTH_JWT_PRINCIPAL_CLAIM="email"))
    principal = await policy.authenticate(_request(_token(RSA_A, email="ada@example.com")))
    assert principal.id == "ada@example.com"
    await _reject(policy, _token(RSA_A), "missing principal claim")
    nested = JwtPolicy(_settings(AUTH_JWT_PRINCIPAL_CLAIM="user.id"))
    assert (await nested.authenticate(_request(_token(RSA_A, user={"id": 42})))).id == "42"


# --- no token contents in logs or errors --------------------------------------------


async def test_token_contents_never_reach_logs_or_errors(
    jwks: JwksServer, caplog: pytest.LogCaptureFixture
) -> None:
    secret_sub = "sub-7f3a9c-secret"
    tokens = [
        _token(RSA_A, sub=secret_sub, exp=int(time.time()) - 999),
        _token(RSA_A, sub=secret_sub, aud="elsewhere"),
        _token(RSA_B, sub=secret_sub, kid="unknown-kid-9d2e"),
        _token(RSA_B, sub=secret_sub, kid="a"),
        _forge({"alg": "none", "kid": "a"}, _claims(sub=secret_sub)),
    ]
    policy = _jwks_policy(jwks)
    details = []
    with caplog.at_level(logging.DEBUG):
        for token in tokens:
            details.append(str((await _reject(policy, token)).detail))
        jwks.status = 500
        policy.jwks = JwksCache(jwks.url, 1, refetch_interval_s=0)
        with pytest.raises(HTTPException):
            await policy.authenticate(_request(tokens[1]))
    text = caplog.text + " ".join(details)
    for token in tokens:
        for part in token.split("."):
            if len(part) > 8:
                assert part not in text
    assert secret_sub not in text and "unknown-kid-9d2e" not in text


# --- wiring: selection, startup, fastapi dependency, LangGraph Server auth ---------


@pytest.fixture
def jwt_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    monkeypatch.setenv("AUTH_POLICY", "jwt")
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("AUTH_JWT_PUBLIC_KEY", _pem(RSA_A))
    monkeypatch.setenv("AUTH_JWT_ISSUER", ISSUER)
    monkeypatch.setenv("AUTH_JWT_AUDIENCE", AUDIENCE)
    monkeypatch.delenv("AUTH_JWT_JWKS_URL", raising=False)
    monkeypatch.delenv("AUTH_JWT_ALGORITHMS", raising=False)
    reset_policy_cache()
    yield monkeypatch
    reset_policy_cache()


async def test_selected_by_auth_policy_and_used_by_the_fastapi_dependency(jwt_env) -> None:
    assert isinstance(get_policy(), JwtPolicy)
    check_startup()
    principal = await require("chat.send")(_request(_token(RSA_A)))
    assert principal.id == "user-1"
    with pytest.raises(HTTPException) as exc:
        await require("thread.read")(_request(_token(RSA_B)))
    assert exc.value.status_code == 401 and "WWW-Authenticate" in exc.value.headers


async def test_startup_fails_closed_outside_dev_and_requests_get_503(
    jwt_env, caplog: pytest.LogCaptureFixture
) -> None:
    jwt_env.delenv("AUTH_JWT_AUDIENCE")
    reset_policy_cache()
    with pytest.raises(RuntimeError, match="AUTH_JWT_AUDIENCE is required"):
        check_startup()
    for action in ACTIONS:
        with pytest.raises(HTTPException) as exc:
            await require(action)(_request(_token(RSA_A)))
        assert exc.value.status_code == 503 and "AUTH_POLICY=jwt" in exc.value.detail
    # Under dev a key-less configuration is logged, not fatal; requests still get 503.
    jwt_env.setenv("APP_ENV", "dev")
    jwt_env.delenv("AUTH_JWT_PUBLIC_KEY")
    reset_policy_cache()
    with caplog.at_level(logging.ERROR):
        check_startup()
    assert "no verification key" in caplog.text
    with pytest.raises(HTTPException) as exc:
        await require("chat.send")(_request(_token(RSA_A)))
    assert exc.value.status_code == 503


async def test_langgraph_server_auth_uses_the_same_policy(jwt_env) -> None:
    from langgraph_sdk import Auth

    auth = build_sdk_auth()
    authenticate = auth._authenticate_handler
    user = await authenticate(request=_request(_token(RSA_A, roles="admin ops")))
    assert user["identity"] == "user-1"
    assert "role:admin" in user["permissions"] and "role:ops" in user["permissions"]
    with pytest.raises(Auth.exceptions.HTTPException) as exc:
        await authenticate(request=_request(_token(RSA_A, iss="https://evil.test/")))
    assert exc.value.status_code == 401
    assert "invalid_token" in dict(exc.value.headers or {})["WWW-Authenticate"]
    # A misconfigured jwt policy stops the server from loading its auth outside dev.
    jwt_env.delenv("AUTH_JWT_ISSUER")
    reset_policy_cache()
    with pytest.raises(RuntimeError, match="AUTH_JWT_ISSUER"):
        build_sdk_auth()
