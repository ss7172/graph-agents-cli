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

import ast
import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException
from langchain_core.messages import AIMessage

from {{cookiecutter.agent_directory}}.app_utils.auth import Actor, Principal
from {{cookiecutter.agent_directory}}.app_utils.chat import serialize_message
from {{cookiecutter.agent_directory}}.app_utils.db import Database
from {{cookiecutter.agent_directory}}.app_utils.threads import (
    ThreadBusy,
    ThreadLocks,
    ThreadStore,
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
        tool_calls=[{"id": "c1", "name": "probe", "args": {"query": "SF"}}],
    )
    with_args = serialize_message(message)
    assert with_args["tool_calls"] == [{"id": "c1", "name": "probe", "args": {"query": "SF"}}]
    without = serialize_message(message, include_tool_args=False)
    assert without["tool_calls"] == [{"id": "c1", "name": "probe"}]
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
    # The owner is listed hashed, never as the raw principal id.
    assert record.public().keys() == {"thread_id", "owner", "created_at", "updated_at"}
    assert record.public()["owner"] == OWNER.hashed_id() != OWNER.id


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


async def test_the_fence_admits_only_held_threads() -> None:
    """In-process leases never expire; writes to a thread nobody here holds are refused."""
    from {{cookiecutter.agent_directory}}.app_utils.threads import LeaseLost

    locks = ThreadLocks()
    lease = await locks.acquire("t1")
    locks.fence("t1")
    with pytest.raises(LeaseLost):
        locks.fence("t2")
    await lease.release()
    with pytest.raises(LeaseLost):
        locks.fence("t1")
    with pytest.raises(LeaseLost):
        lease.check()


# --- threads an agent started for its user (0.3) ------------------------------------------

ALICE = Principal(id="a", roles=["viewer"])
CONCIERGE = Principal(id="a", actor=Actor(id="concierge", chain=("concierge",)))
BILLING = Principal(id="a", actor=Actor(id="billing", chain=("billing",)))
BOBS_CONCIERGE = Principal(id="b", actor=Actor(id="concierge", chain=("concierge",)))


async def test_the_subject_owns_the_threads_its_agents_start(store: ThreadStore) -> None:
    record = await store.create("t-agent", CONCIERGE)
    assert record.actor == "concierge" and record.principal_id == "a"
    # The person reads, continues and deletes it.
    assert is_owner(ALICE, record) and can_access(ALICE, record)
    assert (await store.ensure("t-agent", ALICE)).actor == "concierge"
    assert [r.thread_id for r in await store.list_for(ALICE)] == ["t-agent"]


async def test_an_agent_reaches_only_its_own_threads_for_the_user(store: ThreadStore) -> None:
    concierge_thread = await store.create("t-concierge", CONCIERGE)
    direct_thread = await store.create("t-direct", ALICE)
    assert is_owner(CONCIERGE, concierge_thread)
    for stranger in (BILLING, BOBS_CONCIERGE):
        assert not can_access(stranger, concierge_thread)
        for check in (assert_access, assert_owner):
            with pytest.raises(HTTPException) as exc:
                check(stranger, concierge_thread)
            assert exc.value.status_code == 403
            assert exc.value.detail == "This thread belongs to another principal."
        with pytest.raises(HTTPException):
            await store.ensure("t-concierge", stranger)
    # A direct thread (one a person started, or one from before 0.3) is no agent's.
    assert not is_owner(CONCIERGE, direct_thread)
    with pytest.raises(HTTPException):
        await store.ensure("t-direct", CONCIERGE)
    assert [r.thread_id for r in await store.list_for(CONCIERGE)] == ["t-concierge"]
    assert await store.list_for(BILLING) == []


async def test_a_delegated_principals_roles_never_read_across(store: ThreadStore) -> None:
    record = await store.create("t1", OWNER)
    agent = Principal(id="c", roles=["auditor"], actor=Actor(id="concierge"))
    assert not can_access(agent, record)
    assert await store.list_for(agent) == []


def test_a_record_without_an_actor_reads_as_direct() -> None:
    from {{cookiecutter.agent_directory}}.app_utils.threads import _record_from_row

    legacy = _record_from_row({"thread_id": "t", "principal_id": "a", "tenant": None})
    assert legacy.actor == "" and is_owner(ALICE, legacy) and not is_owner(CONCIERGE, legacy)


# Every place the template builds a `Principal`: (file, function, whether it passes `actor`).
# A site that rebuilds a principal without its actor turns an agent into the person it acts
# for, so a new site fails this test until it is reviewed and added here.
REVIEWED_PRINCIPAL_SITES = sorted(
    [
        # A2A: the forwarded words added to the caller (its actor kept); a task owner key's
        # subject hashed to compare it with an approval's requester (the actor compared apart).
        ("app_utils/a2a.py", "with_origin", True),
        ("app_utils/a2a.py", "owner_is_requester", False),
        ("app_utils/approvals.py", "resume_principal", True),  # the requester's @actor
        ("app_utils/auth.py", "authenticate", False),  # shared-bearer: one direct principal
        ("app_utils/auth.py", "finalize_principal", False),  # direct: only @actor removed
        ("app_utils/auth.py", "finalize_principal", True),
        ("app_utils/auth.py", "on_threads_create_run", False),  # hashes the subject only
        ("app_utils/auth.py", "principal_from_claims", True),  # jwt: act / azp
        ("app_utils/threads.py", "owner_hash", False),  # hashes the subject only
        ("app_utils/threads.py", "own_view", True),
    ]
)


def _principal_sites(root: Path) -> list[tuple[str, str, bool]]:
    sites: set[tuple[str, str, bool]] = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Each call's innermost function: `ast.walk` reaches an outer function first.
        innermost: dict[int, tuple[str, ast.Call]] = {}
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "Principal"
                ):
                    innermost[id(node)] = (function.name, node)
        for name, node in innermost.values():
            has_actor = any(k.arg == "actor" for k in node.keywords)
            sites.add((path.relative_to(root).as_posix(), name, has_actor))
    return sorted(sites)


def test_no_principal_reconstruction_drops_actor() -> None:
    from {{cookiecutter.agent_directory}} import agent

    root = Path(agent.__file__).parent
    assert _principal_sites(root) == sorted(set(REVIEWED_PRINCIPAL_SITES))
