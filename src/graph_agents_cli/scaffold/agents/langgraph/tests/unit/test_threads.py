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

"""Thread ownership (D23): owner, read-across role and stranger; tool-args redaction."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from langchain_core.messages import AIMessage

from {{cookiecutter.agent_directory}}.app_utils.auth import Principal
from {{cookiecutter.agent_directory}}.app_utils.chat import serialize_message
from {{cookiecutter.agent_directory}}.app_utils.db import Database
from {{cookiecutter.agent_directory}}.app_utils.threads import (
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
        tool_calls=[{"id": "c1", "name": "get_weather", "args": {"query": "SF"}}],
    )
    with_args = serialize_message(message)
    assert with_args["tool_calls"] == [{"id": "c1", "name": "get_weather", "args": {"query": "SF"}}]
    without = serialize_message(message, include_tool_args=False)
    assert without["tool_calls"] == [{"id": "c1", "name": "get_weather"}]
    assert "args" not in without["tool_calls"][0]
