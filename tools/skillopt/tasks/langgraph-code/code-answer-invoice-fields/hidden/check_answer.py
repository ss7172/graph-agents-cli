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

"""Hidden check: the project's graph, on the fake model, ends a run with a structured answer.

Runs the compiled ``graph`` from ``app/agent.py`` the way the runtime does (one user message,
the default context) and checks the state's ``structured_response``: an object whose keys and
values fit what the task asked for. Run from the project root (``uv run python <check>``).
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.getcwd())
os.environ["MODEL_PROVIDER"] = "fake"
os.environ.setdefault("API_KEY", "dev")

MESSAGE = "Invoice INV-20417 for 1,250.00 EUR is due on 2026-10-31."
EXPECT = {"invoice_id": None, "amount": None, "currency": ["EUR", "USD", "GBP"], "due_date": None}  # key -> None (any value) or the allowed values


async def main() -> None:
    from langchain_core.messages import HumanMessage

    import app.agent as agent

    result = await agent.graph.ainvoke(
        {"messages": [HumanMessage(content=MESSAGE)]}, context=agent.AgentContext()
    )
    answer = result.get("structured_response")
    if not isinstance(answer, dict):
        sys.exit(f"the run ended with no structured answer (structured_response={answer!r})")
    missing = sorted(set(EXPECT) - set(answer))
    if missing:
        sys.exit(f"the answer lacks {missing}: {answer!r}")
    for key, allowed in EXPECT.items():
        if allowed is not None and answer[key] not in allowed:
            sys.exit(f"answer[{key!r}] = {answer[key]!r}, expected one of {allowed!r}")
    print(f"structured answer: {answer!r}")


asyncio.run(main())
