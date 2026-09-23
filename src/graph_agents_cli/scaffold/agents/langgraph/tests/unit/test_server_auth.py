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

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from langgraph_sdk import Auth
from starlette.requests import Request

from {{cookiecutter.agent_directory}}.app_utils.auth import (
    Principal,
    build_sdk_auth,
    reset_policy_cache,
)
from {{cookiecutter.agent_directory}}.app_utils.middleware import AuthErrorMiddleware

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


SOURCE = "11111111-1111-1111-1111-111111111111"
COPY = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def auth(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setenv("AUTH_POLICY", "shared-bearer")
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.delenv("PRINCIPAL_HASH_SALT", raising=False)
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


def _http(method: str, path: str, authorization: str | None = "Bearer k") -> Request:
    headers = [(b"authorization", authorization.encode())] if authorization else []
    return Request(
        {"type": "http", "method": method, "path": path, "headers": headers, "query_string": b""}
    )


async def _authenticate(auth: Any, method: str, path: str) -> None:
    """What the server does first for every native API request (per-request state included)."""
    await auth._authenticate_handler(request=_http(method, path))


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


async def test_a_copy_is_a_write_even_for_read_across_roles(auth: Any) -> None:
    """The server's copy reads its source with the read filter, then authorizes a create
    without metadata (the copy keeps the source's metadata, owner included)."""
    support = _user("sam", "support")

    async def copy_as(user: Any) -> Any:
        await _authenticate(auth, "POST", f"/threads/{SOURCE}/copy")
        source_filter = await _dispatch(auth, user, "threads", "read", {"thread_id": SOURCE})
        await _dispatch(auth, user, "threads", "create", {"thread_id": COPY})
        return source_filter

    # The source is limited to the caller's own threads: alice's is "not found" to sam.
    assert await copy_as(support) == {"principal_id": "sam"}
    assert await copy_as(_user("alice")) == {"principal_id": "alice"}
    # Everywhere else sam still reads across.
    await _authenticate(auth, "GET", f"/threads/{SOURCE}")
    assert await _dispatch(auth, support, "threads", "read", {"thread_id": SOURCE}) is None
    await _authenticate(auth, "POST", "/threads/search")
    assert await _dispatch(auth, support, "threads", "search", {}) is None
    # A metadata-less create the handler cannot tell apart from a copy fails
    # closed for read-across roles (the source filter may have been lifted).
    await _authenticate(auth, "POST", f"/threads/{SOURCE}/fork")
    with pytest.raises(Auth.exceptions.HTTPException) as exc:
        await _dispatch(auth, support, "threads", "create", {"thread_id": COPY})
    assert exc.value.status_code == 403
    assert await _dispatch(auth, _user("bob"), "threads", "create", {"thread_id": COPY}) == {
        "principal_id": "bob"
    }


async def test_the_copy_flag_is_per_request(auth: Any) -> None:
    """Concurrent requests (separate tasks, like the server's) never see each other's flag."""
    support = _user("sam", "support")
    gate = asyncio.Event()

    async def copying() -> Any:
        await _authenticate(auth, "POST", f"/threads/{SOURCE}/copy")
        await gate.wait()
        return await _dispatch(auth, support, "threads", "read", {"thread_id": SOURCE})

    async def reading() -> Any:
        await _authenticate(auth, "GET", f"/threads/{SOURCE}")
        gate.set()
        return await _dispatch(auth, support, "threads", "read", {"thread_id": SOURCE})

    assert await asyncio.gather(copying(), reading()) == [{"principal_id": "sam"}, None]


async def test_native_runs_on_existing_threads_carry_the_hashed_owner_id(
    auth: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRINCIPAL_HASH_SALT", "pepper")
    alice = _user("alice@example.com")
    hashed = Principal(id="alice@example.com").hashed_id()
    run: dict[str, Any] = {"thread_id": SOURCE, "if_not_exists": "reject", "metadata": {}}
    assert await _dispatch(auth, alice, "threads", "create_run", run) == {
        "principal_id": "alice@example.com"  # the filter still checks the thread's raw stamp
    }
    assert run["metadata"] == {"principal_id": hashed, "tenant": None}
    # A run that may create its thread stamps the raw id: it becomes the thread's owner.
    for creating in (
        {"thread_id": SOURCE, "if_not_exists": "create", "metadata": {}},
        {"thread_id": None, "metadata": {"principal_id": "mallory"}},
    ):
        await _dispatch(auth, alice, "threads", "create_run", creating)
        assert creating["metadata"]["principal_id"] == "alice@example.com"


async def test_the_native_api_gets_the_policys_challenge_and_status(
    auth: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LangGraph Server keeps only the detail of a 401 and turns a 503 into a 500.

    The fake below behaves like its auth middleware: it calls the authenticate
    handler, answers 401/403 bare and lets any other status escape.
    """

    async def server(scope: Any, receive: Any, send: Any) -> None:
        try:
            await auth._authenticate_handler(request=Request(scope, receive))
        except Auth.exceptions.HTTPException as exc:
            if exc.status_code not in (401, 403):
                raise
            await send({"type": "http.response.start", "status": exc.status_code, "headers": []})
            await send({"type": "http.response.body", "body": b'{"detail": "bare"}'})
            return
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=AuthErrorMiddleware(server)), base_url="http://server"
    )
    async with client:
        r = await client.get("/assistants/search", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
        r = await client.get("/assistants/search", headers={"Authorization": "Bearer k"})
        assert r.status_code == 200 and "www-authenticate" not in r.headers
        monkeypatch.delenv("API_KEY")  # a misconfigured policy answers 503, not 500
        r = await client.get("/assistants/search", headers={"Authorization": "Bearer k"})
        assert r.status_code == 503 and "API_KEY is not configured" in r.json()["detail"]


async def test_the_auth_error_middleware_leaves_other_errors_alone() -> None:
    async def failing(scope: Any, receive: Any, send: Any) -> None:
        raise RuntimeError("not an auth error")

    async def late(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise Auth.exceptions.HTTPException(status_code=503)

    for app in (failing, late):
        transport = httpx.ASGITransport(app=AuthErrorMiddleware(app))
        async with httpx.AsyncClient(transport=transport, base_url="http://server") as client:
            with pytest.raises((RuntimeError, Auth.exceptions.HTTPException)):
                await client.get("/threads")
