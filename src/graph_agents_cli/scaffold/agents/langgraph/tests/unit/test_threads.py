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

"""Thread ownership (atomic claim, owner, read-across role, stranger), run locks, redaction."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from langchain_core.messages import AIMessage

from {{cookiecutter.agent_directory}}.app_utils.auth import Principal
from {{cookiecutter.agent_directory}}.app_utils.chat import serialize_message
from {{cookiecutter.agent_directory}}.app_utils.db import Database
from {{cookiecutter.agent_directory}}.app_utils.threads import (
    ThreadBusy,
    ThreadLocks,
    ThreadStore,
    advisory_key,
    assert_access,
    assert_owner,
    can_access,
    is_owner,
)

OWNER = Principal(id="a", roles=["viewer"])
STRANGER = Principal(id="b", roles=["viewer"])
AUDITOR = Principal(id="c", roles=["auditor"])


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> ThreadStore:
    monkeypatch.setenv("AUTH_READ_ACROSS_ROLES", "auditor, ops")
    return ThreadStore(Database("memory"))


async def test_owner_reads_and_continues_its_thread(store: ThreadStore) -> None:
    record = await store.create("t1", OWNER)
    assert is_owner(OWNER, record) and can_access(OWNER, record)
    assert (await store.ensure("t1", OWNER)).principal_id == "a"
    assert [r.thread_id for r in await store.list_for(OWNER)] == ["t1"]


async def test_stranger_is_refused_everywhere(store: ThreadStore) -> None:
    record = await store.create("t1", OWNER)
    assert not can_access(STRANGER, record)
    for check in (assert_access, assert_owner):
        with pytest.raises(HTTPException) as exc:
            check(STRANGER, record)
        assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        await store.ensure("t1", STRANGER)
    assert exc.value.status_code == 403
    assert await store.list_for(STRANGER) == []
    # Creating a thread of its own is unchanged.
    assert (await store.ensure("t2", STRANGER)).principal_id == "b"


async def test_read_across_role_reads_but_does_not_write(store: ThreadStore) -> None:
    record = await store.create("t1", OWNER)
    assert can_access(AUDITOR, record) and not is_owner(AUDITOR, record)
    assert_access(AUDITOR, record)  # reads are allowed
    assert [r.thread_id for r in await store.list_for(AUDITOR)] == ["t1"]
    with pytest.raises(HTTPException) as exc:
        assert_owner(AUDITOR, record)
    assert exc.value.status_code == 403
    # chat.send (ensure) is a write: a read-across role may not continue another's thread.
    with pytest.raises(HTTPException) as exc:
        await store.ensure("t1", AUDITOR)
    assert exc.value.status_code == 403


async def test_read_across_roles_default_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTH_READ_ACROSS_ROLES", raising=False)
    store = ThreadStore(Database("memory"))
    record = await store.create("t1", OWNER)
    assert not can_access(AUDITOR, record)


def test_serialize_message_omits_tool_args_when_asked() -> None:
    message = AIMessage(
        content="",
        tool_calls=[{"id": "c1", "name": "get_weather", "args": {"query": "SF"}}],
    )
    with_args = serialize_message(message)
    assert with_args["tool_calls"] == [{"id": "c1", "name": "get_weather", "args": {"query": "SF"}}]
    without = serialize_message(message, include_tool_args=False)
    assert without["tool_calls"] == [{"id": "c1", "name": "get_weather"}]
    assert "args" not in without["tool_calls"][0]


async def test_claim_is_atomic_under_concurrency(store: ThreadStore) -> None:
    """Two principals racing for one new id: exactly one owns it, the other is refused."""
    results = await asyncio.gather(store.claim("race", OWNER), store.claim("race", STRANGER))
    assert sorted(created for _, created in results) == [False, True]
    owner = next(record for record, created in results if created).principal_id
    loser = STRANGER if owner == OWNER.id else OWNER
    with pytest.raises(HTTPException) as exc:
        await store.ensure("race", loser)
    assert exc.value.status_code == 403


async def test_continuing_a_thread_moves_its_idle_clock(store: ThreadStore) -> None:
    record = await store.create("t1", OWNER)
    store._memory["t1"].updated_at = "2000-01-01T00:00:00+00:00"
    assert await store.idle_before("2001-01-01T00:00:00+00:00") == ["t1"]
    await store.ensure("t1", OWNER)
    assert await store.idle_before("2001-01-01T00:00:00+00:00") == []
    assert record.public().keys() == {"thread_id", "created_at", "updated_at"}


async def test_list_is_most_recent_first_and_paged(store: ThreadStore) -> None:
    for i in range(3):
        await store.create(f"t{i}", OWNER)
        store._memory[f"t{i}"].updated_at = f"2026-01-0{i + 1}T00:00:00+00:00"
    assert [r.thread_id for r in await store.list_for(OWNER)] == ["t2", "t1", "t0"]
    assert [r.thread_id for r in await store.list_for(OWNER, limit=1, offset=1)] == ["t1"]
    assert await store.list_for(STRANGER) == []


async def test_one_run_per_thread() -> None:
    locks = ThreadLocks()
    lease = await locks.acquire("t1")
    with pytest.raises(ThreadBusy):
        await locks.acquire("t1")
    other = await locks.acquire("t2")  # other threads are independent
    await lease.release()
    await lease.release()  # idempotent
    again = await locks.acquire("t1")
    assert locks.held == {"t1", "t2"}
    await again.release()
    await other.release()
    assert locks.held == frozenset()


def test_advisory_keys_are_stable_signed_64_bit() -> None:
    key = advisory_key("11111111-1111-1111-1111-111111111111")
    assert key == advisory_key("11111111-1111-1111-1111-111111111111")
    assert -(2**63) <= key < 2**63 and key != advisory_key("other")
