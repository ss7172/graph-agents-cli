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

"""Unit tests for the auth policies: shared-bearer, the custom stub and policy selection.

The jwt policy has its own file (test_jwt_policy.py). No network, no model key.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from {{cookiecutter.agent_directory}}.app_utils import auth as auth_module
from {{cookiecutter.agent_directory}}.app_utils.auth import (
    ACTIONS,
    Actor,
    JwtPolicy,
    Principal,
    SharedBearerPolicy,
    actor_from_claims,
    admin_roles,
    authenticate_and_authorize,
    check_startup,
    delegation_settings,
    dev_mode,
    finalize_principal,
    get_policy,
    keep_subject_token,
    policy_name,
    read_across_roles,
    require,
    reset_policy_cache,
)
from {{cookiecutter.agent_directory}}.app_utils.limits import SettingsError
from {{cookiecutter.agent_directory}}.policies import build_policy
from {{cookiecutter.agent_directory}}.policies.custom import NOT_IMPLEMENTED, CustomPolicy


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


# --- CustomPolicy stub and an unconfigured jwt policy: fail closed ---------------


@pytest.fixture
def custom_policy(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AUTH_POLICY", "custom")
    reset_policy_cache()
    yield
    reset_policy_cache()


async def test_custom_stub_fails_closed_on_every_action(custom_policy) -> None:
    assert isinstance(build_policy("custom"), CustomPolicy)
    assert isinstance(get_policy(), CustomPolicy)
    with pytest.raises(HTTPException) as exc:
        await require("chat.send")(_request({"Authorization": "Bearer test-key"}))
    assert exc.value.status_code == 503 and exc.value.detail == NOT_IMPLEMENTED
    for action in ACTIONS:
        with pytest.raises(HTTPException) as exc:
            await authenticate_and_authorize(_request(), action)
        assert exc.value.status_code == 503
    # A stub that only failed authenticate would still be caught here.
    with pytest.raises(HTTPException) as exc:
        await CustomPolicy().authorize(Principal(id="u1"), "chat.send", None)
    assert exc.value.status_code == 503


async def test_unconfigured_jwt_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_POLICY", "jwt")
    monkeypatch.setenv("APP_ENV", "dev")
    for name in ("AUTH_JWT_JWKS_URL", "AUTH_JWT_PUBLIC_KEY", "AUTH_JWT_ALGORITHMS"):
        monkeypatch.delenv(name, raising=False)
    reset_policy_cache()
    try:
        assert isinstance(get_policy(), JwtPolicy)
        for action in ACTIONS:
            with pytest.raises(HTTPException) as exc:
                await authenticate_and_authorize(
                    _request({"Authorization": "Bearer x.y.z"}), action
                )
            assert exc.value.status_code == 503
            assert "AUTH_POLICY=jwt" in exc.value.detail
    finally:
        reset_policy_cache()


# --- startup check and shared settings ------------------------------------------------


def test_check_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    for app_env in ("dev", "prod"):
        monkeypatch.setenv("APP_ENV", app_env)
        # shared-bearer and the custom stub have nothing to check at startup.
        for name in ("shared-bearer", "custom", "product-session"):
            monkeypatch.setenv("AUTH_POLICY", name)
            reset_policy_cache()
            check_startup()
        # A typo in AUTH_POLICY never starts, in any environment.
        monkeypatch.setenv("AUTH_POLICY", "jtw")
        reset_policy_cache()
        with pytest.raises(RuntimeError, match="Unknown AUTH_POLICY"):
            check_startup()
    reset_policy_cache()


def test_dev_mode_needs_an_explicit_app_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only exactly `dev`: dev relaxes checks, so a near miss is a deployed environment.
    for value, expected in (
        ("dev", True),
        (" DEV ", False),
        ("Dev", False),
        ("dev ", False),
        ("development", False),
        ("prod", False),
        ("", False),
    ):
        monkeypatch.setenv("APP_ENV", value)
        assert dev_mode() is expected, value
    monkeypatch.delenv("APP_ENV")
    assert dev_mode() is False


def test_role_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTH_ADMIN_ROLES", raising=False)
    assert admin_roles() == set()  # empty = nobody
    monkeypatch.setenv("AUTH_ADMIN_ROLES", " admin, ,platform ")
    monkeypatch.setenv("AUTH_READ_ACROSS_ROLES", "support")
    assert admin_roles() == {"admin", "platform"}
    assert read_across_roles() == {"support"}


def test_retired_policy_name_reads_as_custom(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("AUTH_POLICY", "product-session")
    monkeypatch.setattr(auth_module, "_warned_aliases", set())
    reset_policy_cache()
    try:
        with caplog.at_level("WARNING"):
            assert policy_name() == "custom"
            assert policy_name() == "custom"
        assert isinstance(get_policy(), CustomPolicy)
        warnings = [r for r in caplog.records if "deprecated" in r.getMessage()]
        assert len(warnings) == 1
    finally:
        reset_policy_cache()


def test_public_attributes_never_carry_credentials() -> None:
    principal = Principal(
        id="u1", attributes={"tenant": "t1", "credentials": {"billing": "Bearer secret"}}
    )
    assert principal.public_attributes() == {"tenant": "t1"}
    assert "credentials" in principal.attributes  # the original is untouched


def test_hashed_id_is_salted_with_principal_hash_salt(monkeypatch: pytest.MonkeyPatch) -> None:
    import hashlib
    import hmac

    principal = Principal(id="alice@example.com")
    monkeypatch.delenv("PRINCIPAL_HASH_SALT", raising=False)
    unsalted = principal.hashed_id()
    assert unsalted == hashlib.sha256(b"alice@example.com").hexdigest()[:16]
    monkeypatch.setenv("PRINCIPAL_HASH_SALT", "  ")  # blank = not set
    assert principal.hashed_id() == unsalted
    monkeypatch.setenv("PRINCIPAL_HASH_SALT", "pepper-1")
    salted = principal.hashed_id()
    expected = hmac.new(b"pepper-1", b"alice@example.com", hashlib.sha256).hexdigest()[:16]
    assert salted == expected and salted != unsalted and len(salted) == 16
    assert Principal(id="bob").hashed_id() != salted
    monkeypatch.setenv("PRINCIPAL_HASH_SALT", "pepper-2")
    assert principal.hashed_id() not in (salted, unsalted)


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


# --- finalize_principal: one rule set for every policy (0.3) --------------------------------


class _FixedPolicy:
    """A custom policy that returns one principal, as a project's own policy would."""

    def __init__(self, principal: Principal) -> None:
        self.principal = principal

    async def authenticate(self, request: Request) -> Principal:
        return self.principal

    async def authorize(self, principal: Principal, action: str, resource: str | None) -> None:
        return None


