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

"""RFC 8693 token exchange for `auth: exchange` APIs (another agent, a user-scoped service).

An `auth: exchange` API is called with a token the issuer mints for it in
exchange for the caller's own (the verified bearer the `jwt` policy keeps in
`credentials["@subject_token"]`, or a custom policy's `keep_subject_token`):
the API's `exchange.audience`, and its optional `scope` and `resource`, pin
what the new token is good for, and the issuer names this agent in its `act`
claim. `api_client` asks for it just before sending, after the policy check,
the approval gate and the limits, so a refused call, or one paused for a
person's approval, never exchanges; nothing here runs while a request is
authenticated.

Settings (environment; `TOKEN_EXCHANGE_CLIENT_SECRET` is a secret):

* `TOKEN_EXCHANGE_URL`: the issuer's token endpoint. https outside
  `APP_ENV=dev`, unless the host is loopback or `TOKEN_EXCHANGE_ALLOW_HTTP=true`
  (a trusted in-cluster issuer), as for `AUTH_JWT_JWKS_URL`.
* `TOKEN_EXCHANGE_CLIENT_ID`, `TOKEN_EXCHANGE_CLIENT_SECRET`: this agent's
  client, sent as `TOKEN_EXCHANGE_CLIENT_AUTH` says (`client_secret_basic`,
  the default, or `client_secret_post`).
* `TOKEN_EXCHANGE_SUBJECT_TOKEN_TYPE`: the `subject_token_type` sent
  (`urn:ietf:params:oauth:token-type:access_token`, the default, or `...:jwt`).
* `TOKEN_EXCHANGE_TIMEOUT_MS` (2000): the whole exchange's deadline (connecting
  takes at most 1 s of it).
* `TOKEN_EXCHANGE_MAX_TTL_S` (300, at most 300): how long an exchanged token is
  reused.
* `TOKEN_EXCHANGE_FAILURE_TTL_S` (10): how long a refusal is remembered, and
  how long the issuer is left alone after it fails.
* `TOKEN_EXCHANGE_CACHE_MAX` (10000): exchanged tokens kept per process.

Behaviour:

* Cached in process memory only (never persisted, traced or logged), per
  subject token (its SHA-256), token URL, client, audience, scope and resource,
  for min(`expires_in`, `TOKEN_EXCHANGE_MAX_TTL_S`, the subject token's
  remaining lifetime) less 30 s; a token that would be kept under 5 s is used
  once and not kept. Concurrent calls for one key share one exchange.
* A subject token with 10 s or less left is not exchanged.
* The issuer's refusal (a 4xx: `invalid_grant`, `invalid_target`, ...) is
  remembered for that key for `TOKEN_EXCHANGE_FAILURE_TTL_S`.
* A timeout, a connection error, a 5xx (or 408/429) or an unusable answer
  counts against the token URL's circuit breaker: 3 in a row open it for
  `TOKEN_EXCHANGE_FAILURE_TTL_S`, while calls fail at once; then one call
  probes the issuer, and its outcome closes or reopens it. During an issuer
  outage a process waits at most one deadline per window, on calls to
  exchange APIs only.
* An issued token that names no actor is refused, and nothing is sent: a JWT
  without the `act` claim (or the claim `AUTH_JWT_ACTOR_CLAIM` names), and
  any token that is not a readable signed JWT (opaque, encrypted). The agent
  behind the API would read it as the user's own unless it sets
  `AUTH_JWT_DIRECT_CLIENTS`, so it could let this agent decide the user's
  approvals there. The refusal is remembered for that key like the issuer's
  own. An API opts in with `exchange.allow_actorless: true` (the agent behind
  it must then set `AUTH_JWT_DIRECT_CLIENTS` and list this agent as
  `client:<its client id>` in `AUTH_ALLOWED_ACTORS`); the first such token
  then logs one warning saying so. The claims are read unverified, for this
  check only (the called agent verifies the token).
* Metrics: `agent_token_exchanges_total{api, outcome}` (`issued`, `cached`,
  `refused`, `no_actor`, `unavailable`, `circuit_open`) and
  `agent_token_exchange_duration_seconds{api}`; one log line per exchange
  sent, one warning when the breaker opens. Neither carries token material or
  the subject's hash; only the issuer's RFC 6749 `error` code and HTTP status.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from {{cookiecutter.agent_directory}}.app_utils.limits import SettingsError

logger = logging.getLogger(__name__)

GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"
JWT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:jwt"
SUBJECT_TOKEN_TYPES = (ACCESS_TOKEN_TYPE, JWT_TOKEN_TYPE)
CLIENT_SECRET_BASIC = "client_secret_basic"
CLIENT_SECRET_POST = "client_secret_post"
CLIENT_AUTH_METHODS = (CLIENT_SECRET_BASIC, CLIENT_SECRET_POST)

DEFAULT_TIMEOUT_MS = 2000
MIN_TIMEOUT_MS = 100
MAX_TIMEOUT_MS = 60_000
MAX_CONNECT_S = 1.0
DEFAULT_MAX_TTL_S = 300
MAX_TTL_CAP_S = 300
DEFAULT_FAILURE_TTL_S = 10
MAX_FAILURE_TTL_S = 300
DEFAULT_CACHE_MAX = 10_000
MAX_CACHE_MAX = 1_000_000
# `expires_in` when the issuer leaves it out (RFC 6749 section 5.1 makes it optional).
DEFAULT_EXPIRES_IN_S = 60
# A kept token is dropped this long before it would expire; one that would be kept for
# less than MIN_CACHE_TTL_S is used for the call that asked and not kept.
EXPIRY_MARGIN_S = 30
MIN_CACHE_TTL_S = 5
# A subject token with this long or less to live is not exchanged.
MIN_SUBJECT_LIFETIME_S = 10
RESPONSE_MAX_BYTES = 65_536
ACCESS_TOKEN_MAX_CHARS = 16_384
# Consecutive issuer failures that open a token URL's circuit breaker.
BREAKER_THRESHOLD = 3
# The RFC 8693 claim naming the agent that presents a token, unless
# `AUTH_JWT_ACTOR_CLAIM` names another (`auth.DEFAULT_JWT_ACTOR_CLAIM`).
ACTOR_CLAIM = "act"

ISSUED = "issued"
CACHED = "cached"
REFUSED = "refused"
NO_ACTOR = "no_actor"
UNAVAILABLE = "unavailable"
CIRCUIT_OPEN = "circuit_open"
OUTCOMES = (ISSUED, CACHED, REFUSED, NO_ACTOR, UNAVAILABLE, CIRCUIT_OPEN)
# Kept as a key's refusal when the issued token names no actor: never an RFC 6749 error code
# (those match `_ERROR_CODE_RE`, which allows no space).
_NO_ACTOR_CODE = "names no actor"

_TRUE = ("1", "true", "yes", "on")
_LOOPBACK_HOSTS = ("localhost",)
# The issuer's RFC 6749 error code, when it is one (never other response text).
_ERROR_CODE_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


class TokenExchangeError(Exception):
    """No token for the call: the message is what the tool (and the model) reads.

    `outcome` is the metric's (`refused`, `no_actor`, `unavailable`,
    `circuit_open`), or None
    when no exchange was attempted (not configured, no or an expiring subject
    token); `reason` is a short fixed phrase for the call's log line.
    """

    def __init__(self, message: str, *, outcome: str | None, reason: str) -> None:
        super().__init__(message)
        self.outcome = outcome
        self.reason = reason


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def _dev(env: Mapping[str, str]) -> bool:
    return env.get("APP_ENV") == "dev"  # exactly, as auth.dev_mode()


def _int(env: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(f"{name}={raw!r} is not a whole number.") from None
    if not low <= value <= high:
        raise SettingsError(f"{name}={value} must be from {low} to {high}.")
    return value


def _loopback(host: str) -> bool:
    host = host.strip("[]").lower()
    if host in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class ExchangeSettings:
    """The `TOKEN_EXCHANGE_*` settings, validated (`exchange_settings`)."""

    url: str | None = None
    client_id: str | None = None
    # Out of repr: a repr ends up in logs and tracebacks.
    client_secret: str | None = field(default=None, repr=False)
    client_auth: str = CLIENT_SECRET_BASIC
    subject_token_type: str = ACCESS_TOKEN_TYPE
    timeout_s: float = DEFAULT_TIMEOUT_MS / 1000
    max_ttl_s: int = DEFAULT_MAX_TTL_S
    failure_ttl_s: int = DEFAULT_FAILURE_TTL_S
    cache_max: int = DEFAULT_CACHE_MAX

    def missing(self) -> list[str]:
        """The settings an exchange needs that are not set."""
        return [
            name
            for name, value in (
                ("TOKEN_EXCHANGE_URL", self.url),
                ("TOKEN_EXCHANGE_CLIENT_ID", self.client_id),
                ("TOKEN_EXCHANGE_CLIENT_SECRET", self.client_secret),
            )
            if not value
        ]


def exchange_settings(env: Mapping[str, str] | None = None) -> ExchangeSettings:
    """The token exchange settings; `SettingsError` names a bad one (a startup error).

    Unset settings are not errors here (`ExchangeSettings.missing`, reported at
    startup when an `auth: exchange` API exists); a malformed value, or an http
    `TOKEN_EXCHANGE_URL` outside `APP_ENV=dev` without a loopback host or
    `TOKEN_EXCHANGE_ALLOW_HTTP=true`, is.
    """
    env = os.environ if env is None else env
    url = (env.get("TOKEN_EXCHANGE_URL") or "").strip() or None
    if url is not None:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.scheme not in ("https", "http") or not host:
            raise SettingsError(
                "TOKEN_EXCHANGE_URL must be an https:// URL (the issuer's token endpoint)."
            )
        if parts.username or parts.password or parts.fragment:
            raise SettingsError("TOKEN_EXCHANGE_URL must not carry credentials or a fragment.")
        allow_http = (env.get("TOKEN_EXCHANGE_ALLOW_HTTP") or "").strip().lower() in _TRUE
        if parts.scheme == "http" and not (_dev(env) or _loopback(host) or allow_http):
            raise SettingsError(
                "TOKEN_EXCHANGE_URL must use https outside APP_ENV=dev (it carries the users' "
                "tokens and this agent's client secret); set TOKEN_EXCHANGE_ALLOW_HTTP=true only "
                "for a trusted in-cluster issuer."
            )
    client_auth = (env.get("TOKEN_EXCHANGE_CLIENT_AUTH") or CLIENT_SECRET_BASIC).strip().lower()
    if client_auth not in CLIENT_AUTH_METHODS:
        raise SettingsError(
            f"TOKEN_EXCHANGE_CLIENT_AUTH={client_auth!r} must be one of "
            f"{', '.join(CLIENT_AUTH_METHODS)}."
        )
    token_type = (env.get("TOKEN_EXCHANGE_SUBJECT_TOKEN_TYPE") or ACCESS_TOKEN_TYPE).strip()
    if token_type not in SUBJECT_TOKEN_TYPES:
        raise SettingsError(
            f"TOKEN_EXCHANGE_SUBJECT_TOKEN_TYPE={token_type!r} must be one of "
            f"{', '.join(SUBJECT_TOKEN_TYPES)}."
        )
    return ExchangeSettings(
        url=url,
        client_id=(env.get("TOKEN_EXCHANGE_CLIENT_ID") or "").strip() or None,
        client_secret=env.get("TOKEN_EXCHANGE_CLIENT_SECRET") or None,
        client_auth=client_auth,
        subject_token_type=token_type,
        timeout_s=_int(
            env, "TOKEN_EXCHANGE_TIMEOUT_MS", DEFAULT_TIMEOUT_MS, MIN_TIMEOUT_MS, MAX_TIMEOUT_MS
        )
        / 1000,
        max_ttl_s=_int(env, "TOKEN_EXCHANGE_MAX_TTL_S", DEFAULT_MAX_TTL_S, 1, MAX_TTL_CAP_S),
        failure_ttl_s=_int(
            env, "TOKEN_EXCHANGE_FAILURE_TTL_S", DEFAULT_FAILURE_TTL_S, 1, MAX_FAILURE_TTL_S
        ),
        cache_max=_int(env, "TOKEN_EXCHANGE_CACHE_MAX", DEFAULT_CACHE_MAX, 1, MAX_CACHE_MAX),
    )


# ---------------------------------------------------------------------------
# Which APIs act for the caller, and where (the compatibility matrix)
# ---------------------------------------------------------------------------

SHARED_BEARER = "shared-bearer"
JWT = "jwt"
FASTAPI = "fastapi"
LANGGRAPH_SERVER = "langgraph-server"


def server_runtime(env: Mapping[str, str] | None = None) -> str:
    """`langgraph-server` or `fastapi`, read as `chat.detect_runtime` reads it."""
    env = os.environ if env is None else env
    explicit = (env.get("RUNTIME") or "").strip().lower()
    if explicit in (FASTAPI, LANGGRAPH_SERVER):
        return explicit
    if (env.get("LANGGRAPH_SERVER") or "").lower() in ("1", "true", "yes"):
        return LANGGRAPH_SERVER
    if env.get("LANGSERVE_GRAPHS"):
        return LANGGRAPH_SERVER
    return FASTAPI


def _policy_apis() -> dict[str, dict[str, Any]]:
    """The loaded api-policy's APIs; empty without a readable, valid policy."""
    try:
        from {{cookiecutter.agent_directory}}.app_utils.api_client import load_policy

        return load_policy().apis
    except Exception:
        return {}


