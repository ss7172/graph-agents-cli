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
and, under `langgraph-server`, the native Threads/Runs/Assistants API through
`auth`, a `langgraph_sdk.Auth` object built from the same policy and
referenced by `langgraph.json`. `langgraph_sdk` is imported lazily, so the
fastapi runtime does not need it.

Policies:
  * `SharedBearerPolicy` (`shared-bearer`, default): `Authorization: Bearer <API_KEY>`,
    constant-time compare, one principal `shared`, every action allowed.
  * `JwtPolicy` (`jwt`): per-user principals from a verified OIDC/JWT bearer
    token (`AUTH_JWT_*` settings, see `.env.example`).
  * `CustomPolicy` (`custom`): interface plus a fail-closed stub in
    `policies/custom.py`, implemented by the project (for example to validate
    an existing application's session cookie).

The retired name `product-session` is still read as `custom`, with a warning.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

# Every action a policy may be asked to authorize.
ACTIONS: frozenset[str] = frozenset(
    {
        "chat.send",
        "thread.read",
        "thread.list",
        "thread.delete",
        "run.read",
        "a2a.invoke",
        "card.read",
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


@dataclass
class Principal:
    """Who is calling. `id` is what traces and run records use, hashed.

    `attributes` may hold secrets only under `credentials` (api name ->
    credential string). Anything persisted, logged, traced or passed into
    LangGraph Server run context/metadata must use `public_attributes()`.
    """

    id: str
    roles: list[str] = field(default_factory=list)
    permissions: set[str] = field(default_factory=set)
    attributes: dict[str, Any] = field(default_factory=dict)

    def hashed_id(self) -> str:
        """sha256 of the id, first 16 hex characters (what `metadata` trace capture records)."""
        return hashlib.sha256(self.id.encode("utf-8")).hexdigest()[:16]

    def public_attributes(self) -> dict[str, Any]:
        """`attributes` without `credentials`: safe to persist, log or trace."""
        return {k: v for k, v in self.attributes.items() if k != CREDENTIALS_KEY}


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


JWT_NOT_AVAILABLE = (
    "AUTH_POLICY=jwt: token verification is not available in this build of the "
    "template; every request is refused until it is (fail closed)."
)


class JwtPolicy:
    """Per-user principals from a verified OIDC/JWT bearer token.

    Placeholder: refuses every request (503) until token verification lands,
    so selecting `jwt` can never mean "no auth".
    """

    async def authenticate(self, request: Request) -> Principal:
        raise HTTPException(status_code=503, detail=JWT_NOT_AVAILABLE)

    async def authorize(self, principal: Principal, action: str, resource: str | None) -> None:
        raise HTTPException(status_code=503, detail=JWT_NOT_AVAILABLE)


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
    """For tests that switch `AUTH_POLICY`."""
    _policies.clear()


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
            principal = await policy.authenticate(request)
            await policy.authorize(principal, action, request.path_params.get("thread_id"))
        except (HTTPException, NotImplementedError) as exc:
            raise _as_http_exception(exc) from exc
        request.state.principal = principal
        return principal

    dependency.__name__ = f"require_{action.replace('.', '_')}"
    return dependency


async def authenticate_and_authorize(
    request: Request, action: str, resource: str | None = None
) -> Principal:
    """Same check as `require`, for code paths outside FastAPI's dependency system."""
    policy = get_policy()
    try:
        principal = await policy.authenticate(request)
        await policy.authorize(principal, action, resource)
    except (HTTPException, NotImplementedError) as exc:
        raise _as_http_exception(exc) from exc
    return principal


# ---------------------------------------------------------------------------
# LangGraph Server auth handler (langgraph.json "auth": {"path": ".../auth.py:auth"})
# ---------------------------------------------------------------------------

ROLE_PERMISSION_PREFIX = "role:"


def build_sdk_auth() -> Any:
    """A `langgraph_sdk.Auth` whose handlers delegate to the selected policy.

    `@auth.authenticate` runs the policy's `authenticate`; the `@auth.on`
    handlers set `{principal_id, tenant}` in thread metadata at creation and
    return owner filters on threads and runs. A role listed in
    `AUTH_READ_ACROSS_ROLES` relaxes only the read and search filters; update,
    delete and create_run stay owner-only (read-across roles may *read*
    others' threads, never change them). These handlers cover the native API
    called from outside the app; the custom routes' loopback SDK calls bypass
    the server's auth middleware, so `chat.py` enforces the same rule in-app.
    """
    from langgraph_sdk import Auth

    auth = Auth()

    def _roles_of(user: Any) -> set[str]:
        perms = getattr(user, "permissions", None) or []
        return {
            p[len(ROLE_PERMISSION_PREFIX) :]
            for p in perms
            if str(p).startswith(ROLE_PERMISSION_PREFIX)
        }

    def _read_across() -> set[str]:
        raw = os.environ.get("AUTH_READ_ACROSS_ROLES", "")
        return {r.strip() for r in raw.split(",") if r.strip()}

    def _owner_filter(ctx: Any) -> dict[str, Any] | None:
        """Read filter: none for a read-across role, else the caller's own threads."""
        if _roles_of(ctx.user) & _read_across():
            return None  # no filter: may read across principals
        return {"principal_id": ctx.user.identity}

    def _strict_owner_filter(ctx: Any) -> dict[str, Any]:
        """Write filter: always the caller's own threads (read-across is read-only)."""
        return {"principal_id": ctx.user.identity}

    @auth.authenticate
    async def authenticate(request: Any) -> dict[str, Any]:
        policy = get_policy()
        try:
            principal = await policy.authenticate(request)
        except HTTPException as exc:
            raise Auth.exceptions.HTTPException(
                status_code=exc.status_code, detail=str(exc.detail), headers=exc.headers
            ) from exc
        except NotImplementedError as exc:
            raise Auth.exceptions.HTTPException(status_code=503, detail=str(exc)) from exc
        permissions = sorted(principal.permissions) + [
            f"{ROLE_PERMISSION_PREFIX}{r}" for r in principal.roles
        ]
        return {
            "identity": principal.id,
            "display_name": principal.id,
            "is_authenticated": True,
            "permissions": permissions,
        }

    @auth.on.threads.create
    async def on_threads_create(ctx: Any, value: Any) -> dict[str, Any] | None:
        metadata = value.setdefault("metadata", {})
        metadata["principal_id"] = ctx.user.identity
        metadata.setdefault("tenant", None)
        return {"principal_id": ctx.user.identity}

    @auth.on.threads.read
    async def on_threads_read(ctx: Any, value: Any) -> dict[str, Any] | None:
        return _owner_filter(ctx)

    @auth.on.threads.search
    async def on_threads_search(ctx: Any, value: Any) -> dict[str, Any] | None:
        return _owner_filter(ctx)

    @auth.on.threads.update
    async def on_threads_update(ctx: Any, value: Any) -> dict[str, Any] | None:
        return _strict_owner_filter(ctx)

    @auth.on.threads.delete
    async def on_threads_delete(ctx: Any, value: Any) -> dict[str, Any] | None:
        return _strict_owner_filter(ctx)

    @auth.on.threads.create_run
    async def on_threads_create_run(ctx: Any, value: Any) -> dict[str, Any] | None:
        metadata = value.setdefault("metadata", {})
        metadata["principal_id"] = ctx.user.identity
        return _strict_owner_filter(ctx)

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
