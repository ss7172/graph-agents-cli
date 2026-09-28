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

"""Authentication and authorization adapter.

One `AuthPolicy` is selected by `AUTH_POLICY` and applied to every surface:
the chat API and thread routes through the `require(action)` dependency, the
A2A card and JSON-RPC endpoints through the middleware in `fast_api_app.py`,
and, under `langgraph-server`, the server's native API (threads, runs,
assistants, crons, store) through `auth`, a `langgraph_sdk.Auth` object built
from the same policy and referenced by `langgraph.json`. `langgraph_sdk` and
`jwt` (PyJWT) are imported lazily, so a runtime or policy that does not use
them never needs them.

Policies:
  * `SharedBearerPolicy` (`shared-bearer`, default): `Authorization: Bearer <API_KEY>`,
    constant-time compare, one principal `shared`, every action allowed.
  * `JwtPolicy` (`jwt`): per-user principals from a verified OIDC/JWT bearer
    token (`AUTH_JWT_*` settings, see `.env.example`): signature from a JWKS
    URL (cached, refetched on an unknown key id) or one PEM public key,
    issuer, audience, `exp`/`nbf`/`iat` with leeway, an algorithm allow-list.
  * `CustomPolicy` (`custom`): interface plus a fail-closed stub in
    `policies/custom.py`, implemented by the project (for example to validate
    an existing application's session cookie).

Startup fails closed: `check_startup()` builds the selected policy when the
app is assembled (`a2a.add_a2a_routes`) and when LangGraph Server loads
`auth`. An unknown `AUTH_POLICY` stops the process in every environment; a
policy whose `startup_problems()` reports a misconfiguration stops it outside
`APP_ENV=dev` (under dev the problem is logged and every request gets 503).

LangGraph Server's own meta routes (`/docs`, `/openapi.json`, `/info`,
`/metrics`) are outside this auth. `langgraph dev` keeps them (LangGraph
Studio reads `/info`). The server image built from the project's Dockerfile
sets `"disable_meta": true` in its `LANGGRAPH_HTTP`, which removes them all
but `/ok`; this app's own `/metrics` (optionally behind `METRICS_TOKEN`) is
served in their place. A custom image must set the same flag.

The retired name `product-session` is still read as `custom`, with a warning.

Agents calling agents (delegation). A request may come from another agent
acting for a user: a `jwt` token carrying the RFC 8693 `act` claim, or a
principal a `custom` policy marks with `Principal.actor`. The principal's `id`
stays the subject (the user); `actor` names the agent presenting the request.
`finalize_principal` runs right after every policy's `authenticate` and applies
one rule set to all of them: it validates the ids, refuses a chain deeper than
`AUTH_MAX_DELEGATION_DEPTH` (401) and an agent `AUTH_ALLOWED_ACTORS` does not
list (403; none is listed by default), keeps only the roles
`AUTH_DELEGATED_ROLES` lends to agents, and publishes the actor in
`attributes["@actor"]` (a policy's own `@actor` is never trusted). Ownership
is by owner key (`Principal.owner_key`): a direct principal owns everything
done for its subject, a delegated one only what was created under its exact
subject and actor. Privileged role checks (read-across, admin, role approvers)
ignore delegated principals.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from {{cookiecutter.agent_directory}}.app_utils.limits import SettingsError

logger = logging.getLogger(__name__)

# Every action a policy may be asked to authorize. `approval.read` lists the
# approvals of gated API calls and `approval.decide` approves or rejects one;
# who may decide a given approval is then the policy's `approvers`
# (`app_utils.approvals.may_decide`).
ACTIONS: frozenset[str] = frozenset(
    {
        "chat.send",
        "thread.read",
        "thread.list",
        "thread.delete",
        "run.read",
        "a2a.invoke",
        "card.read",
        "approval.read",
        "approval.decide",
    }
)

SHARED_BEARER = "shared-bearer"
JWT = "jwt"
CUSTOM = "custom"
DEFAULT_POLICY = SHARED_BEARER
# Retired policy names still accepted from AUTH_POLICY.
LEGACY_ALIASES = {"product-session": CUSTOM}

# The one attribute key that may hold secrets: api name -> credential string,
# forwarded by `app_utils.api_client` for `auth: forward` APIs.
CREDENTIALS_KEY = "credentials"

# Optional secret key of `Principal.hashed_id()` (HMAC-SHA256); unset = plain sha256.
PRINCIPAL_HASH_SALT_ENV = "PRINCIPAL_HASH_SALT"

# Attribute keys starting with `@` belong to the framework (API names, the keys of
# `credentials`, never start with one). `@actor` is public (persisted with an
# approval's requester, passed in LangGraph Server run context); the `@` keys under
# `credentials` are private, like every credential.
ACTOR_ATTRIBUTE = "@actor"
SUBJECT_TOKEN_CREDENTIAL = "@subject_token"
SUBJECT_AUD_CREDENTIAL = "@subject_aud"
ORIGIN_CREDENTIAL = "@origin"
# Between the subject and the actor in an owner key: the unit separator, which no
# valid id holds (ids have no control characters).
OWNER_KEY_SEPARATOR = "\x1f"
# `AUTH_MAX_DELEGATION_DEPTH`: how many agents may stand between the user and this one.
DEFAULT_MAX_DELEGATION_DEPTH = 3
MIN_DELEGATION_DEPTH = 1
MAX_DELEGATION_DEPTH = 8
# `AUTH_ALLOWED_ACTORS=*` allows any agent (the issuer's audience policy alone decides).
ANY_ACTOR = "*"
# The actor of a jwt token with no `act` whose client `AUTH_JWT_DIRECT_CLIENTS` does not
# list: `client:<azp>` (`client:?` when the token names no client).
CLIENT_ACTOR_PREFIX = "client:"
UNKNOWN_CLIENT = "?"
# The RFC 8693 claim that nests earlier actors inside an actor.
NESTED_ACTOR_CLAIM = "act"
DEFAULT_JWT_CLIENT_CLAIM = "azp"
# Read when the client claim is absent (RFC 9068 access tokens).
FALLBACK_JWT_CLIENT_CLAIM = "client_id"

_TRUE = ("1", "true", "yes", "on")


def dev_mode() -> bool:
    """`APP_ENV` is exactly `dev`. Unset or any other value (`DEV`, ` dev`, `development`)
    is a deployed environment: dev relaxes checks, so only the exact value turns it on."""
    return os.environ.get("APP_ENV") == "dev"


def _csv(raw: str | None) -> list[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def admin_roles() -> set[str]:
    """Roles allowed to manage assistants, crons and the store (`AUTH_ADMIN_ROLES`; empty = nobody)."""
    return set(_csv(os.environ.get("AUTH_ADMIN_ROLES")))


def read_across_roles() -> set[str]:
    """Roles allowed to read other principals' threads (`AUTH_READ_ACROSS_ROLES`)."""
    return set(_csv(os.environ.get("AUTH_READ_ACROSS_ROLES")))


@dataclass(frozen=True)
class Actor:
    """The agent presenting a delegated request. Public: persisted and shown, never secret.

    `id` is the current actor (the outermost `act.sub` of a jwt token,
    `client:<azp>`, or what a custom policy names); `chain` is every actor,
    current first (earlier agents follow); `client` the token's authorized
    party (`azp`/`client_id`) when it names one.
    """

    id: str
    chain: tuple[str, ...] = ()
    client: str | None = None

    def public(self) -> dict[str, Any]:
        """The actor as `attributes["@actor"]` holds it."""
        return {"id": self.id, "chain": list(self.chain or (self.id,)), "client": self.client}

    @classmethod
    def from_public(cls, value: Any) -> Actor | None:
        """An actor from its `public()` form (a persisted requester's), or None when it is not one."""
        if not isinstance(value, Mapping):
            return None
        actor_id = value.get("id")
        if not _valid_id(actor_id):
            return None
        raw = value.get("chain")
        chain = (
            tuple(str(a) for a in raw if _valid_id(a))
            if isinstance(raw, list | tuple)
            else (actor_id,)
        )
        client = value.get("client")
        return cls(
            id=actor_id,
            chain=chain or (actor_id,),
            client=client if isinstance(client, str) else None,
        )


def owner_key_of(subject: str, actor: str | None) -> str:
    """The owner key of a record kept as two columns: the subject, or subject and actor.

    Byte-identical to the subject for a direct owner (no actor, or an empty one),
    so every row written before 0.3 keeps its owner.
    """
    return subject if not actor else f"{subject}{OWNER_KEY_SEPARATOR}{actor}"


