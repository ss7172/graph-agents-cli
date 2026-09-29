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

"""Hidden check: app/agent.py follows the template contract again."""

import ast
import os
import sys
from pathlib import Path

sys.path.insert(0, os.getcwd())
os.environ["MODEL_PROVIDER"] = "fake"


def fail(message):
    print(f"FAIL: {message}")
    raise SystemExit(1)


source = Path("app/agent.py").read_text()
tree = ast.parse(source)


def from_get_model(node):
    """`get_model()`, or a name the module assigns `get_model()` once (`model = get_model()`)."""
    if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "get_model":
        return True
    if isinstance(node, ast.Name):
        values = [
            n.value
            for n in tree.body
            if isinstance(n, (ast.Assign, ast.AnnAssign))
            and any(getattr(t, "id", "") == node.id for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))
        ]
        return len(values) == 1 and from_get_model(values[0])
    return False


calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "create_agent"]
if len(calls) != 1:
    fail("expected one create_agent(...) call")
kwargs = {k.arg: k.value for k in calls[0].keywords}
if "checkpointer" in kwargs:
    fail("create_agent still binds a checkpointer: the app binds the one CHECKPOINTER selects")
if not from_get_model(kwargs.get("model")):
    fail("the model must come from get_model() (MODEL_PROVIDER / MODEL_NAME), not be hard-coded")
for name in ("init_chat_model", "ChatOpenAI", "InMemorySaver"):
    if name in source:
        fail(f"{name} is still in app/agent.py")
import app.agent as agent

if getattr(agent.graph, "checkpointer", None) is not None:
    fail("the exported graph has a checkpointer")
print("ok: unbound graph, model from get_model()")
