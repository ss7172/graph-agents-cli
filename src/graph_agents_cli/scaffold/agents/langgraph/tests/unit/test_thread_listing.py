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

"""Thread listing scopes, owners shown hashed, and thread-delete listeners."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import HTTPException

from {{cookiecutter.agent_directory}}.app_utils.auth import Actor, Principal
from {{cookiecutter.agent_directory}}.app_utils.checkpointer import POSTGRES
from {{cookiecutter.agent_directory}}.app_utils.db import Database
from {{cookiecutter.agent_directory}}.app_utils.threads import (
    DELETE_LISTENERS,
    SCOPE_ALL,
    SCOPE_OWN,
    ThreadStore,
    check_scope,
    own_view,
    search_server_threads,
)

OWNER = Principal(id="alice@example.com", roles=["viewer"])
AUDITOR = Principal(id="carol", roles=["auditor", "viewer"], attributes={"credentials": {"x": "s"}})


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> ThreadStore:
    monkeypatch.setenv("AUTH_READ_ACROSS_ROLES", "auditor")
    return ThreadStore(Database("memory"))


async def test_a_read_across_role_lists_its_own_threads_unless_it_asks_for_all(
    store: ThreadStore,
) -> None:
    await store.create("t-alice", OWNER)
    await store.create("t-carol", AUDITOR)
    assert [r.thread_id for r in await store.list_for(AUDITOR, scope=SCOPE_OWN)] == ["t-carol"]
    everything = await store.list_for(AUDITOR, scope=SCOPE_ALL)
    assert {r.thread_id for r in everything} == {"t-alice", "t-carol"}
    rows = {r.thread_id: r.public() for r in everything}
    assert rows["t-alice"]["owner"] == OWNER.hashed_id()
    assert "alice@example.com" not in repr(rows)
    # Someone without the role gets its own threads whatever it asks for.
    assert [r.thread_id for r in await store.list_for(OWNER, scope=SCOPE_ALL)] == ["t-alice"]


def test_scope_all_needs_a_read_across_role(store: ThreadStore) -> None:
    assert check_scope(AUDITOR, SCOPE_ALL) == SCOPE_ALL
    assert check_scope(OWNER, SCOPE_OWN) == SCOPE_OWN
    with pytest.raises(HTTPException) as exc:
        check_scope(OWNER, SCOPE_ALL)
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        check_scope(AUDITOR, "everyone")
    assert exc.value.status_code == 422


def test_own_view_drops_the_read_across_roles_and_the_credentials(store: ThreadStore) -> None:
    view = own_view(AUDITOR)
    assert view.id == "carol" and view.roles == ["viewer"]
    assert "credentials" not in view.attributes


class _Threads:
    def __init__(self, threads: list[dict[str, Any]]) -> None:
        self.threads = threads
        self.searches: list[dict[str, Any]] = []

    async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.searches.append(kwargs)
        return self.threads


class _Client:
    def __init__(self, threads: list[dict[str, Any]]) -> None:
        self.threads = _Threads(threads)


async def test_server_listing_filters_by_owner_and_hashes_it() -> None:
    client = _Client(
        [
            {"thread_id": "1", "metadata": {"principal_id": "alice@example.com"}},
            {"thread_id": "2", "metadata": {}},
            "not a thread",
        ]
    )
    rows = await search_server_threads(client, AUDITOR, scope=SCOPE_ALL, limit=5, offset=0)
    assert client.threads.searches[-1] == {
        "limit": 5,
        "offset": 0,
        "sort_by": "updated_at",
        "sort_order": "desc",
    }
    assert [(r["thread_id"], r["owner"]) for r in rows] == [("1", OWNER.hashed_id()), ("2", None)]
    await search_server_threads(client, OWNER, scope=SCOPE_OWN, limit=5, offset=10)
    assert client.threads.searches[-1]["metadata"] == {"principal_id": "alice@example.com"}


async def test_deleting_a_thread_tells_the_listeners(store: ThreadStore) -> None:
    told: list[str] = []

    async def listener(thread_id: str) -> None:
        told.append(thread_id)

    async def broken(thread_id: str) -> None:
        raise RuntimeError("listener down")

    DELETE_LISTENERS.extend([broken, listener])
    try:
        await store.create("t1", OWNER)
        await store.delete("t1")  # a failing listener never fails the delete
        assert told == ["t1"] and await store.get("t1") is None
    finally:
        DELETE_LISTENERS.remove(broken)
        DELETE_LISTENERS.remove(listener)


# --- the same rules on the Postgres `threads` table (opt-in: TEST_POSTGRES_DSN) ------------

ADMIN_DSN = os.environ.get("TEST_POSTGRES_DSN", "")


@pytest.fixture
async def pg_store(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ThreadStore]:
    if not ADMIN_DSN:
        pytest.skip("TEST_POSTGRES_DSN is not set")
    from urllib.parse import urlsplit

    import psycopg

    monkeypatch.setenv("AUTH_READ_ACROSS_ROLES", "auditor")
    name = f"gac_test_{uuid.uuid4().hex[:12]}"
    async with await psycopg.AsyncConnection.connect(ADMIN_DSN, autocommit=True) as admin:
        await admin.execute(f'CREATE DATABASE "{name}"')
    db = Database(POSTGRES, urlsplit(ADMIN_DSN)._replace(path=f"/{name}").geturl())
    await db.open()
    try:
        yield ThreadStore(db)
    finally:
        await db.close()
        async with await psycopg.AsyncConnection.connect(ADMIN_DSN, autocommit=True) as admin:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


async def test_postgres_listing_scopes_and_delete_listeners(pg_store: ThreadStore) -> None:
    await pg_store.create("t-alice", OWNER)
    await pg_store.create("t-carol", AUDITOR)
    own = await pg_store.list_for(own_view(AUDITOR))
    assert [r.thread_id for r in own] == ["t-carol"]
    assert [r.thread_id for r in await pg_store.list_for(AUDITOR, scope=SCOPE_OWN)] == ["t-carol"]
    everything = await pg_store.list_for(AUDITOR, scope=SCOPE_ALL)
    assert {r.public()["owner"] for r in everything} == {OWNER.hashed_id(), AUDITOR.hashed_id()}
    told: list[str] = []

    async def listener(thread_id: str) -> None:
        told.append(thread_id)

    DELETE_LISTENERS.append(listener)
    try:
        await pg_store.delete("t-alice")
    finally:
        DELETE_LISTENERS.remove(listener)
    assert told == ["t-alice"] and await pg_store.get("t-alice") is None


# --- threads an agent started for its user (0.3) ------------------------------------------

AGENT = Principal(id="alice@example.com", actor=Actor(id="concierge", chain=("concierge",)))
OTHER_AGENT = Principal(id="alice@example.com", actor=Actor(id="billing", chain=("billing",)))


async def test_listing_follows_the_owner_key(store: ThreadStore) -> None:
    await store.create("t-direct", OWNER)
    await store.create("t-concierge", AGENT)
    await store.create("t-billing", OTHER_AGENT)
    assert {r.thread_id for r in await store.list_for(OWNER)} == {
        "t-direct",
        "t-concierge",
        "t-billing",
    }
    assert [r.thread_id for r in await store.list_for(AGENT)] == ["t-concierge"]
    assert [r.thread_id for r in await store.list_for(own_view(OTHER_AGENT))] == ["t-billing"]
    assert own_view(AGENT).actor == AGENT.actor
    # The rows keep their v0.2 shape: the owner (hashed subject), never the actor.
    row = (await store.list_for(AGENT))[0].public()
    assert set(row) == {"thread_id", "owner", "created_at", "updated_at"}


async def test_server_listing_of_an_agent_filters_by_its_actor() -> None:
    client = _Client([])
    await search_server_threads(client, AGENT, scope=SCOPE_OWN, limit=5, offset=0)
    assert client.threads.searches[-1]["metadata"] == {
        "principal_id": "alice@example.com",
        "actor": "concierge",
    }
    await search_server_threads(client, OWNER, scope=SCOPE_OWN, limit=5, offset=0)
    assert client.threads.searches[-1]["metadata"] == {"principal_id": "alice@example.com"}


async def test_postgres_listing_follows_the_owner_key(pg_store: ThreadStore) -> None:
    await pg_store.create("t-direct", OWNER)
    await pg_store.create("t-concierge", AGENT)
    await pg_store.create("t-billing", OTHER_AGENT)
    assert {r.thread_id for r in await pg_store.list_for(OWNER)} == {
        "t-direct",
        "t-concierge",
        "t-billing",
    }
    assert [r.thread_id for r in await pg_store.list_for(AGENT)] == ["t-concierge"]
    stored = await pg_store.get("t-concierge")
    assert stored is not None and stored.actor == "concierge"
    assert (await pg_store.ensure("t-concierge", OWNER)).actor == "concierge"
    with pytest.raises(HTTPException) as exc:
        await pg_store.ensure("t-concierge", OTHER_AGENT)
    assert exc.value.status_code == 403


async def test_a_02_threads_table_is_upgraded_in_place(pg_store: ThreadStore) -> None:
    """A v0.2 table (no `actor` column) gains it at startup; its rows read as direct."""
    db = pg_store.db
    await db.execute("DROP TABLE threads")
    await db.execute(
        "CREATE TABLE threads (thread_id TEXT PRIMARY KEY, principal_id TEXT NOT NULL, "
        "tenant TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
        "updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    await db.execute(
        "INSERT INTO threads (thread_id, principal_id) VALUES (%s, %s)",
        ("t-old", OWNER.id),
    )
    await db.setup()
    legacy = await pg_store.get("t-old")
    assert legacy is not None and legacy.actor == ""
    assert [r.thread_id for r in await pg_store.list_for(OWNER)] == ["t-old"]
    assert await pg_store.list_for(AGENT) == []
    with pytest.raises(HTTPException):
        await pg_store.ensure("t-old", AGENT)