@dataclass
class Principal:
    """Who is calling. `id` is what traces and run records use, hashed.

    `id` is always the subject: the user a request acts for (or a service
    acting for itself). `actor` is the agent presenting a delegated request on
    the subject's behalf, None for a direct one (the subject itself calls).

    `attributes` may hold secrets only under `credentials` (api name ->
    credential string). Anything persisted, logged, traced or passed into
    LangGraph Server run context/metadata must use `public_attributes()`.
    """

    id: str
    roles: list[str] = field(default_factory=list)
    permissions: set[str] = field(default_factory=set)
    # Out of repr: `credentials` holds secrets, and a repr ends up in logs and tracebacks.
    attributes: dict[str, Any] = field(default_factory=dict, repr=False)
    actor: Actor | None = None

    @property
    def delegated(self) -> bool:
        """Whether an agent presents this request for the subject (see `actor`)."""
        return self.actor is not None

    def owner_key(self) -> str:
        """What owns the threads and tasks this principal creates: the subject, or the
        subject and the actor (`owner_key_of`). Equal to `id` for a direct principal."""
        return owner_key_of(self.id, self.actor.id if self.actor is not None else None)

    def hashed_id(self) -> str:
        """The id hashed, first 16 hex characters: what logs, traces and run records carry.

        HMAC-SHA256 keyed with `PRINCIPAL_HASH_SALT` when that is set, else
        plain sha256 (the default, kept for compatibility). A secret salt stops
        anyone holding logs or traces from confirming a guessed id (an email
        address, say) by hashing it; changing the salt changes every hash, so
        new hashes no longer match older logs and run records.
        """
        data = self.id.encode("utf-8")
        salt = (os.environ.get(PRINCIPAL_HASH_SALT_ENV) or "").strip()
        if salt:
            return hmac.new(salt.encode("utf-8"), data, hashlib.sha256).hexdigest()[:16]
        return hashlib.sha256(data).hexdigest()[:16]

    def public_attributes(self) -> dict[str, Any]:
        """`attributes` without `credentials`: safe to persist, log or trace."""
        return {k: v for k, v in self.attributes.items() if k != CREDENTIALS_KEY}


