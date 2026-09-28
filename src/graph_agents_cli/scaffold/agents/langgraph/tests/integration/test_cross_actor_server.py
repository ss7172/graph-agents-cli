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

"""Threads agents start for a user, under a real LangGraph dev server (the native API filters).

The server runs this app and its auth handler as `test_approvals_server.py`
does, with `AUTH_ALLOWED_ACTORS` set. Tokens carry the RFC 8693 `act` claim of
the agents presenting them. The server's own thread filters are containment
filters on the thread metadata: the user's (direct) filter must match the
threads their agents started, an agent's must match only its own, and a thread
without `actor` (the user's own, or one created before 0.3) must match no
agent's. A run the native API starts acts for the authenticated caller, with
its roles and actor, whatever run context the request sends. Skipped when the
LangGraph CLI and its in-memory server are not installed.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from test_approvals_server import (
    AUDIENCE,
    BODY,
    ISSUER,
    KEY,
    POLICY,
    Upstream,
    _free_port,
    _server_env,
    _start_dev,
    _stop,
    _write_config,
    parse_sse,
)

LANGGRAPH = Path(sys.executable).with_name("langgraph")
pytestmark = pytest.mark.skipif(
    not LANGGRAPH.exists() or importlib.util.find_spec("langgraph_api") is None,
    reason="needs the LangGraph CLI with its in-memory server (langgraph-cli[inmem])",
)


def _token(user: str, *actors: str) -> dict[str, str]:
    """`user`'s token, presented by `actors` (current first) when any."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": user,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "nbf": now,
        "exp": now + 600,
        "roles": ["user"],
    }
    act: dict[str, Any] | None = None
    for actor in reversed(actors):
        act = {"sub": actor} if act is None else {"sub": actor, "act": act}
    if act is not None:
        claims["act"] = act
    return {"Authorization": f"Bearer {jwt.encode(claims, KEY, algorithm='RS256')}"}


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    work = tmp_path_factory.mktemp("cross-actor-server")
    upstream = Upstream()
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    body_file = work / "body.json"
    body_file.write_text(json.dumps(BODY), encoding="utf-8")
    policy = work / "api-policy.yaml"
    policy.write_text(POLICY, encoding="utf-8")
    _write_config(work)
    env = {
        **_server_env(policy, body_file, upstream),
        "AUTH_ALLOWED_ACTORS": "concierge,billing",
    }
    port = _free_port()
    proc = None
    try:
        proc = _start_dev(work, env, port, work / "server.log")
        yield f"http://127.0.0.1:{port}"
    finally:
        if proc is not None:
            _stop(proc)
        upstream.shutdown()
        upstream.server_close()


def _get(server: str, thread: str, headers: dict[str, str]) -> httpx.Response:
    return httpx.get(f"{server}/threads/{thread}", headers=headers, timeout=30)


def _listed(server: str, headers: dict[str, str]) -> set[str]:
    r = httpx.post(f"{server}/threads/search", json={"limit": 100}, headers=headers, timeout=30)
    assert r.status_code == 200, r.text
    return {t["thread_id"] for t in r.json()}


def _patch(server: str, thread: str, metadata: dict[str, Any], headers: dict[str, str]) -> Any:
    return httpx.patch(
        f"{server}/threads/{thread}", json={"metadata": metadata}, headers=headers, timeout=30
    )


def test_native_threads_follow_the_owner_key(server: str) -> None:
    user = f"alice-{uuid.uuid4().hex[:8]}"
    alice, concierge = _token(user), _token(user, "concierge")
    billing = _token(user, "billing", "concierge")
    # The concierge's thread is stamped with its actor, whatever it sends.
    created = httpx.post(
        f"{server}/threads",
        json={"metadata": {"actor": "billing", "topic": "x"}},
        headers=concierge,
        timeout=30,
    )
    assert created.status_code == 200, created.text
    thread = created.json()["thread_id"]
    assert created.json()["metadata"]["actor"] == "concierge"
    assert created.json()["metadata"]["principal_id"] == user
    # The person reads and lists it; another agent of the same user does neither.
    assert _get(server, thread, alice).status_code == 200
    assert thread in _listed(server, alice) and thread in _listed(server, concierge)
    assert _get(server, thread, billing).status_code in (403, 404)
    assert thread not in _listed(server, billing)
    # The person's own thread (as any thread from before 0.3: no actor) is no agent's.
    direct = httpx.post(
        f"{server}/threads", json={"metadata": {"actor": "concierge"}}, headers=alice, timeout=30
    )
    assert direct.status_code == 200 and "actor" not in direct.json()["metadata"]
    mine = direct.json()["thread_id"]
    assert _get(server, mine, concierge).status_code in (403, 404)
    assert mine not in _listed(server, concierge) and mine in _listed(server, alice)
    # Nobody moves or strips the actor.
    moved = _patch(server, thread, {"actor": "billing"}, concierge)
    assert moved.status_code == 200 and moved.json()["metadata"]["actor"] == "concierge"
    stripped = _patch(server, thread, {"actor": ""}, alice)
    assert stripped.status_code == 200 and stripped.json()["metadata"]["actor"] == "concierge"
    assert _patch(server, thread, {"topic": "y"}, billing).status_code in (403, 404)


