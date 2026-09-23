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

"""The LangGraph Server authorization rules (`auth.build_sdk_auth`), without a server.

Handlers are resolved with the server's documented precedence (resource and
action, then resource, then action, then the global handler).
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph_sdk import Auth

from {{cookiecutter.agent_directory}}.app_utils.auth import build_sdk_auth, reset_policy_cache

RESOURCE_ACTIONS = {
    "threads": ("create", "read", "update", "delete", "search", "create_run"),
    "assistants": ("create", "read", "update", "delete", "search"),
    "crons": ("create", "read", "update", "delete", "search"),
    "store": ("put", "get", "search", "list_namespaces", "delete"),
}
READS = {
    ("assistants", "read"),
    ("assistants", "search"),
    ("crons", "read"),
    ("crons", "search"),
    ("store", "get"),
    ("store", "search"),
    ("store", "list_namespaces"),
}
WRITES = {
    (resource, action)
    for resource in ("assistants", "crons", "store")
    for action in RESOURCE_ACTIONS[resource]
    if (resource, action) not in READS
}


@pytest.fixture
def auth(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setenv("AUTH_POLICY", "shared-bearer")
    monkeypatch.setenv("AUTH_ADMIN_ROLES", "admin, platform")
    monkeypatch.setenv("AUTH_READ_ACROSS_ROLES", "support")
    monkeypatch.setenv("APP_ENV", "prod")
    reset_policy_cache()
    yield build_sdk_auth()
    reset_policy_cache()


def _user(identity: str, *roles: str) -> Any:
    return SimpleNamespace(
        identity=identity,
        display_name=identity,
        is_authenticated=True,
        permissions=["chat.send", *(f"role:{r}" for r in roles)],
    )


def _handler(auth: Any, resource: str, action: str) -> Any:
    for key in ((resource, action), (resource, "*"), ("*", action), ("*", "*")):
        if key in auth._handlers:
            return auth._handlers[key][-1]
    return auth._global_handlers[-1] if auth._global_handlers else None


async def _dispatch(auth: Any, user: Any, resource: str, action: str, value: Any = None) -> Any:
    ctx = Auth.types.AuthContext(
        user=user, permissions=list(user.permissions), resource=resource, action=action
    )
    handler = _handler(auth, resource, action)
    assert handler is not None, f"{resource}.{action} would be allowed without a rule"
    return await handler(ctx=ctx, value={} if value is None else value)


def _sdk_resource_actions(auth: Any) -> set[tuple[str, str]]:
    """Every (resource, action) the installed SDK can register a handler for.

    The same walk LangGraph Server does at startup to warn about paths that
    would be allowed without a rule, so a newer SDK action shows up here.
    """
    pairs: set[tuple[str, str]] = set()
    for slot in getattr(type(auth.on), "__slots__", ()) or ():
        namespace = getattr(auth.on, slot, None)
        if slot.startswith("_") or namespace is None:
            continue
        for name in dir(namespace):
            action = getattr(getattr(namespace, name, None), "action", None)
            if not name.startswith("_") and isinstance(action, str):
                pairs.add((slot, action))
    return pairs


def test_every_dispatch_path_has_a_rule_and_the_rest_is_denied(auth: Any) -> None:
    assert auth._global_handlers, "no default-deny handler"
    known = {(r, a) for r, actions in RESOURCE_ACTIONS.items() for a in actions}
    assert known <= _sdk_resource_actions(auth)
    for resource, action in known:
        assert (resource, action) in auth._handlers, f"{resource}.{action}"
    # A pair a newer SDK adds falls to the default-deny handler (checked below).


@pytest.mark.parametrize(("resource", "action"), sorted(READS))
async def test_reads_are_open_to_every_authenticated_principal(
    auth: Any, resource: str, action: str
) -> None:
    assert await _dispatch(auth, _user("bob"), resource, action) is True


@pytest.mark.parametrize(("resource", "action"), sorted(WRITES))
async def test_writes_need_an_admin_role(auth: Any, resource: str, action: str) -> None:
    with pytest.raises(Auth.exceptions.HTTPException) as exc:
        await _dispatch(auth, _user("bob", "support"), resource, action)
    assert exc.value.status_code == 403 and "AUTH_ADMIN_ROLES" in str(exc.value.detail)
    assert await _dispatch(auth, _user("carol", "admin"), resource, action) is True
    assert await _dispatch(auth, _user("dan", "viewer", "platform"), resource, action) is True


async def test_no_admin_roles_means_nobody_may_change_shared_resources(
    auth: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_ADMIN_ROLES", "")
    with pytest.raises(Auth.exceptions.HTTPException):
        await _dispatch(auth, _user("carol", "admin"), "assistants", "update")


async def test_anything_without_a_rule_is_denied_even_for_admins(auth: Any) -> None:
    for resource, action in (("runs", "read"), ("threads", "prune"), ("profiles", "create")):
        with pytest.raises(Auth.exceptions.HTTPException) as exc:
            await _dispatch(auth, _user("carol", "admin"), resource, action)
        assert exc.value.status_code == 403


async def test_studio_user_is_trusted_only_under_dev(
    auth: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    studio = Auth.types.StudioUser("langgraph-studio-user")
    with pytest.raises(Auth.exceptions.HTTPException):
        await _dispatch(auth, studio, "assistants", "update")
    monkeypatch.setenv("APP_ENV", "dev")
    assert await _dispatch(auth, studio, "assistants", "update") is True


async def test_thread_rules_are_owner_scoped(auth: Any) -> None:
    alice, support = _user("alice"), _user("sam", "support")
    value: dict[str, Any] = {"metadata": {"principal_id": "mallory", "topic": "x"}}
    assert await _dispatch(auth, alice, "threads", "create", value) == {"principal_id": "alice"}
    assert value["metadata"] == {"principal_id": "alice", "topic": "x", "tenant": None}
    for action in ("read", "search"):
        assert await _dispatch(auth, alice, "threads", action) == {"principal_id": "alice"}
        assert await _dispatch(auth, support, "threads", action) is None  # read-across
    for action in ("update", "delete", "create_run"):
        # Read-across never extends to changes.
        assert await _dispatch(auth, support, "threads", action) == {"principal_id": "sam"}
    run: dict[str, Any] = {"metadata": {"principal_id": "mallory", "tenant": "acme"}}
    await _dispatch(auth, alice, "threads", "create_run", run)
    assert run["metadata"] == {"principal_id": "alice", "tenant": None}
    # A native caller cannot vouch for a tenant.
    claimed: dict[str, Any] = {"metadata": {"tenant": "acme"}}
    await _dispatch(auth, alice, "threads", "create", claimed)
    assert claimed["metadata"] == {"principal_id": "alice", "tenant": None}
    # Ownership that cannot be stamped is refused, not skipped.
    for action in ("create", "create_run"):
        with pytest.raises(Auth.exceptions.HTTPException) as exc:
            await _dispatch(auth, alice, "threads", action, {"metadata": None})
        assert exc.value.status_code == 403


async def test_an_owner_cannot_hand_a_thread_to_another_principal(auth: Any) -> None:
    value: dict[str, Any] = {
        "thread_id": "t1",
        "metadata": {"principal_id": "bob", "tenant": "t-bob", "topic": "kept"},
    }
    result = await _dispatch(auth, _user("alice"), "threads", "update", value)
    assert result == {"principal_id": "alice"}
    assert value["metadata"] == {"principal_id": "alice", "topic": "kept"}
    # An update without metadata (for example a run cancellation) passes through.
    assert await _dispatch(auth, _user("alice"), "threads", "update", {"thread_id": "t1"}) == {
        "principal_id": "alice"
    }