def _names(apis: Mapping[str, Mapping[str, Any]], test: Callable[[Mapping[str, Any]], bool]) -> str:
    return ", ".join(name for name, api in apis.items() if test(api))


def compatibility(
    auth_policy: str,
    runtime: str,
    apis: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[list[str], list[str]]:
    """The api-policy's identity-carrying APIs against this auth policy and runtime.

    Returns `(problems, warnings)`. Problems (the startup refuses them outside
    `APP_ENV=dev`): an `auth: exchange` API under `shared-bearer` (no user token
    to exchange) or under langgraph-server (the server persists the run
    context, so the user's token would be stored). Warnings, logged: an
    `auth: forward` API under `shared-bearer`, under `jwt` without
    `forward_audience`, or under langgraph-server, whose every call fails for
    want of a credential (0.2 projects keep starting with one).
    """
    apis = _policy_apis() if apis is None else apis
    problems: list[str] = []
    warnings: list[str] = []
    exchange = _names(apis, lambda a: a.get("auth") == "exchange")
    forward = _names(apis, lambda a: a.get("auth") == "forward")
    if exchange and auth_policy == SHARED_BEARER:
        problems.append(
            f"auth: exchange (apis: {exchange}) under AUTH_POLICY=shared-bearer: shared-bearer "
            "has no user token to exchange; use auth: bearer with the peer's agent key"
        )
    if exchange and runtime == LANGGRAPH_SERVER:
        problems.append(
            f"auth: exchange (apis: {exchange}) is not supported with runtime langgraph-server: "
            "LangGraph Server persists the run context, so the user's token would be stored"
        )
    if forward and auth_policy == SHARED_BEARER:
        warnings.append(
            f"auth: forward (apis: {forward}) under AUTH_POLICY=shared-bearer: there is no user "
            "credential to forward, so every call to it fails"
        )
    unaimed = _names(apis, lambda a: a.get("auth") == "forward" and "forward_audience" not in a)
    if unaimed and auth_policy == JWT:
        warnings.append(
            f"auth: forward (apis: {unaimed}) under AUTH_POLICY=jwt without forward_audience: jwt "
            "sets no per-API credential, so every call to it fails; set forward_audience (the "
            "caller's token must be minted for that audience too), or prefer auth: exchange"
        )
    if forward and runtime == LANGGRAPH_SERVER:
        warnings.append(
            f"auth: forward (apis: {forward}) is not supported with runtime langgraph-server: "
            "the server's run context carries no credentials, so every call to it fails"
        )
    return problems, warnings


def startup_problems(auth_policy: str, runtime: str | None = None) -> list[str]:
    """What stops startup outside `APP_ENV=dev` (`auth.check_startup`): the refused
    cells of the compatibility matrix, and an `auth: exchange` API without the
    issuer's token endpoint or this agent's client. The warnings are logged."""
    apis = _policy_apis()
    problems, warnings = compatibility(auth_policy, runtime or server_runtime(), apis)
    for warning in warnings:
        logger.warning("api-policy: %s", warning)
    exchange = _names(apis, lambda a: a.get("auth") == "exchange")
    if exchange:
        try:
            missing = exchange_settings().missing()
        except SettingsError:
            missing = []  # the lifespan's settings check names the bad value
        if missing:
            problems.append(
                f"auth: exchange (apis: {exchange}) needs {', '.join(missing)} (the issuer's "
                "token endpoint and this agent's client there)"
            )
    return problems


# ---------------------------------------------------------------------------
# Loops
# ---------------------------------------------------------------------------

# How `jwt` names an agent known only by its token's client (`auth.CLIENT_ACTOR_PREFIX`).
CLIENT_ACTOR_PREFIX = "client:"


def own_names(env: Mapping[str, str] | None = None) -> set[str]:
    """What names this agent: its A2A name, and the audiences its own tokens carry."""
    env = os.environ if env is None else env
    names = {env.get("A2A_NAME") or "{{cookiecutter.agent_directory}}"}
    names.update(p.strip() for p in (env.get("AUTH_JWT_AUDIENCE") or "").split(",") if p.strip())
    return names


def loop_problem(target: str, actor_chain: tuple[str, ...]) -> str | None:
    """Why a call to the agent `target` (an audience) would loop, or None.

    It would when `target` is this agent (its A2A name or one of its own
    audiences), or an agent already in the delegation chain of the request
    (`actor_chain`, current first): A -> B -> A. An actor the policy knows only
    by its client (`client:<azp>`, an issuer that names no actor) counts by
    that client's name.
    """
    me = os.environ.get("A2A_NAME") or "{{cookiecutter.agent_directory}}"
    if target in own_names():
        return f"calling {target} would call this agent itself ({me})"
    if target in actor_chain or f"{CLIENT_ACTOR_PREFIX}{target}" in actor_chain:
        path = " -> ".join([*reversed(actor_chain), me, target])
        return f"calling {target} would loop back through the delegation chain ({path})"
    return None


# ---------------------------------------------------------------------------
# The exchange
# ---------------------------------------------------------------------------


def _observe(api: str, outcome: str, seconds: float | None = None) -> None:
    try:
        from {{cookiecutter.agent_directory}}.app_utils.metrics import observe_token_exchange
    except ImportError:  # loaded outside its package, or without prometheus_client
        return
    observe_token_exchange(api, outcome, seconds)


def _client_auth(settings: ExchangeSettings) -> tuple[dict[str, str], dict[str, str]]:
    """(headers, form fields) that authenticate this agent's client (RFC 6749 section 2.3.1)."""
    client_id, secret = settings.client_id or "", settings.client_secret or ""
    if settings.client_auth == CLIENT_SECRET_POST:
        return {}, {"client_id": client_id, "client_secret": secret}
    # Each part form-encoded first, then base64 (RFC 6749 section 2.3.1).
    pair = f"{quote(client_id, safe='')}:{quote(secret, safe='')}"
    return {"Authorization": "Basic " + base64.b64encode(pair.encode("utf-8")).decode("ascii")}, {}


@dataclass
class _Breaker:
    """A token URL's circuit breaker: consecutive failures, open until, one probe."""

    failures: int = 0
    open_until: float | None = None
    # When the call probing the issuer (half open) started; None when none is.
    probing: float | None = None


class _Unavailable(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class _Refused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TokenExchanger:
    """The process's exchanged tokens, refusals, in-flight exchanges and breakers."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._clock = clock
        self._wall = wall
        self._transport = transport
        self._lock = threading.Lock()
        # key -> (token, drop at, on self._clock)
        self._tokens: OrderedDict[tuple[str, ...], tuple[str, float]] = OrderedDict()
        # key -> (the issuer's error code, forget at)
        self._refusals: OrderedDict[tuple[str, ...], tuple[str, float]] = OrderedDict()
        self._inflight: dict[tuple[str, ...], asyncio.Task[str]] = {}
        self._breakers: dict[str, _Breaker] = {}
        # Whether the warning for an allowed token that names no actor was logged.
        self._warned_no_actor = False

    def clear(self) -> None:
        """Forget every token, refusal and breaker (tests)."""
        with self._lock:
            self._tokens.clear()
            self._refusals.clear()
            self._breakers.clear()
            self._warned_no_actor = False

    def cached(self) -> int:
        """How many exchanged tokens are kept."""
        with self._lock:
            return len(self._tokens)

    async def token(
        self,
        api: str,
        subject_token: str,
        *,
        audience: str,
        scope: str | None = None,
        resource: str | None = None,
        subject_expires_at: float | None = None,
        settings: ExchangeSettings | None = None,
        allow_actorless: bool = False,
    ) -> str:
        """A token for `api` exchanged for `subject_token`; `TokenExchangeError` otherwise.

        `subject_expires_at` is the subject token's `exp` (epoch seconds), when known.
        `allow_actorless` (the API's `exchange.allow_actorless`) accepts a token
        that names no actor; without it such a token is refused (`no_actor`).
        """
        if settings is None:
            try:
                settings = exchange_settings()
            except SettingsError as exc:
                raise TokenExchangeError(
                    f"API {api!r} uses auth: exchange, but the token exchange settings are "
                    f"invalid ({exc}); nothing was sent.",
                    outcome=None,
                    reason="token exchange misconfigured",
                ) from None
        missing = settings.missing()
        if missing:
            raise TokenExchangeError(
                f"API {api!r} uses auth: exchange, but {', '.join(missing)} "
                f"{'is' if len(missing) == 1 else 'are'} not set; nothing was sent.",
                outcome=None,
                reason="token exchange not configured",
            )
        if (
            subject_expires_at is not None
            and subject_expires_at - self._wall() <= MIN_SUBJECT_LIFETIME_S
        ):
            raise TokenExchangeError(
                f"API {api!r} uses auth: exchange, but the caller's token has expired; nothing "
                "was sent.",
                outcome=None,
                reason="caller's token expired",
            )
        assert settings.url is not None and settings.client_id is not None
        key = (
            hashlib.sha256(subject_token.encode("utf-8")).hexdigest(),
            settings.url,
            settings.client_id,
            audience,
            scope or "",
            resource or "",
            # A token kept for an API that accepts one naming no actor is never handed to
            # an API that does not.
            "allow_actorless" if allow_actorless else "",
        )
        now = self._clock()
        with self._lock:
            kept = self._tokens.get(key)
            if kept is not None and kept[1] > now:
                self._tokens.move_to_end(key)
                _observe(api, CACHED)
                return kept[0]
            if kept is not None:
                del self._tokens[key]
            refusal = self._refusals.get(key)
            if refusal is not None and refusal[1] <= now:
                del self._refusals[key]
                refusal = None
        if refusal is not None:
            if refusal[0] == _NO_ACTOR_CODE:
                _observe(api, NO_ACTOR)
                raise self._no_actor_error(api)
            _observe(api, REFUSED)
            raise self._refused_error(api, refusal[0])

        task = self._inflight.get(key)
        joined = (
            task is not None and not task.done() and task.get_loop() is asyncio.get_running_loop()
        )
        if not joined:
            self._admit(api, settings)  # the breaker: raises while it is open
            task = asyncio.get_running_loop().create_task(
                self._exchange(
                    api,
                    key,
                    subject_token,
                    audience,
                    scope,
                    resource,
                    subject_expires_at,
                    settings,
                    allow_actorless,
                )
            )
            self._inflight[key] = task
            task.add_done_callback(lambda done, k=key: self._forget_inflight(k, done))
        assert task is not None
        try:
            # Shielded: a caller that goes away does not cancel the exchange others share.
            token = await asyncio.shield(task)
        except TokenExchangeError as exc:
            if joined and exc.outcome:
                _observe(api, exc.outcome)
            raise
        if joined:
            _observe(api, CACHED)
        return token

    def _forget_inflight(self, key: tuple[str, ...], task: asyncio.Task[Any]) -> None:
        if self._inflight.get(key) is task:
            del self._inflight[key]
        if task.cancelled():
            # Cancelled, perhaps before it ran (its loop closed): it probes nothing any more.
            with self._lock:
                breaker = self._breakers.get(key[1])
                if breaker is not None:
                    breaker.probing = None
        else:
            task.exception()  # retrieved: a failed exchange nobody awaits is not "never retrieved"

    def _admit(self, api: str, settings: ExchangeSettings) -> None:
        """Let an exchange go to the issuer, or fail at once while its breaker is open."""
        assert settings.url is not None
        now = self._clock()
        with self._lock:
            breaker = self._breakers.setdefault(settings.url, _Breaker())
            if breaker.open_until is None:
                return
            # A probe that has run past its deadline is gone (its loop closed): probe again.
            probing = (
                breaker.probing is not None and now - breaker.probing <= settings.timeout_s + 1
            )
            if now < breaker.open_until or probing:
                wait = max(1, round(max(breaker.open_until, now + 1) - now))
            else:
                breaker.probing = now  # half open: this call probes the issuer
                return
        _observe(api, CIRCUIT_OPEN)
        raise TokenExchangeError(
            f"token issuer unavailable (retry in {wait} s); nothing was sent to API {api!r}.",
            outcome=CIRCUIT_OPEN,
            reason="token issuer unavailable (circuit open)",
        )

    def _settle(self, settings: ExchangeSettings, *, failed: bool) -> None:
        """Record the issuer's answer (or its failure) on the token URL's breaker."""
        assert settings.url is not None
        opened = False
        with self._lock:
            breaker = self._breakers.setdefault(settings.url, _Breaker())
            if not failed:
                breaker.failures, breaker.open_until, breaker.probing = 0, None, None
                return
            breaker.failures += 1
            if breaker.probing is not None or breaker.failures >= BREAKER_THRESHOLD:
                opened = breaker.open_until is None
                breaker.open_until = self._clock() + settings.failure_ttl_s
                breaker.probing = None
        if opened:
            logger.warning(
                "token exchange: the issuer failed %d times in a row; calls to auth: exchange "
                "APIs fail at once for %d s, then one call tries it again",
                BREAKER_THRESHOLD,
                settings.failure_ttl_s,
            )

    @staticmethod
    def _no_actor_error(api: str) -> TokenExchangeError:
        return TokenExchangeError(
            f"token exchange for API {api!r}: the issuer's token names no actor (no "
            f"{actor_claim()} claim, or not a readable JWT), so the agent behind the API would "
            "take this agent's call for the user's own; nothing was sent. Have the issuer name "
            "this agent in the token, or set exchange.allow_actorless: true for the API once "
            "that agent sets AUTH_JWT_DIRECT_CLIENTS.",
            outcome=NO_ACTOR,
            reason="exchanged token names no actor",
        )

    @staticmethod
    def _refused_error(api: str, code: str) -> TokenExchangeError:
        return TokenExchangeError(
            f"token exchange for API {api!r} was refused ({code}); nothing was sent.",
            outcome=REFUSED,
            reason=f"token exchange refused ({code})",
        )

    async def _exchange(
        self,
        api: str,
        key: tuple[str, ...],
        subject_token: str,
        audience: str,
        scope: str | None,
        resource: str | None,
        subject_expires_at: float | None,
        settings: ExchangeSettings,
        allow_actorless: bool = False,
    ) -> str:
        started = time.perf_counter()
        try:
            token, expires_in = await asyncio.wait_for(
                self._post(subject_token, audience, scope, resource, settings),
                timeout=settings.timeout_s,
            )
        except _Refused as exc:
            self._settle(settings, failed=False)  # the issuer answered
            with self._lock:
                self._refusals[key] = (exc.code, self._clock() + settings.failure_ttl_s)
                self._refusals.move_to_end(key)
                while len(self._refusals) > settings.cache_max:
                    self._refusals.popitem(last=False)
            self._log(api, audience, f"refused ({exc.code})", started)
            _observe(api, REFUSED, time.perf_counter() - started)
            raise self._refused_error(api, exc.code) from None
        except (_Unavailable, TimeoutError, httpx.HTTPError) as exc:
            detail = (
                exc.detail
                if isinstance(exc, _Unavailable)
                else "timed out"
                if isinstance(exc, TimeoutError | httpx.TimeoutException)
                else type(exc).__name__
            )
            self._settle(settings, failed=True)
            self._log(api, audience, f"unavailable ({detail})", started, failed=True)
            _observe(api, UNAVAILABLE, time.perf_counter() - started)
            raise TokenExchangeError(
                f"token issuer unavailable ({detail}); nothing was sent to API {api!r}.",
                outcome=UNAVAILABLE,
                reason=f"token issuer unavailable ({detail})",
            ) from None
        except BaseException:
            # Cancelled (the loop is closing), or a bug: never leave a probe marked running.
            with self._lock:
                breaker = self._breakers.get(settings.url or "")
                if breaker is not None:
                    breaker.probing = None
            raise
        self._settle(settings, failed=False)
        claim = actor_claim()
        try:
            unnamed = names_no_actor(token, claim)
        except Exception:  # a bug while reading the claims: judged as naming no actor
            unnamed = True
        if unnamed and not allow_actorless:
            with self._lock:
                self._refusals[key] = (_NO_ACTOR_CODE, self._clock() + settings.failure_ttl_s)
                self._refusals.move_to_end(key)
                while len(self._refusals) > settings.cache_max:
                    self._refusals.popitem(last=False)
            self._log(api, audience, "refused (the token names no actor)", started)
            _observe(api, NO_ACTOR, time.perf_counter() - started)
            raise self._no_actor_error(api)
        self._keep(key, token, expires_in, subject_expires_at, settings)
        self._log(api, audience, "issued", started)
        _observe(api, ISSUED, time.perf_counter() - started)
        if unnamed:
            self._warn_actorless(api, audience, claim, settings)
        return token

    def _warn_actorless(
        self, api: str, audience: str, claim: str, settings: ExchangeSettings
    ) -> None:
        """Warn once per process when an API that allows it gets a token naming no actor.

        The agent behind the API reads this agent's calls as the user's own
        (it could let this agent decide the user's approvals) unless it sets
        `AUTH_JWT_DIRECT_CLIENTS`; this agent cannot check that it does.
        """
        with self._lock:
            if self._warned_no_actor:
                return
            self._warned_no_actor = True
        logger.warning(
            "token exchange: the token the issuer minted for %s (audience %s) names no actor "
            "(no %s claim), and exchange.allow_actorless lets it through: the agent behind it "
            "reads this agent's calls as the user's own, and may let this agent decide the "
            "user's approvals there, unless it sets AUTH_JWT_DIRECT_CLIENTS to the clients "
            "people sign in with and lists client:%s in AUTH_ALLOWED_ACTORS",
            api,
            audience,
            claim,
            settings.client_id,
        )

    def _keep(
        self,
        key: tuple[str, ...],
        token: str,
        expires_in: int,
        subject_expires_at: float | None,
        settings: ExchangeSettings,
    ) -> None:
        """Keep the token for min(expires_in, the cap, the subject's lifetime) less the margin."""
        lifetime = float(min(expires_in, settings.max_ttl_s))
        if subject_expires_at is not None:
            lifetime = min(lifetime, subject_expires_at - self._wall())
        ttl = lifetime - EXPIRY_MARGIN_S
        if ttl < MIN_CACHE_TTL_S:
            return
        with self._lock:
            self._tokens[key] = (token, self._clock() + ttl)
            self._tokens.move_to_end(key)
            while len(self._tokens) > settings.cache_max:
                self._tokens.popitem(last=False)

    def _log(
        self, api: str, audience: str, outcome: str, started: float, *, failed: bool = False
    ) -> None:
        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.log(
            logging.WARNING if failed else logging.INFO,
            "token exchange for %s (audience %s): %s (%d ms)",
            api,
            audience,
            outcome,
            latency_ms,
            extra={"api": api, "outcome": outcome, "latency_ms": latency_ms},
        )

    async def _post(
        self,
        subject_token: str,
        audience: str,
        scope: str | None,
        resource: str | None,
        settings: ExchangeSettings,
    ) -> tuple[str, int]:
        """POST the RFC 8693 request; `(access_token, expires_in)`, or `_Refused`/`_Unavailable`."""
        headers, auth_fields = _client_auth(settings)
        form = {
            "grant_type": GRANT_TYPE,
            "subject_token": subject_token,
            "subject_token_type": settings.subject_token_type,
            "requested_token_type": ACCESS_TOKEN_TYPE,
            "audience": audience,
            **({"scope": scope} if scope else {}),
            **({"resource": resource} if resource else {}),
            **auth_fields,
        }
        timeout = httpx.Timeout(settings.timeout_s, connect=min(MAX_CONNECT_S, settings.timeout_s))
        body = bytearray()
        assert settings.url is not None
        async with httpx.AsyncClient(
            transport=self._transport, timeout=timeout, follow_redirects=False
        ) as client:
            async with client.stream(
                "POST",
                settings.url,
                data=form,
                headers={**headers, "Accept": "application/json"},
            ) as response:
                status = response.status_code
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > RESPONSE_MAX_BYTES:
                        raise _Unavailable("unusable issuer response: too large")
        if 400 <= status < 500 and status not in (408, 429):
            raise _Refused(_error_code(bytes(body)) or f"HTTP {status}")
        if status != 200:
            raise _Unavailable(f"HTTP {status}")
        return _issued(bytes(body))


def _error_code(body: bytes) -> str | None:
    """The RFC 6749 `error` code of an error response, when it is one."""
    try:
        data = json.loads(body)
    except ValueError:
        return None
    code = data.get("error") if isinstance(data, dict) else None
    return code if isinstance(code, str) and _ERROR_CODE_RE.fullmatch(code) else None


def _issued(body: bytes) -> tuple[str, int]:
    """`(access_token, expires_in)` of a 200 answer, or `_Unavailable` (an unusable one)."""
    try:
        data = json.loads(body)
    except ValueError:
        raise _Unavailable("unusable issuer response: not JSON") from None
    if not isinstance(data, dict):
        raise _Unavailable("unusable issuer response: not a JSON object")
    token = data.get("access_token")
    if not isinstance(token, str) or not token or len(token) > ACCESS_TOKEN_MAX_CHARS:
        raise _Unavailable("unusable issuer response: access_token")
    token_type = data.get("token_type")
    if not isinstance(token_type, str) or token_type.lower() != "bearer":
        raise _Unavailable("unusable issuer response: token_type is not Bearer")
    issued_type = data.get("issued_token_type")
    if issued_type is not None and issued_type != ACCESS_TOKEN_TYPE:
        raise _Unavailable("unusable issuer response: issued_token_type is not an access token")
    expires_in = data.get("expires_in", DEFAULT_EXPIRES_IN_S)
    if not isinstance(expires_in, int) or isinstance(expires_in, bool) or expires_in <= 0:
        raise _Unavailable("unusable issuer response: expires_in")
    return token, expires_in


def actor_claim(env: Mapping[str, str] | None = None) -> str:
    """The claim naming the agent that presents a token: `AUTH_JWT_ACTOR_CLAIM`, else `act`."""
    env = os.environ if env is None else env
    return (env.get("AUTH_JWT_ACTOR_CLAIM") or "").strip() or ACTOR_CLAIM


def names_no_actor(token: str, claim: str = ACTOR_CLAIM) -> bool:
    """Whether `token` names no actor: it is not a readable signed JWT, or its claims lack `claim`.

    `claim` is a dotted path, read as `jwt` reads it (a top-level claim of that
    exact name first; a null value counts as present: the called agent refuses
    it). The claims are read without verifying the token (the agent it is sent
    to does): only to tell whether the issuer names the agent presenting it. A
    token that cannot be read (opaque, encrypted, not base64url or JSON) names
    no actor as far as this agent can tell: True.
    """
    parts = token.split(".")
    if len(parts) != 3 or not parts[1]:
        return True
    try:
        payload = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        claims = json.loads(payload)
    except (ValueError, RecursionError):  # not base64url, UTF-8 or JSON; nested too deep
        return True
    if not isinstance(claims, dict):
        return True
    if claim in claims:
        return False
    node: Any = claims
    for part in claim.split("."):
        if not isinstance(node, dict) or part not in node:
            return True
        node = node[part]
    return False


_exchanger = TokenExchanger()


def exchanger() -> TokenExchanger:
    """The process's token exchanger."""
    return _exchanger


def reset_token_exchange(
    *,
    clock: Callable[[], float] = time.monotonic,
    wall: Callable[[], float] = time.time,
    transport: httpx.AsyncBaseTransport | None = None,
) -> TokenExchanger:
    """Start from an empty exchanger (tests); returns it."""
    global _exchanger
    _exchanger = TokenExchanger(clock=clock, wall=wall, transport=transport)
    return _exchanger