async def test_shared_bearer_principals_pass_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_POLICY", "shared-bearer")
    monkeypatch.setenv("API_KEY", "k")
    reset_policy_cache()
    try:
        principal = await require("chat.send")(_request({"Authorization": "Bearer k"}))
    finally:
        reset_policy_cache()
    assert (principal.id, principal.roles, principal.actor) == ("shared", ["shared"], None)
    assert principal.owner_key() == "shared" and "@actor" not in principal.attributes


@pytest.mark.parametrize("bad_id", ["", "a\x00b", "tab\there", "x" * 257])
async def test_a_custom_policys_invalid_ids_are_refused_as_its_bug(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, bad_id: str
) -> None:
    monkeypatch.setenv("AUTH_POLICY", "custom")
    for principal in (
        Principal(id=bad_id),
        Principal(id="alice", actor=Actor(id=bad_id)),
        Principal(id="alice", actor=Actor(id="concierge", chain=("concierge", bad_id))),
    ):
        monkeypatch.setattr(auth_module, "get_policy", lambda p=principal: _FixedPolicy(p))
        with pytest.raises(HTTPException) as exc:
            await authenticate_and_authorize(_request(), "a2a.invoke")
        assert exc.value.status_code == 500
    assert "a bug in the policy" in caplog.text


async def test_a_custom_policy_marks_delegation_with_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_POLICY", "custom")
    delegated = Principal(
        id="alice", roles=["support"], actor=Actor(id="concierge"), permissions=set(ACTIONS)
    )
    monkeypatch.setattr(auth_module, "get_policy", lambda: _FixedPolicy(delegated))
    with pytest.raises(HTTPException) as exc:
        await require("chat.send")(_request())
    assert exc.value.status_code == 403
    monkeypatch.setenv("AUTH_ALLOWED_ACTORS", "concierge")
    principal = await require("chat.send")(_request())
    assert principal.actor == Actor(id="concierge", chain=("concierge",))
    assert principal.roles == [] and principal.owner_key() == "alice\x1fconcierge"
    # A chain that does not start with the actor is a bug in the policy.
    broken = Principal(id="alice", actor=Actor(id="concierge", chain=("billing",)))
    monkeypatch.setattr(auth_module, "get_policy", lambda: _FixedPolicy(broken))
    with pytest.raises(HTTPException) as exc:
        await require("chat.send")(_request())
    assert exc.value.status_code == 500


def test_the_jwt_helpers_are_exported_for_custom_policies() -> None:
    actor = actor_from_claims({"sub": "alice", "act": {"sub": "concierge"}})
    assert actor == Actor(id="concierge", chain=("concierge",), client=None)
    principal = keep_subject_token(Principal(id="alice"), "tok", ["a", "b"])
    assert principal.attributes["credentials"] == {
        "@subject_token": "tok",
        "@subject_aud": ("a", "b"),
    }
    assert principal.public_attributes() == {}


def test_delegation_settings_are_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    defaults = delegation_settings({})
    assert (defaults.max_depth, defaults.allowed_actors, defaults.delegated_roles) == (
        3,
        frozenset(),
        frozenset(),
    )
    assert not defaults.allows("concierge")
    assert delegation_settings({"AUTH_ALLOWED_ACTORS": "*"}).allows("anyone")
    for env in (
        {"AUTH_MAX_DELEGATION_DEPTH": "0"},
        {"AUTH_MAX_DELEGATION_DEPTH": "9"},
        {"AUTH_MAX_DELEGATION_DEPTH": "three"},
        {"AUTH_ALLOWED_ACTORS": "concierge, bad\x01id"},
    ):
        with pytest.raises(SettingsError):
            delegation_settings(env)
    # A bad value reached at request time (the startup check refuses it) fails closed.
    monkeypatch.setenv("AUTH_MAX_DELEGATION_DEPTH", "99")
    with pytest.raises(HTTPException) as exc:
        finalize_principal(Principal(id="alice"))
    assert exc.value.status_code == 503