def _valid_id(value: Any) -> bool:
    """A principal or actor id: 1-`PRINCIPAL_ID_MAX_CHARS` characters, no control characters."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= PRINCIPAL_ID_MAX_CHARS
        and not _CONTROL_CHARS.search(value)
    )


def keep_subject_token(principal: Principal, token: str, aud: Any = None) -> Principal:
    """Keep the verified inbound bearer and its audience in the principal's private credentials.

    Under `credentials["@subject_token"]` and `credentials["@subject_aud"]` (a
    tuple): the token an API that acts with the user's own identity is called
    with (`auth: exchange` exchanges it, `auth: forward` with `forward_audience`
    forwards it). `jwt` keeps it only when the loaded api-policy has such an API
    (`subject_token_needed`); a custom policy calls this itself. Credentials are
    never persisted, logged or traced (`public_attributes()` drops them).
    """
    if isinstance(aud, str):
        audience: tuple[str, ...] = (aud,)
    elif isinstance(aud, list | tuple):
        audience = tuple(str(a) for a in aud if isinstance(a, str))
    else:
        audience = ()
    credentials = principal.attributes.get(CREDENTIALS_KEY)
    kept = dict(credentials) if isinstance(credentials, Mapping) else {}
    kept[SUBJECT_TOKEN_CREDENTIAL] = token
    kept[SUBJECT_AUD_CREDENTIAL] = audience
    principal.attributes[CREDENTIALS_KEY] = kept
    return principal


def subject_token_needed() -> bool:
    """Whether the loaded api-policy has an API that acts with the caller's own token.

    That is an `auth: exchange` API, or an `auth: forward` one with
    `forward_audience`. Without one (and without a readable policy) no
    subject token is kept.
    """
    try:
        from {{cookiecutter.agent_directory}}.app_utils.api_client import load_policy

        apis = load_policy().apis
    except Exception:
        return False
    return any(api.get("auth") == "exchange" or "forward_audience" in api for api in apis.values())


# ---------------------------------------------------------------------------
# Delegation: one rule set for every policy (`finalize_principal`)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DelegationSettings:
    """`AUTH_MAX_DELEGATION_DEPTH`, `AUTH_ALLOWED_ACTORS` and `AUTH_DELEGATED_ROLES`."""

    max_depth: int = DEFAULT_MAX_DELEGATION_DEPTH
    # The agents that may present a request for a user; `*` in it allows any.
    allowed_actors: frozenset[str] = field(default_factory=lambda: DEFAULT_ALLOWED_ACTORS)
    # The roles a delegated principal keeps (the rest are dropped).
    delegated_roles: frozenset[str] = frozenset()

    def allows(self, actor_id: str) -> bool:
        return ANY_ACTOR in self.allowed_actors or actor_id in self.allowed_actors


def delegation_settings(env: Mapping[str, str] | None = None) -> DelegationSettings:
    """The delegation settings, validated; `SettingsError` names a bad one (a startup error)."""
    env = os.environ if env is None else env
    raw_depth = (env.get("AUTH_MAX_DELEGATION_DEPTH") or "").strip()
    depth = DEFAULT_MAX_DELEGATION_DEPTH
    if raw_depth:
        try:
            depth = int(raw_depth)
        except ValueError:
            raise SettingsError(
                f"AUTH_MAX_DELEGATION_DEPTH={raw_depth!r} is not a whole number."
            ) from None
        if not MIN_DELEGATION_DEPTH <= depth <= MAX_DELEGATION_DEPTH:
            raise SettingsError(
                f"AUTH_MAX_DELEGATION_DEPTH={depth} must be from {MIN_DELEGATION_DEPTH} to "
                f"{MAX_DELEGATION_DEPTH}."
            )
    actors = _csv(env.get("AUTH_ALLOWED_ACTORS"))
    for actor in actors:
        if actor != ANY_ACTOR and (not _valid_id(actor) or any(c.isspace() for c in actor)):
            raise SettingsError(
                f"AUTH_ALLOWED_ACTORS: {actor[:40]!r} is not an agent id (1-256 characters, no "
                "whitespace or control characters), or * for any agent."
            )
    roles = _csv(env.get("AUTH_DELEGATED_ROLES"))
    for role in roles:
        if not _valid_id(role):
            raise SettingsError(f"AUTH_DELEGATED_ROLES: {role[:40]!r} is not a role name.")
    return DelegationSettings(
        max_depth=depth,
        allowed_actors=frozenset(actors) or DEFAULT_ALLOWED_ACTORS,
        delegated_roles=frozenset(roles),
    )


def _principal_problem(principal: Principal) -> str | None:
    """Which id of a principal is invalid (`principal` or `actor`), or None."""
    if not _valid_id(principal.id):
        return "principal"
    actor = principal.actor
    if actor is None:
        return None
    if not _valid_id(actor.id) or not isinstance(actor.chain, tuple):
        return "actor"
    if actor.chain and (actor.chain[0] != actor.id or not all(_valid_id(a) for a in actor.chain)):
        return "actor"
    if actor.client is not None and not _valid_id(actor.client):
        return "actor"
    return None


def finalize_principal(principal: Principal) -> Principal:
    """Apply the delegation rules to what a policy's `authenticate` returned (every policy).

    In this order: the ids are valid (1-256 characters, no control
    characters; a `jwt` token that breaks this is refused with 401, a custom
    policy's principal with 500, logged as a bug in the policy); a delegated
    principal's actor chain is at most `AUTH_MAX_DELEGATION_DEPTH` long (401)
    and its actor is listed in `AUTH_ALLOWED_ACTORS` (403); it keeps only the
    roles `AUTH_DELEGATED_ROLES` lends to agents; and its actor is published
    in `attributes["@actor"]`. A direct principal is returned as it came,
    without any `@actor` a policy put there (only this function sets it).
    """
    try:
        settings = delegation_settings()
    except SettingsError as exc:
        # The startup check refuses to start with this; fail closed if it is reached anyway.
        logger.error("delegation settings are invalid: %s", exc)
        raise HTTPException(
            status_code=503, detail="The server's delegation settings are invalid."
        ) from None
    problem = _principal_problem(principal)
    if problem is not None:
        name = policy_name()
        if name == JWT:
            raise _invalid_token(f"invalid {problem} claim")
        logger.error(
            "AUTH_POLICY=%s returned a principal with an invalid %s id (1-%d characters, no "
            "control characters): a bug in the policy; the request is refused",
            name,
            problem,
            PRINCIPAL_ID_MAX_CHARS,
        )
        raise HTTPException(status_code=500, detail="The auth policy returned an invalid caller.")
    attributes = {k: v for k, v in principal.attributes.items() if k != ACTOR_ATTRIBUTE}
    actor = principal.actor
    if actor is None:
        if ACTOR_ATTRIBUTE not in principal.attributes:
            return principal
        return Principal(
            id=principal.id,
            roles=list(principal.roles),
            permissions=set(principal.permissions),
            attributes=attributes,
        )
    chain = actor.chain or (actor.id,)
    if len(chain) > settings.max_depth:
        raise _invalid_token("delegation too deep")
    if not settings.allows(actor.id):
        raise HTTPException(
            status_code=403,
            detail=f"Delegated caller {actor.id} is not allowed here (AUTH_ALLOWED_ACTORS).",
        )
    actor = Actor(id=actor.id, chain=chain, client=actor.client)
    attributes[ACTOR_ATTRIBUTE] = actor.public()
    return Principal(
        id=principal.id,
        roles=[r for r in principal.roles if r in settings.delegated_roles],
        permissions=set(principal.permissions),
        attributes=attributes,
        actor=actor,
    )


def actor_of_attributes(attributes: Any) -> Actor | None:
    """The actor a principal's (public) attributes carry under `@actor`, or None (direct)."""
    if not isinstance(attributes, Mapping):
        return None
    return Actor.from_public(attributes.get(ACTOR_ATTRIBUTE))


@runtime_checkable
class AuthPolicy(Protocol):
    async def authenticate(self, request: Request) -> Principal:
        """Return the caller or raise `HTTPException(401)`."""
        ...

    async def authorize(self, principal: Principal, action: str, resource: str | None) -> None:
        """Allow `action` on `resource` (a thread id or None) or raise `HTTPException(403)`."""
        ...


class SharedBearerPolicy:
    """`Authorization: Bearer <API_KEY>`; one anonymous principal; every action allowed.

    Suitable for internal tools and development. Conversation ownership is not
    enforced because every caller is the same principal.
    """

    principal_id = "shared"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key

    def expected_key(self) -> str:
        return self._api_key if self._api_key is not None else os.environ.get("API_KEY", "")

    async def authenticate(self, request: Request) -> Principal:
        expected = self.expected_key()
        if not expected:
            # Fail closed: an unset key must never mean "no auth".
            raise HTTPException(
                status_code=503,
                detail="API_KEY is not configured on the server (AUTH_POLICY=shared-bearer).",
            )
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        token = token.strip()
        if (
            scheme.lower() != "bearer"
            or not token
            or not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))
        ):
            raise HTTPException(
                status_code=401,
                detail="Missing or invalid bearer token.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return Principal(id=self.principal_id, roles=["shared"], permissions=set(ACTIONS))

    async def authorize(self, principal: Principal, action: str, resource: str | None) -> None:
        if action not in ACTIONS:
            raise HTTPException(status_code=403, detail=f"Unknown action {action!r}.")


# ---------------------------------------------------------------------------
# JwtPolicy: per-user principals from a verified OIDC/JWT bearer token
# ---------------------------------------------------------------------------

# Asymmetric algorithms and the key they need: (JWK kty, curve or None).
ASYMMETRIC_ALGORITHMS: dict[str, tuple[str, str | None]] = {
    "RS256": ("RSA", None),
    "RS384": ("RSA", None),
    "RS512": ("RSA", None),
    "PS256": ("RSA", None),
    "PS384": ("RSA", None),
    "PS512": ("RSA", None),
    "ES256": ("EC", "P-256"),
    "ES384": ("EC", "P-384"),
    "ES512": ("EC", "P-521"),
    "EdDSA": ("OKP", None),  # Ed25519 or Ed448
}
# Shared-secret algorithms: only with AUTH_JWT_ALLOW_HS=true and AUTH_JWT_SECRET.
HMAC_ALGORITHMS = ("HS256", "HS384", "HS512")
DEFAULT_JWT_ALGORITHMS = "RS256,ES256"
DEFAULT_LEEWAY_S = 60
DEFAULT_JWKS_CACHE_S = 300
# A token longer than this is refused before it is parsed.
JWT_MAX_TOKEN_CHARS = 16_384
# JWKS fetch: time limit, size limit, and how often an unknown key id (or a
# failed fetch) may trigger another request to the issuer.
JWKS_TIMEOUT_S = 5.0
JWKS_MAX_BYTES = 1_048_576
JWKS_REFETCH_INTERVAL_S = 30.0
# When a refresh fails, the last good key set stays usable this much longer
# than AUTH_JWT_JWKS_CACHE_S; after that requests get 503 until the issuer answers.
JWKS_STALE_GRACE_S = 3600.0
HMAC_MIN_SECRET_BYTES = 32
# Delegated callers (0.3). The jwt claim that names the agent acting for the user
# (`AUTH_JWT_ACTOR_CLAIM`, RFC 8693 `act`; set it empty to read every token as the
# user's own, as 0.2 does), and the agents that may act (`AUTH_ALLOWED_ACTORS`, a comma
# list): none by default, so a delegated caller is refused until it is listed.
DEFAULT_JWT_ACTOR_CLAIM = "act"
DEFAULT_ALLOWED_ACTORS: frozenset[str] = frozenset()
PRINCIPAL_ID_MAX_CHARS = 256
MAX_ROLES = 256
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")


def _canonical_algorithm(name: str) -> str | None:
    """`rs256` -> `RS256`, `eddsa` -> `EdDSA`; None for anything not supported."""
    for known in (*ASYMMETRIC_ALGORITHMS, *HMAC_ALGORITHMS):
        if name.lower() == known.lower():
            return known
    return None


def _key_matches_algorithm(algorithm: str, kty: Any, crv: Any = None, key_alg: Any = None) -> bool:
    """True when a key of type `kty`/`crv` (pinned to `key_alg`, if any) may verify `algorithm`.

    Keeps the algorithm families apart (an RSA key never verifies ES256, an
    EC P-384 key never verifies ES256) and honours a JWK's own `alg`.
    """
    spec = ASYMMETRIC_ALGORITHMS.get(algorithm)
    if spec is None:
        return False
    if key_alg is not None and key_alg != algorithm:
        return False
    want_kty, want_crv = spec
    if kty != want_kty:
        return False
    if want_kty == "EC":
        return crv == want_crv
    if want_kty == "OKP":
        return crv in ("Ed25519", "Ed448")
    return True


def _describe_public_key(key: Any) -> tuple[str, str | None]:
    """(kty, crv) of a `cryptography` public key, in JWK terms."""
    from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, rsa

    if isinstance(key, rsa.RSAPublicKey):
        return "RSA", None
    if isinstance(key, ec.EllipticCurvePublicKey):
        curves = {"secp256r1": "P-256", "secp384r1": "P-384", "secp521r1": "P-521"}
        return "EC", curves.get(key.curve.name, key.curve.name)
    if isinstance(key, ed25519.Ed25519PublicKey):
        return "OKP", "Ed25519"
    if isinstance(key, ed448.Ed448PublicKey):
        return "OKP", "Ed448"
    return type(key).__name__, None


def _load_public_key(pem: str) -> Any:
    """A PEM public key or certificate -> a `cryptography` public key. Private keys are refused."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    data = pem.strip().replace("\\n", "\n").encode("utf-8")
    if b"PRIVATE KEY" in data:
        raise ValueError("a private key was given; set the issuer's public key")
    try:
        return serialization.load_pem_public_key(data)
    except ValueError:
        return x509.load_pem_x509_certificate(data).public_key()


def _int_setting(
    env: Mapping[str, str], name: str, default: int, low: int, high: int, problems: list[str]
) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        problems.append(f"{name} must be a whole number of seconds, got {raw!r}")
        return default
    if not low <= value <= high:
        problems.append(f"{name} must be between {low} and {high}, got {value}")
        return default
    return value


@dataclass
class JwtSettings:
    """The `AUTH_JWT_*` settings, validated. `problems` lists what makes them unusable."""

    algorithms: tuple[str, ...] = ()
    jwks_url: str | None = None
    public_key: Any = None
    public_key_type: tuple[str, str | None] = ("", None)
    secret: bytes | None = None
    issuer: str | None = None
    audience: tuple[str, ...] = ()
    principal_claim: str = "sub"
    roles_claim: str = "roles"
    # Delegation (RFC 8693): the actor claim ("" = not read, the 0.2 behaviour), the
    # client claim, and the clients of human sign-in (empty = every token without an
    # actor claim is direct).
    actor_claim: str = DEFAULT_JWT_ACTOR_CLAIM
    client_claim: str = DEFAULT_JWT_CLIENT_CLAIM
    direct_clients: frozenset[str] = frozenset()
    leeway_s: int = DEFAULT_LEEWAY_S
    jwks_cache_s: int = DEFAULT_JWKS_CACHE_S
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> JwtSettings:
        env = os.environ if env is None else env
        dev = env.get("APP_ENV") == "dev"  # exactly, as dev_mode()
        s = cls()
        problems, warnings = s.problems, s.warnings

        # Algorithms: an explicit allow-list; `none` never, HS* only when opted in.
        algorithms: list[str] = []
        for name in _csv(env.get("AUTH_JWT_ALGORITHMS") or DEFAULT_JWT_ALGORITHMS):
            canonical = _canonical_algorithm(name)
            if name.lower() == "none":
                problems.append("AUTH_JWT_ALGORITHMS must never include 'none'")
            elif canonical is None:
                problems.append(f"AUTH_JWT_ALGORITHMS: unsupported algorithm {name!r}")
            elif canonical not in algorithms:
                algorithms.append(canonical)
        if not algorithms and not problems:
            problems.append("AUTH_JWT_ALGORITHMS lists no algorithm")
        s.algorithms = tuple(algorithms)

        hmac_algs = [a for a in algorithms if a in HMAC_ALGORITHMS]
        if hmac_algs:
            secret = env.get("AUTH_JWT_SECRET") or ""
            if (env.get("AUTH_JWT_ALLOW_HS") or "").strip().lower() not in _TRUE:
                problems.append(
                    f"AUTH_JWT_ALGORITHMS lists {', '.join(hmac_algs)}: shared-secret "
                    "algorithms need AUTH_JWT_ALLOW_HS=true and AUTH_JWT_SECRET"
                )
            elif not secret:
                problems.append("AUTH_JWT_ALLOW_HS=true needs AUTH_JWT_SECRET")
            elif "-----BEGIN" in secret:
                problems.append("AUTH_JWT_SECRET must be a shared secret, not a PEM key")
            elif len(secret.encode("utf-8")) < HMAC_MIN_SECRET_BYTES:
                problems.append(
                    f"AUTH_JWT_SECRET must be at least {HMAC_MIN_SECRET_BYTES} bytes long"
                )
            else:
                s.secret = secret.encode("utf-8")

        # Keys for the asymmetric algorithms: a JWKS URL or one PEM public key.
        asymmetric = [a for a in algorithms if a in ASYMMETRIC_ALGORITHMS]
        jwks_url = (env.get("AUTH_JWT_JWKS_URL") or "").strip()
        pem = (env.get("AUTH_JWT_PUBLIC_KEY") or "").strip()
        if asymmetric:
            if jwks_url and pem:
                problems.append("set AUTH_JWT_JWKS_URL or AUTH_JWT_PUBLIC_KEY, not both")
            elif not jwks_url and not pem:
                problems.append(
                    "no verification key: set AUTH_JWT_JWKS_URL (the issuer's JWKS) "
                    "or AUTH_JWT_PUBLIC_KEY (a PEM public key)"
                )
            elif jwks_url:
                parts = urlsplit(jwks_url)
                host = (parts.hostname or "").lower()
                insecure_ok = (
                    dev
                    or host in _LOOPBACK_HOSTS
                    or (env.get("AUTH_JWT_JWKS_ALLOW_HTTP") or "").strip().lower() in _TRUE
                )
                if parts.scheme not in ("https", "http") or not host:
                    problems.append("AUTH_JWT_JWKS_URL must be an https:// URL")
                elif parts.username or parts.password:
                    problems.append("AUTH_JWT_JWKS_URL must not carry credentials")
                elif parts.scheme == "http" and not insecure_ok:
                    problems.append(
                        "AUTH_JWT_JWKS_URL must use https outside APP_ENV=dev (keys fetched "
                        "over plain http can be replaced in transit); set "
                        "AUTH_JWT_JWKS_ALLOW_HTTP=true only for a trusted in-cluster issuer"
                    )
                else:
                    s.jwks_url = jwks_url
            else:
                try:
                    key = _load_public_key(pem)
                except Exception as exc:  # never echo the key material
                    reason = (
                        str(exc) if isinstance(exc, ValueError) and "private" in str(exc) else ""
                    )
                    problems.append(
                        "AUTH_JWT_PUBLIC_KEY is not a PEM public key or certificate"
                        + (f" ({reason})" if reason else "")
                    )
                else:
                    kty, crv = _describe_public_key(key)
                    if not any(_key_matches_algorithm(a, kty, crv) for a in asymmetric):
                        problems.append(
                            f"AUTH_JWT_PUBLIC_KEY is a {kty}{' ' + crv if crv else ''} key, "
                            f"which verifies none of AUTH_JWT_ALGORITHMS ({', '.join(asymmetric)})"
                        )
                    else:
                        s.public_key, s.public_key_type = key, (kty, crv)

        # Issuer and audience: required outside dev.
        s.issuer = (env.get("AUTH_JWT_ISSUER") or "").strip() or None
        s.audience = tuple(_csv(env.get("AUTH_JWT_AUDIENCE")))
        for name, value in (("AUTH_JWT_ISSUER", s.issuer), ("AUTH_JWT_AUDIENCE", s.audience)):
            if value:
                continue
            if dev:
                warnings.append(
                    f"{name} is not set: it is not checked (allowed under APP_ENV=dev only)"
                )
            else:
                problems.append(f"{name} is required outside APP_ENV=dev")

        s.principal_claim = (env.get("AUTH_JWT_PRINCIPAL_CLAIM") or "sub").strip()
        s.roles_claim = (env.get("AUTH_JWT_ROLES_CLAIM") or "roles").strip()
        for name, claim in (
            ("AUTH_JWT_PRINCIPAL_CLAIM", s.principal_claim),
            ("AUTH_JWT_ROLES_CLAIM", s.roles_claim),
        ):
            if not claim or any(not part for part in claim.split(".")):
                problems.append(f"{name} must be a claim name or a dotted path, got {claim!r}")

        # AUTH_JWT_ACTOR_CLAIM set to nothing turns delegation off (0.2); unset is `act`.
        raw_actor = env.get("AUTH_JWT_ACTOR_CLAIM")
        s.actor_claim = DEFAULT_JWT_ACTOR_CLAIM if raw_actor is None else raw_actor.strip()
        s.client_claim = (env.get("AUTH_JWT_CLIENT_CLAIM") or DEFAULT_JWT_CLIENT_CLAIM).strip()
        for name, claim in (
            ("AUTH_JWT_ACTOR_CLAIM", s.actor_claim),
            ("AUTH_JWT_CLIENT_CLAIM", s.client_claim),
        ):
            if claim and any(not part for part in claim.split(".")):
                problems.append(f"{name} must be a claim name or a dotted path, got {claim!r}")
        if not s.client_claim:
            problems.append("AUTH_JWT_CLIENT_CLAIM must be a claim name or a dotted path")
        s.direct_clients = frozenset(_csv(env.get("AUTH_JWT_DIRECT_CLIENTS")))

        s.leeway_s = _int_setting(env, "AUTH_JWT_LEEWAY_S", DEFAULT_LEEWAY_S, 0, 600, problems)
        s.jwks_cache_s = _int_setting(
            env, "AUTH_JWT_JWKS_CACHE_S", DEFAULT_JWKS_CACHE_S, 1, 86_400, problems
        )
        return s


class JwksUnavailable(Exception):
    """No usable key set: the JWKS URL did not answer and no recent keys are cached."""


def _usable_signing_jwk(jwk: Any) -> bool:
    if not isinstance(jwk, dict) or jwk.get("kty") not in ("RSA", "EC", "OKP"):
        return False  # symmetric (`oct`) keys from a JWKS are never trusted
    if jwk.get("use") not in (None, "sig"):
        return False
    ops = jwk.get("key_ops")
    return ops is None or (isinstance(ops, list) and "verify" in ops)


class JwksCache:
    """The issuer's signing keys, fetched from a JWKS URL and cached.

    Keys are refetched when the cache (`ttl_s`) expires and when a token
    names a key id the cache does not know (key rotation).

    * Single flight: at most one fetch runs at a time; every request that
      needs it waits for that fetch and shares its result (a rotated key is
      fetched once however many requests carry it).
    * Stale while revalidate: once the cache has expired, requests keep
      verifying with the cached keys while a background fetch refreshes them,
      so a slow or hanging issuer never stalls a request the cached keys can
      verify. A failed refresh keeps the last good keys for `stale_grace_s`
      more; after that requests wait for a fetch and get 503 when it fails.
    * Bounded: fetch attempts, failed ones included, are at most one per
      `refetch_interval_s` (a flood of tokens with random key ids cannot flood
      the issuer), and each has a total deadline of twice `timeout_s`.
    """

    def __init__(
        self,
        url: str,
        ttl_s: float,
        *,
        timeout_s: float = JWKS_TIMEOUT_S,
        refetch_interval_s: float = JWKS_REFETCH_INTERVAL_S,
        stale_grace_s: float = JWKS_STALE_GRACE_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url = url
        self.ttl_s = ttl_s
        self.timeout_s = timeout_s
        self.refetch_interval_s = min(refetch_interval_s, ttl_s)
        self.stale_grace_s = stale_grace_s
        self._clock = clock
        self._keys: list[dict[str, Any]] = []
        self._fetched_at: float | None = None
        self._last_attempt: float | None = None
        self._task: asyncio.Task[bool] | None = None

    def _age(self, now: float) -> float | None:
        return None if self._fetched_at is None else now - self._fetched_at

    def _fresh(self, now: float) -> bool:
        age = self._age(now)
        return age is not None and age < self.ttl_s

    def _usable(self, now: float) -> bool:
        age = self._age(now)
        return age is not None and age < self.ttl_s + self.stale_grace_s

    def _may_attempt(self, now: float) -> bool:
        return self._last_attempt is None or now - self._last_attempt >= self.refetch_interval_s

    def _in_flight(self) -> asyncio.Task[bool] | None:
        """The fetch running on this event loop, if any."""
        task = self._task
        if task is None or task.done() or task.get_loop() is not asyncio.get_running_loop():
            return None
        return task

    def _start_fetch(self) -> asyncio.Task[bool]:
        # No await between the callers' checks and this: one fetch per loop.
        previous_attempt, self._last_attempt = self._last_attempt, self._clock()
        self._task = asyncio.get_running_loop().create_task(self._refresh(previous_attempt))
        return self._task

    @staticmethod
    async def _join(task: asyncio.Task[bool]) -> bool:
        # Shielded: a request that goes away (client disconnect) does not
        # cancel the fetch other requests are waiting for.
        return await asyncio.shield(task)

    async def keys(self) -> list[dict[str, Any]]:
        """The current key set; raises `JwksUnavailable` when there is none to use."""
        now = self._clock()
        if self._fresh(now):
            return self._keys
        task = self._in_flight()
        if task is None and self._may_attempt(now):
            task = self._start_fetch()
        if self._usable(now):
            return self._keys  # the refresh (if any) completes in the background
        if task is not None:
            await self._join(task)
        if self._usable(self._clock()):
            return self._keys
        raise JwksUnavailable(self.url)

    async def refresh_for_unknown_kid(self, kid: str | None = None) -> bool:
        """Refetch because a token named an unknown key id `kid`.

        True when the key set is worth looking at again: another request's
        fetch already brought `kid`, or the fetch this call started or joined
        succeeded. False when rate-limited or the fetch failed.
        """
        if kid is not None and any(k.get("kid") == kid for k in self._keys):
            return True
        task = self._in_flight()
        if task is None:
            if not self._may_attempt(self._clock()):
                return False
            task = self._start_fetch()
        return await self._join(task)

    async def idle(self) -> None:
        """Wait until no fetch is running (for tests and orderly shutdown)."""
        task = self._in_flight()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def _refresh(self, previous_attempt: float | None) -> bool:
        try:
            # A total deadline too: the per-read timeout alone lets a server that
            # trickles bytes keep a fetch (and the requests waiting on it) going.
            keys = await asyncio.wait_for(self._fetch(), timeout=2 * self.timeout_s)
        except asyncio.CancelledError:
            # Stopped from outside (the event loop is shutting down), not the
            # issuer's fault: the next request may try again at once.
            self._last_attempt = previous_attempt
            raise
        except Exception as exc:
            logger.warning(
                "jwt: could not fetch the JWKS from %s (%s); %s",
                self.url,
                type(exc).__name__,
                "keeping the previous keys for now"
                if self._usable(self._clock())
                else "no signing keys are available",
            )
            return False
        self._keys, self._fetched_at = keys, self._clock()
        return True

    async def _fetch(self) -> list[dict[str, Any]]:
        import httpx

        body = bytearray()
        async with httpx.AsyncClient(timeout=self.timeout_s, follow_redirects=False) as client:
            async with client.stream(
                "GET", self.url, headers={"Accept": "application/json"}
            ) as response:
                if response.status_code != 200:
                    raise ValueError(f"HTTP {response.status_code}")
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > JWKS_MAX_BYTES:
                        raise ValueError("JWKS response too large")
        data = json.loads(bytes(body))
        if not isinstance(data, dict) or not isinstance(data.get("keys"), list):
            raise ValueError("not a JWK set")
        keys = [k for k in data["keys"] if _usable_signing_jwk(k)]
        if not keys:
            raise ValueError("the JWK set has no usable signing keys")
        return keys


def _claim(claims: Mapping[str, Any], path: str) -> Any:
    """A claim by name, or by dotted path (`realm_access.roles`); None when absent.

    A top-level claim whose name itself contains dots (for example a
    namespaced `https://example.com/roles`) wins over the dotted reading.
    """
    if path in claims:
        return claims[path]
    current: Any = claims
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


_ABSENT = object()


def _claim_or_absent(claims: Mapping[str, Any], path: str) -> Any:
    """As `_claim`, but `_ABSENT` for a claim the token does not have (a null one is present)."""
    if path in claims:
        return claims[path]
    current: Any = claims
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return _ABSENT
        current = current[part]
    return current


def _actor_chain(value: Any) -> tuple[str, ...]:
    """The actors of an RFC 8693 `act` claim, current first; 401 for a malformed one.

    Each level is a mapping with a string `sub`, and may nest the earlier actor
    under `act`. A chain longer than `MAX_DELEGATION_DEPTH` is refused while it
    is walked.
    """
    chain: list[str] = []
    node = value
    while True:
        if not isinstance(node, Mapping):
            raise _invalid_token("invalid actor claim")
        sub = node.get("sub")
        if not _valid_id(sub):
            raise _invalid_token("invalid actor claim")
        chain.append(sub)
        if len(chain) > MAX_DELEGATION_DEPTH:
            raise _invalid_token("invalid actor claim")
        if NESTED_ACTOR_CLAIM not in node:
            return tuple(chain)
        node = node[NESTED_ACTOR_CLAIM]


def actor_from_claims(
    claims: Mapping[str, Any],
    *,
    actor_claim: str = DEFAULT_JWT_ACTOR_CLAIM,
    client_claim: str = DEFAULT_JWT_CLIENT_CLAIM,
    direct_clients: frozenset[str] | set[str] = frozenset(),
    subject: str | None = None,
) -> Actor | None:
    """The actor of a verified token's claims (None: a direct request), as `jwt` reads it.

    * `actor_claim` present (RFC 8693 `act`, default): delegated. It must be a
      mapping with a string `sub`, each nested `act` likewise; anything else
      is refused (401 `invalid actor claim`), never read as a direct request.
    * absent: direct, unless `direct_clients` is set and the token's client
      (`client_claim`, default `azp`, else `client_id`) is not in it: then an
      agent presents it, as `client:<client>` (`client:?` without one). A
      token whose `subject` is its own client (a service's own token) is direct.
    * `actor_claim` empty: direct (the 0.2 reading).

    `may_act`, `scope` and other claims are not read. For custom policies that
    verify tokens themselves; `finalize_principal` checks the rest.
    """
    raw_client = _claim(claims, client_claim) if client_claim else None
    if raw_client is None:
        raw_client = claims.get(FALLBACK_JWT_CLIENT_CLAIM)
    client = raw_client if _valid_id(raw_client) else None
    if actor_claim:
        value = _claim_or_absent(claims, actor_claim)
        if value is not _ABSENT:
            chain = _actor_chain(value)
            return Actor(id=chain[0], chain=chain, client=client)
    if not direct_clients or client in direct_clients:
        return None
    if subject is not None and client is not None and subject == client:
        return None
    actor_id = f"{CLIENT_ACTOR_PREFIX}{client or UNKNOWN_CLIENT}"
    return Actor(id=actor_id, chain=(actor_id,), client=client)


def _roles_from_claim(value: Any) -> list[str]:
    """A list of strings, or one space/comma separated string -> distinct role names."""
    if isinstance(value, str):
        candidates: list[Any] = re.split(r"[\s,]+", value)
    elif isinstance(value, list | tuple):
        candidates = list(value)
    else:
        return []
    roles: list[str] = []
    for item in candidates:
        if not isinstance(item, str):
            continue
        role = item.strip()
        if (
            role
            and role not in roles
            and len(role) <= PRINCIPAL_ID_MAX_CHARS
            and "," not in role
            and not _CONTROL_CHARS.search(role)
        ):
            roles.append(role)
        if len(roles) >= MAX_ROLES:
            break
    return roles


def _invalid_token(reason: str) -> HTTPException:
    """401 with the RFC 6750 challenge. `reason` is a fixed phrase, never token content."""
    return HTTPException(
        status_code=401,
        detail=f"Invalid bearer token: {reason}.",
        headers={"WWW-Authenticate": f'Bearer error="invalid_token", error_description="{reason}"'},
    )


def _bearer_token(request: Request) -> str:
    scheme, _, token = (request.headers.get("authorization") or "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=401,
            detail="Missing bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


JWT_NOT_CONFIGURED = (
    "AUTH_POLICY=jwt is not configured on the server; every request is refused "
    "until it is (see the server log)."
)


class JwtPolicy:
    """Per-user principals from a verified OIDC/JWT bearer token.

    The principal id is the `AUTH_JWT_PRINCIPAL_CLAIM` claim (default `sub`)
    and the roles come from `AUTH_JWT_ROLES_CLAIM` (default `roles`; a
    dotted path, a list or a space/comma separated string). The actor, for a
    token an agent presents for the user, is read from the RFC 8693 actor
    claim (`AUTH_JWT_ACTOR_CLAIM`, default `act`; see `actor_from_claims`). Every action is
    allowed to an authenticated principal; conversation ownership is enforced
    per thread (`app_utils.threads`, the LangGraph Server owner filters).
    Nothing from the token is logged or put in an error message.
    """

    def __init__(self, settings: JwtSettings | None = None) -> None:
        self.settings = settings if settings is not None else JwtSettings.from_env()
        s = self.settings
        self.jwks = JwksCache(s.jwks_url, s.jwks_cache_s) if s.jwks_url and not s.problems else None
        for warning in s.warnings:
            logger.warning("AUTH_POLICY=jwt: %s", warning)
        if s.problems:
            logger.error(
                "AUTH_POLICY=jwt is misconfigured: %s. Every request is refused (503) until "
                "it is fixed.",
                "; ".join(s.problems),
            )
        elif s.actor_claim and _csv(os.environ.get("AUTH_ALLOWED_ACTORS")) and not s.direct_clients:
            logger.warning(
                "AUTH_POLICY=jwt: delegation is recognised only by the %s claim; if your "
                "issuer's exchanged tokens carry none, set AUTH_JWT_DIRECT_CLIENTS",
                s.actor_claim,
            )

    def startup_problems(self) -> list[str]:
        return list(self.settings.problems)

    async def authenticate(self, request: Request) -> Principal:
        if self.settings.problems:
            raise HTTPException(status_code=503, detail=JWT_NOT_CONFIGURED)
        token = _bearer_token(request)
        claims = await self.verify(token)
        principal = self.principal_from_claims(claims)
        if subject_token_needed():
            # An API acts with the user's own token (exchanged, or forwarded to its audience).
            keep_subject_token(principal, token, claims.get("aud"))
        return principal

    async def authorize(self, principal: Principal, action: str, resource: str | None) -> None:
        if action not in ACTIONS:
            raise HTTPException(status_code=403, detail=f"Unknown action {action!r}.")

    async def verify(self, token: str) -> dict[str, Any]:
        """The token's claims after every check; raises 401 (or 503 when keys are unavailable)."""
        import jwt

        s = self.settings
        if len(token) > JWT_MAX_TOKEN_CHARS:
            raise _invalid_token("token too large")
        try:
            header = jwt.get_unverified_header(token)
        except (jwt.PyJWTError, ValueError, TypeError):
            raise _invalid_token("malformed token") from None
        algorithm = header.get("alg")
        if not isinstance(algorithm, str) or algorithm not in s.algorithms:
            raise _invalid_token("signing algorithm not allowed")
        if "crit" in header:
            raise _invalid_token("unsupported critical header")
        kid = header.get("kid")
        if kid is not None and not isinstance(kid, str):
            raise _invalid_token("malformed token")
        key = await self._key_for(algorithm, kid)
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=[algorithm],
                audience=list(s.audience) or None,
                issuer=s.issuer,
                leeway=s.leeway_s,
                options={
                    "require": ["exp"],
                    "verify_aud": bool(s.audience),
                    "verify_iss": bool(s.issuer),
                },
            )
        except jwt.ExpiredSignatureError:
            raise _invalid_token("token expired") from None
        except jwt.ImmatureSignatureError:
            raise _invalid_token("token not yet valid") from None
        except jwt.InvalidAudienceError:
            raise _invalid_token("wrong audience") from None
        except jwt.InvalidIssuerError:
            raise _invalid_token("wrong issuer") from None
        except jwt.MissingRequiredClaimError:
            raise _invalid_token("missing required claim") from None
        except jwt.InvalidSignatureError:
            raise _invalid_token("invalid signature") from None
        except (jwt.PyJWTError, ValueError, TypeError, OverflowError):
            raise _invalid_token("invalid token") from None
        if not isinstance(claims, dict):
            raise _invalid_token("invalid token")
        return claims

    async def _key_for(self, algorithm: str, kid: str | None) -> Any:
        s = self.settings
        if algorithm in HMAC_ALGORITHMS:
            return s.secret
        if s.public_key is not None:
            if not _key_matches_algorithm(algorithm, *s.public_key_type):
                raise _invalid_token("signing algorithm does not match the key")
            return s.public_key
        if self.jwks is None:  # unreachable with valid settings; fail closed anyway
            raise HTTPException(status_code=503, detail=JWT_NOT_CONFIGURED)
        keys = await self._jwks_keys()
        jwk = self._select(keys, algorithm, kid)
        if jwk is None and kid is not None and all(k.get("kid") != kid for k in keys):
            # Possibly a rotated key: refetch (one fetch shared by every request
            # carrying it, rate-limited) and look again.
            if await self.jwks.refresh_for_unknown_kid(kid):
                keys = await self._jwks_keys()
                jwk = self._select(keys, algorithm, kid)
        if jwk is None:
            raise _invalid_token("unknown signing key")
        from jwt.algorithms import get_default_algorithms

        try:
            return get_default_algorithms()[algorithm].from_jwk(jwk)
        except Exception:
            raise _invalid_token("unusable signing key") from None

    async def _jwks_keys(self) -> list[dict[str, Any]]:
        assert self.jwks is not None
        try:
            return await self.jwks.keys()
        except JwksUnavailable:
            raise HTTPException(
                status_code=503,
                detail="The token issuer's signing keys are unavailable; try again later.",
            ) from None

    @staticmethod
    def _select(
        keys: list[dict[str, Any]], algorithm: str, kid: str | None
    ) -> dict[str, Any] | None:
        matching = [
            k
            for k in keys
            if _key_matches_algorithm(algorithm, k.get("kty"), k.get("crv"), k.get("alg"))
        ]
        if kid is not None:
            matching = [k for k in matching if k.get("kid") == kid]
            return matching[0] if matching else None
        # No key id: only unambiguous when exactly one key fits the algorithm.
        return matching[0] if len(matching) == 1 else None

    def principal_from_claims(self, claims: Mapping[str, Any]) -> Principal:
        s = self.settings
        raw = _claim(claims, s.principal_claim)
        if isinstance(raw, bool) or not isinstance(raw, str | int):
            raise _invalid_token("missing principal claim")
        principal_id = str(raw).strip()
        if (
            not principal_id
            or len(principal_id) > PRINCIPAL_ID_MAX_CHARS
            or _CONTROL_CHARS.search(principal_id)
        ):
            raise _invalid_token("invalid principal claim")
        return Principal(
            id=principal_id,
            roles=_roles_from_claim(_claim(claims, s.roles_claim)),
            permissions=set(ACTIONS),
            actor=actor_from_claims(
                claims,
                actor_claim=s.actor_claim,
                client_claim=s.client_claim,
                direct_clients=s.direct_clients,
                subject=principal_id,
            ),
        )


# ---------------------------------------------------------------------------
# Policy selection
# ---------------------------------------------------------------------------

_policies: dict[str, AuthPolicy] = {}
_warned_aliases: set[str] = set()


def policy_name() -> str:
    """The selected policy; a retired name is read as its replacement (warned once)."""
    name = (os.environ.get("AUTH_POLICY") or DEFAULT_POLICY).strip().lower()
    alias = LEGACY_ALIASES.get(name)
    if alias is None:
        return name
    if name not in _warned_aliases:
        _warned_aliases.add(name)
        logger.warning(
            "AUTH_POLICY=%s is deprecated and read as %s; set AUTH_POLICY=%s.", name, alias, alias
        )
    return alias


def get_policy() -> AuthPolicy:
    """The policy instance for `AUTH_POLICY` (built once per name)."""
    name = policy_name()
    policy = _policies.get(name)
    if policy is None:
        from {{cookiecutter.agent_directory}}.policies import build_policy

        policy = build_policy(name)
        _policies[name] = policy
    return policy


def reset_policy_cache() -> None:
    """For tests that switch `AUTH_POLICY` (or its settings)."""
    _policies.clear()


def check_startup() -> AuthPolicy:
    """Build the selected policy and refuse to start on a configuration that cannot work.

    An unknown `AUTH_POLICY` raises in every environment. A policy may expose
    `startup_problems() -> list[str]`; any problem raises outside `APP_ENV=dev`
    (under dev it is logged and requests get 503 until it is fixed).
    """
    policy = get_policy()
    probe = getattr(policy, "startup_problems", None)
    problems = list(probe()) if callable(probe) else []
    if problems and not dev_mode():
        raise RuntimeError(
            f"AUTH_POLICY={policy_name()} is misconfigured, refusing to start: "
            + "; ".join(problems)
        )
    return policy


def _as_http_exception(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, NotImplementedError):
        return HTTPException(
            status_code=503,
            detail=str(exc) or "The configured AUTH_POLICY is not implemented.",
        )
    raise exc


def require(action: str) -> Callable[[Request], Awaitable[Principal]]:
    """FastAPI dependency: authenticate, then authorize `action` on the route's thread.

    Usage: ``principal: Principal = Depends(require("chat.send"))``.
    """
    if action not in ACTIONS:
        raise ValueError(f"Unknown action {action!r}; expected one of {sorted(ACTIONS)}")

    async def dependency(request: Request) -> Principal:
        policy = get_policy()
        try:
            principal = finalize_principal(await policy.authenticate(request))
            await policy.authorize(principal, action, request.path_params.get("thread_id"))
        except (HTTPException, NotImplementedError) as exc:
            raise _as_http_exception(exc) from exc
        request.state.principal = principal
        return principal

    dependency.__name__ = f"require_{action.replace('.', '_')}"
    return dependency


async def authorize_action(principal: Principal, action: str, resource: str | None = None) -> None:
    """Authorize `action` for an authenticated principal (as `require`: 403, or 503).

    For decisions taken outside a route of their own, such as an approval
    decided over A2A: the endpoint authorized `a2a.invoke`, the decision
    needs `approval.decide` as well.
    """
    if action not in ACTIONS:
        raise ValueError(f"Unknown action {action!r}; expected one of {sorted(ACTIONS)}")
    try:
        await get_policy().authorize(principal, action, resource)
    except (HTTPException, NotImplementedError) as exc:
        raise _as_http_exception(exc) from exc


async def authenticate_and_authorize(
    request: Request, action: str, resource: str | None = None
) -> Principal:
    """Same check as `require`, for code paths outside FastAPI's dependency system."""
    policy = get_policy()
    try:
        principal = finalize_principal(await policy.authenticate(request))
        await policy.authorize(principal, action, resource)
    except (HTTPException, NotImplementedError) as exc:
        raise _as_http_exception(exc) from exc
    return principal


# ---------------------------------------------------------------------------
# LangGraph Server auth handler (langgraph.json "auth": {"path": ".../auth.py:auth"})
# ---------------------------------------------------------------------------

ROLE_PERMISSION_PREFIX = "role:"
# The delegated caller's actor, carried beside the roles in the server's user
# permissions (`actor:<id>`; none for a direct caller).
ACTOR_PERMISSION_PREFIX = "actor:"

# Where `build_sdk_auth` leaves the policy's 401 challenge (`WWW-Authenticate`)
# in the request's ASGI state: LangGraph Server drops the headers of an auth
# error, and `middleware.AuthErrorMiddleware` puts them back.
AUTH_CHALLENGE_STATE_KEY = "auth_challenge"

# The native API's thread copy (`POST /threads/{thread_id}/copy`).
_THREAD_COPY_PATH = re.compile(r"(?:^|/)threads/[^/]+/copy/?$")
# Whether the request being authorized is a thread copy. Set by the
# authenticate handler for every request it sees (the resource handlers run
# later in the same request, in the same context).
_THREAD_COPY: ContextVar[bool] = ContextVar("thread_copy", default=False)


# Run settings that start a run from a given checkpoint (a replay) instead of the latest.
_CHECKPOINT_KEYS = ("checkpoint_id", "checkpoint", "checkpoint_ns", "checkpoint_map")


def _replays(kwargs: Any) -> bool:
    """Whether a native run re-runs a thread's pending step: it has no input, or
    starts from a checkpoint (in `config.configurable` or `context`, where the
    server puts `checkpoint_id` and `checkpoint`)."""
    if not isinstance(kwargs, Mapping):
        return True
    if kwargs.get("input") is None:
        return True
    config = kwargs.get("config")
    configurable = config.get("configurable") if isinstance(config, Mapping) else None
    for settings in (configurable, kwargs.get("context")):
        if isinstance(settings, Mapping) and any(
            settings.get(key) not in (None, "") for key in _CHECKPOINT_KEYS
        ):
            return True
    return False


def _is_thread_copy(request: Any) -> bool:
    scope = getattr(request, "scope", None) or {}
    path = scope.get("path") or ""
    return scope.get("method") == "POST" and bool(_THREAD_COPY_PATH.search(path))


def _stash_challenge(request: Any, headers: Mapping[str, str] | None) -> None:
    """Keep a 401's `WWW-Authenticate` in the request state (see `AuthErrorMiddleware`)."""
    challenge = next(
        (v for k, v in (headers or {}).items() if k.lower() == "www-authenticate"), None
    )
    scope = getattr(request, "scope", None)
    if challenge and isinstance(scope, dict):
        state = scope.setdefault("state", {})
        if isinstance(state, dict):
            state[AUTH_CHALLENGE_STATE_KEY] = challenge


def build_sdk_auth() -> Any:
    """A `langgraph_sdk.Auth` whose handlers delegate to the selected policy.

    `@auth.authenticate` runs the policy's `authenticate`. Resource rules for
    the server's native API:

    * threads (and their runs): `{principal_id, tenant}` is written into the
      thread metadata at creation (the caller's id; `tenant` None, since a
      native API caller cannot vouch for one) and every read, search, update,
      delete and run is filtered to the caller's own threads. A role listed in
      `AUTH_READ_ACROSS_ROLES` relaxes only the read and search filters
      (read-across roles may *read* others' threads, never change them), and
      an update can never change a thread's `principal_id` or `tenant`.
      A delegated caller (an agent acting for the user: `actor:<id>` in its
      permissions) also stamps `actor` and is filtered to the threads of its
      own subject and actor; the filters are containment filters, so the
      subject's own (direct) filter matches the threads its agents started
      for it. Nobody stamps, adopts or strips an actor by sending metadata,
      and a delegated caller's roles never read across or administer.
      A copy (`POST /threads/{id}/copy`) is a write: it creates a thread that
      keeps the source's metadata, owner included, so its source must be the
      caller's own thread whatever the caller's roles. A thread that recorded
      approvals of gated API calls is not copied (403): the copy would carry
      its tool calls without their approvals.
    * a run that carries a `command` (a resume of a paused run) is refused:
      a run paused for the approval of a gated API call resumes only through
      the app's approval routes, which check who may decide (the app's
      approvals ledger refuses a forged resume anyway). A new run on a thread
      whose approval is still pending is refused with 409, as `/chat` does.
      A run without input, or from a checkpoint (`checkpoint_id`,
      `checkpoint`: a replay), is refused (403) on a thread that recorded
      approvals or waits on a gated call: it would run a paused step's tool
      calls again without a decision (the API client refuses a call an
      approval was asked for anyway).
    * the raw principal id is kept only in the thread metadata, where the
      owner filters need it. The server merges thread metadata into every
      run's metadata (and from there into traced config metadata and
      checkpoint metadata); a run on an existing thread therefore carries the
      hashed id (`Principal.hashed_id()`) under `principal_id` instead.
    * assistants, crons, store: any authenticated principal may read (read,
      search, get, list_namespaces); create, update, put and delete are
      allowed only to a role listed in `AUTH_ADMIN_ROLES` (empty = nobody).
      The assistant `/chat` runs on is therefore changeable only by admins.
      Store reads are not per-user: a graph that writes per-user data into
      the store must scope its namespaces by principal.
    * anything else the server dispatches (a resource or action added by a
      later server version): denied (default deny).

    These handlers cover the native API called from outside the app; the
    custom routes' loopback SDK calls bypass the server's auth middleware, so
    `chat.py` enforces the thread rule in-app.

    The server answers an auth error with a bare 401/403 (without the policy's
    `WWW-Authenticate` challenge) and turns any other status (a policy's 503)
    into a 500; `middleware.AuthErrorMiddleware`, installed by `fast_api_app.py`
    under langgraph-server, restores both. It relies on the server's default
    middleware order (no `"middleware_order": "auth_first"` in langgraph.json).
    """
    from langgraph_sdk import Auth

    check_startup()
    auth = Auth()

    def _permissions_of(ctx: Any) -> list[str]:
        perms = getattr(ctx.user, "permissions", None) or getattr(ctx, "permissions", None) or []
        return [str(p) for p in perms]

    def _roles_of(ctx: Any) -> set[str]:
        return {
            p[len(ROLE_PERMISSION_PREFIX) :]
            for p in _permissions_of(ctx)
            if p.startswith(ROLE_PERMISSION_PREFIX)
        }

    def _actor_of(ctx: Any) -> str | None:
        """The delegated caller's actor (its `actor:` permission), None for a direct caller."""
        for p in _permissions_of(ctx):
            if p.startswith(ACTOR_PERMISSION_PREFIX):
                return p[len(ACTOR_PERMISSION_PREFIX) :]
        return None

    def _is_studio(ctx: Any) -> bool:
        # The Studio user exists only under `langgraph dev` (or LangSmith-hosted
        # auth); it is trusted as an admin under APP_ENV=dev only.
        return isinstance(ctx.user, Auth.types.StudioUser) and dev_mode()

    def _reads_across(ctx: Any) -> bool:
        # A delegated caller's roles never read across (whatever AUTH_DELEGATED_ROLES lends).
        return _actor_of(ctx) is None and bool(_roles_of(ctx) & read_across_roles())

    def _owner_filter(ctx: Any) -> dict[str, Any] | None:
        """Read filter: none for a read-across role, else the caller's own threads.

        The server's copy reads its source with this filter; a copy is a
        write, so there the filter is the caller's own threads for every role.
        """
        if _reads_across(ctx) and not _THREAD_COPY.get():
            return None  # no filter: may read across principals
        return _strict_owner_filter(ctx)

    def _strict_owner_filter(ctx: Any) -> dict[str, Any]:
        """Write filter: always the caller's own threads (read-across is read-only).

        A delegated caller's also name its actor: it reaches only the threads
        started under its own subject and actor. A thread without `actor`
        (a direct one, or one created before 0.3) never matches that filter.
        """
        actor = _actor_of(ctx)
        if actor is None:
            return {"principal_id": ctx.user.identity}
        return {"principal_id": ctx.user.identity, "actor": actor}

    def _stamp_actor(ctx: Any, metadata: dict[str, Any]) -> None:
        """The caller's actor in metadata the app stamps: set for a delegated caller,
        removed for a direct one (no caller chooses it)."""
        actor = _actor_of(ctx)
        if actor is None:
            metadata.pop("actor", None)
        else:
            metadata["actor"] = actor

    def _require_admin(ctx: Any) -> bool:
        if _is_studio(ctx) or (_actor_of(ctx) is None and _roles_of(ctx) & admin_roles()):
            return True
        raise Auth.exceptions.HTTPException(
            status_code=403,
            detail=f"{ctx.resource}.{ctx.action} is allowed only to AUTH_ADMIN_ROLES.",
        )

    def _metadata_of(value: Any) -> dict[str, Any]:
        """The request's metadata dict, created when absent; refuses what cannot be stamped."""
        metadata = value.setdefault("metadata", {}) if isinstance(value, dict) else None
        if not isinstance(metadata, dict):
            raise Auth.exceptions.HTTPException(
                status_code=403, detail="Thread ownership metadata could not be set."
            )
        return metadata

    @auth.authenticate
    async def authenticate(request: Any) -> dict[str, Any]:
        _THREAD_COPY.set(_is_thread_copy(request))
        policy = get_policy()
        try:
            principal = finalize_principal(await policy.authenticate(request))
        except HTTPException as exc:
            if exc.status_code == 401:
                _stash_challenge(request, exc.headers)
            raise Auth.exceptions.HTTPException(
                status_code=exc.status_code, detail=str(exc.detail), headers=exc.headers
            ) from exc
        except NotImplementedError as exc:
            raise Auth.exceptions.HTTPException(status_code=503, detail=str(exc)) from exc
        # A policy's own `actor:` permission is never trusted: only the actor it set does.
        permissions = sorted(
            p for p in principal.permissions if not str(p).startswith(ACTOR_PERMISSION_PREFIX)
        ) + [f"{ROLE_PERMISSION_PREFIX}{r}" for r in principal.roles]
        if principal.actor is not None:
            permissions.append(f"{ACTOR_PERMISSION_PREFIX}{principal.actor.id}")
        return {
            "identity": principal.id,
            "display_name": principal.id,
            "is_authenticated": True,
            "permissions": permissions,
        }

    # -- default deny: whatever has no rule below -----------------------------

    @auth.on
    async def deny_by_default(ctx: Any, value: Any) -> bool:
        if _is_studio(ctx):
            return True
        raise Auth.exceptions.HTTPException(
            status_code=403, detail=f"{ctx.resource}.{ctx.action} is not allowed."
        )

    # -- threads (and runs on them): owner-scoped ------------------------------

    @auth.on.threads.create
    async def on_threads_create(ctx: Any, value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict) and "metadata" not in value and not _THREAD_COPY.get():
            # The server creates a thread without metadata only for a copy,
            # which keeps its source's metadata (owner included) and ignores
            # what is stamped here. Not recognised as a copy, its source may
            # not have been limited to the caller's own threads: fail closed
            # for the roles whose reads are not.
            if _reads_across(ctx):
                raise Auth.exceptions.HTTPException(
                    status_code=403,
                    detail="Read-across roles may read other principals' threads, not copy them.",
                )
        metadata = _metadata_of(value)
        metadata["principal_id"] = ctx.user.identity
        metadata["tenant"] = None
        _stamp_actor(ctx, metadata)
        return _strict_owner_filter(ctx)

    @auth.on.threads.read
    async def on_threads_read(ctx: Any, value: Any) -> dict[str, Any] | None:
        thread_id = value.get("thread_id") if isinstance(value, dict) else None
        if _THREAD_COPY.get() and thread_id is not None and not _is_studio(ctx):
            # The server's copy reads its source first. A copy keeps the
            # source's tool calls but not its approvals (they belong to the
            # source, and go when it is deleted): refused where calls were gated.
            from {{cookiecutter.agent_directory}}.app_utils.chat import RUNTIME

            refusal = await RUNTIME.copy_refusal(str(thread_id))
            if refusal:
                raise Auth.exceptions.HTTPException(
                    status_code=403,
                    detail=f"This thread cannot be copied: {refusal}.",
                )
        return _owner_filter(ctx)

    @auth.on.threads.search
    async def on_threads_search(ctx: Any, value: Any) -> dict[str, Any] | None:
        return _owner_filter(ctx)

    @auth.on.threads.update
    async def on_threads_update(ctx: Any, value: Any) -> dict[str, Any] | None:
        metadata = value.get("metadata") if isinstance(value, dict) else None
        if isinstance(metadata, dict):
            # Ownership is not transferable: an owner cannot hand a thread
            # (and its content) to another principal, tenant or actor, and a
            # direct owner cannot strip the actor its agent's thread has.
            metadata["principal_id"] = ctx.user.identity
            metadata.pop("tenant", None)
            _stamp_actor(ctx, metadata)
        return _strict_owner_filter(ctx)

    @auth.on.threads.delete
    async def on_threads_delete(ctx: Any, value: Any) -> dict[str, Any] | None:
        return _strict_owner_filter(ctx)

    @auth.on.threads.create_run
    async def on_threads_create_run(ctx: Any, value: Any) -> dict[str, Any] | None:
        kwargs = value.get("kwargs") if isinstance(value, dict) else None
        if isinstance(kwargs, dict) and kwargs.get("command") and not _is_studio(ctx):
            # A command resumes a paused run: a gated API call waits there for a
            # human decision, which only the app's approval routes may deliver
            # (they check who may decide). The app's own loopback calls do not
            # pass through here.
            raise Auth.exceptions.HTTPException(
                status_code=403,
                detail="Resuming a run is done through the app's approval routes "
                "(POST /threads/{thread_id}/approvals/{approval_id}).",
            )
        thread_id = value.get("thread_id") if isinstance(value, dict) else None
        if thread_id is not None and not _is_studio(ctx):
            # Imported here: chat.py imports this module.
            from {{cookiecutter.agent_directory}}.app_utils.chat import RUNTIME

            if await RUNTIME.pending_approvals(str(thread_id)):
                # A thread waiting for an approval takes no new run (as /chat's
                # 409 approval_pending): it would abandon the pending approval.
                raise Auth.exceptions.HTTPException(
                    status_code=409,
                    detail="approval_pending: a gated API call on this thread waits for a "
                    "decision; decide it first (GET /threads/{thread_id}/approvals).",
                )
            if _replays(kwargs):
                refusal = await RUNTIME.replay_refusal(str(thread_id))
                if refusal:
                    # Continued without input or replayed from a checkpoint, a
                    # paused step runs its tool calls again with no decision.
                    raise Auth.exceptions.HTTPException(
                        status_code=403,
                        detail=f"A run without input, or from a checkpoint, is refused here: "
                        f"{refusal}. Send a new message instead (POST /chat), and decide "
                        "approvals through POST /threads/{thread_id}/approvals/{approval_id}.",
                    )
        metadata = _metadata_of(value)
        if value.get("thread_id") is None or value.get("if_not_exists") == "create":
            # The run may create its thread, whose metadata is then the run's
            # config metadata overlaid with this metadata: stamp both owner
            # keys. The raw id is the ownership stamp the filters compare, so
            # it is needed here (and this run's metadata carries it too).
            metadata["principal_id"] = ctx.user.identity
        else:
            # The thread exists and keeps its own stamp (the filter below
            # checks it). The server copies the run's metadata, thread
            # metadata merged in, into traces and checkpoints: override the
            # raw id there with the hashed one.
            metadata["principal_id"] = Principal(id=str(ctx.user.identity)).hashed_id()
        metadata["tenant"] = None
        _stamp_actor(ctx, metadata)
        return _strict_owner_filter(ctx)

    # -- assistants, crons, store: read for everyone, change for admins --------

    @auth.on.assistants.read
    @auth.on.assistants.search
    @auth.on.crons.read
    @auth.on.crons.search
    async def allow_read(ctx: Any, value: Any) -> bool:
        return True

    @auth.on.assistants.create
    @auth.on.assistants.update
    @auth.on.assistants.delete
    @auth.on.crons.create
    @auth.on.crons.update
    @auth.on.crons.delete
    async def admin_only(ctx: Any, value: Any) -> bool:
        return _require_admin(ctx)

    @auth.on.store(actions=["get", "search", "list_namespaces"])
    async def allow_store_read(ctx: Any, value: Any) -> bool:
        return True

    @auth.on.store(actions=["put", "delete"])
    async def admin_only_store(ctx: Any, value: Any) -> bool:
        return _require_admin(ctx)

    return auth


_sdk_auth: Any = None


def __getattr__(name: str) -> Any:
    # Reading `auth` from this module (langgraph.json `auth.path`) builds the
    # SDK object on first use, so importing the module never requires langgraph_sdk.
    if name == "auth":
        global _sdk_auth
        if _sdk_auth is None:
            _sdk_auth = build_sdk_auth()
        return _sdk_auth
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