def test_the_apps_routes_stamp_the_actor_under_this_runtime(server: str) -> None:
    user = f"alice-{uuid.uuid4().hex[:8]}"
    alice, concierge = _token(user), _token(user, "concierge")
    r = httpx.post(f"{server}/chat", json={"message": "hello"}, headers=concierge, timeout=60)
    assert r.status_code == 200, r.text
    thread = parse_sse(r.text)[0][1]["thread_id"]
    assert _get(server, thread, alice).json()["metadata"]["actor"] == "concierge"
    for who, allowed in ((alice, True), (concierge, True), (_token(user, "billing"), False)):
        messages = httpx.get(f"{server}/threads/{thread}/messages", headers=who, timeout=30)
        assert (messages.status_code == 200) is allowed, messages.text
    # An agent the server does not list is refused before anything runs.
    other = httpx.post(
        f"{server}/chat", json={"message": "hello"}, headers=_token(user, "stranger"), timeout=30
    )
    assert other.status_code == 403


def _native_whoami(
    server: str,
    headers: dict[str, str],
    *,
    context: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Who the `whoami` tool of a native run (`POST /threads/{id}/runs/wait`) acts for."""
    created = httpx.post(f"{server}/threads", json={}, headers=headers, timeout=30)
    assert created.status_code == 200, created.text
    body: dict[str, Any] = {
        "assistant_id": "agent",
        "input": {"messages": [{"role": "user", "content": "whoami for me"}]},
    }
    if context is not None:
        body["context"] = context
    if config is not None:
        body["config"] = config
    thread = created.json()["thread_id"]
    r = httpx.post(f"{server}/threads/{thread}/runs/wait", json=body, headers=headers, timeout=60)
    assert r.status_code == 200, r.text
    results = [m for m in r.json().get("messages", []) if m.get("type") == "tool"]
    assert results, r.text
    return json.loads(results[-1]["content"])


def test_a_native_run_acts_for_the_authenticated_caller_whatever_it_sends(server: str) -> None:
    """The native run API takes a run context in the request (`context`, or
    `config.configurable`, which the server copies into it); tools read who they act
    for from it. The auth handler puts the caller's own there instead."""
    user = f"alice-{uuid.uuid4().hex[:8]}"
    alice, concierge = _token(user), _token(user, "concierge")
    as_alice = {
        "principal_id": user,
        "roles": ["user"],
        "actor": None,
        "actor_chain": [],
        "direct_only": "allowed",
    }
    # A person names another principal and roles, or poses as an agent: the tools
    # still act for them, with their own roles.
    bob = {"principal_id": "bob", "roles": ["ops"], "attributes": {}}
    posing = {
        "principal_id": user,
        "roles": ["user"],
        "attributes": {"@actor": {"id": "concierge", "chain": ["concierge"], "client": None}},
    }
    assert _native_whoami(server, alice, context=bob) == as_alice
    assert _native_whoami(server, alice, config={"configurable": bob}) == as_alice
    assert _native_whoami(server, alice, context=posing) == as_alice
    assert _native_whoami(server, alice) == as_alice  # nothing sent: still the caller's
    # An agent cannot drop its actor (a tool only a person may trigger refuses it),
    # name another one, or take roles it was not lent.
    for sent in (
        None,
        {"principal_id": user, "roles": [], "attributes": {}},
        {"principal_id": user, "roles": ["ops"], "attributes": {"@actor": {"id": "billing"}}},
    ):
        seen = _native_whoami(server, concierge, context=sent)
        assert seen["principal_id"] == user and seen["roles"] == [], seen
        assert seen["actor"] == "concierge" and seen["actor_chain"] == ["concierge"], seen
        refusal = "only the user directly may ask for this, not agent 'concierge'"
        assert refusal in seen["direct_only"], seen
    # The whole chain of agents reaches the tools, as through /chat.
    seen = _native_whoami(
        server, _token(user, "billing", "concierge"), context={"principal_id": user}
    )
    assert seen["actor"] == "billing" and seen["actor_chain"] == ["billing", "concierge"], seen
