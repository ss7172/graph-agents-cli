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

"""Checkpointer selection for the fastapi runtime (memory or postgres).

`CHECKPOINTER=memory` gives an `InMemorySaver` (no database, state lost on
restart); `CHECKPOINTER=postgres` gives an `AsyncPostgresSaver` on
`POSTGRES_DSN` with its schema set up. Used ONLY by `fast_api_app.py`: under
`langgraph-server` the server owns persistence through `DATABASE_URI`/`REDIS_URI`
and `agent.py` always exports an unbound graph.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

MEMORY = "memory"
POSTGRES = "postgres"


def checkpointer_kind() -> str:
    """`memory` (default) or `postgres`, from `CHECKPOINTER`."""
    return (os.environ.get("CHECKPOINTER") or MEMORY).strip().lower()


def postgres_dsn() -> str:
    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise RuntimeError(
            "CHECKPOINTER=postgres requires POSTGRES_DSN (from the Secret or the chart)."
        )
    return dsn


@asynccontextmanager
async def get_checkpointer() -> AsyncIterator[Any]:
    """Yield a ready checkpointer for the configured kind.

    Postgres uses a connection pool and runs `setup()` (idempotent) so the
    checkpoint tables exist before the first run.
    """
    kind = checkpointer_kind()
    if kind == MEMORY:
        from langgraph.checkpoint.memory import InMemorySaver

        yield InMemorySaver()
        return
    if kind == POSTGRES:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        pool: AsyncConnectionPool = AsyncConnectionPool(
            conninfo=postgres_dsn(),
            open=False,
            kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0},
        )
        await pool.open()
        try:
            saver = AsyncPostgresSaver(pool)
            await saver.setup()
            yield saver
        finally:
            await pool.close()
        return
    raise RuntimeError(f"Unknown CHECKPOINTER {kind!r}; expected 'memory' or 'postgres'.")
