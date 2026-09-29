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

"""Hidden-check helpers: load the project's tools and call one the way the agent runtime does.

Run from the project root with the project's interpreter (``uv run python <check>``).
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.getcwd())
os.environ["MODEL_PROVIDER"] = "fake"


def tools_by_name() -> dict:
    import app.tools as pkg

    return {getattr(t, "name", ""): t for t in pkg.get_tools()}


def function_of(tool):
    return getattr(tool, "coroutine", None) or getattr(tool, "func", None)


def module_of(tool):
    return sys.modules[function_of(tool).__module__]


def call(tool, args: dict, user_message: str = ""):
    """Call the tool's function with ``args`` and, when it takes one, a runtime whose latest
    user message is ``user_message``; returns the result or the exception it raised."""
    from langchain_core.messages import HumanMessage

    fn = function_of(tool)
    kwargs = dict(args)
    runtime = SimpleNamespace(
        state={"messages": [HumanMessage(content=user_message)]},
        context=None,
        tool_call_id="call-1",
        config={},
        store=None,
        stream_writer=None,
    )
    for name, param in inspect.signature(fn).parameters.items():
        if name == "runtime" or "ToolRuntime" in str(param.annotation):
            kwargs[name] = runtime
    try:
        result = fn(**kwargs)
        if inspect.isawaitable(result):
            result = asyncio.run(result)
        return result
    except Exception as exc:
        return exc


def mock_api(handler) -> None:
    """Send every request of the policy client through ``httpx.MockTransport(handler)``."""
    import httpx

    from app.app_utils import api_client

    transport = httpx.MockTransport(handler)
    original = api_client.get_client

    def patched(name, **kw):
        kw["transport"] = transport
        return original(name, **kw)

    api_client.get_client = patched
    for module in list(sys.modules.values()):
        if getattr(module, "__name__", "").startswith("app.tools") and getattr(module, "get_client", None) is original:
            module.get_client = patched


def declared(tool) -> list:
    return list(getattr(module_of(tool), "API_CALLS", None) or [])


def fail(message: str) -> None:
    print(f"FAIL: {message}")
    raise SystemExit(1)
