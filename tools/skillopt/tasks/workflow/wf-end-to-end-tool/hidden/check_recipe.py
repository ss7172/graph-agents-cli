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

"""Hidden check: suggest_recipe is registered, declares no API calls and uses the ingredient."""

from _toolkit import call, fail, module_of, tools_by_name

tools = tools_by_name()
if "suggest_recipe" not in tools:
    fail(f"no tool named suggest_recipe (tools: {sorted(tools)})")
tool = tools["suggest_recipe"]
if getattr(module_of(tool), "API_CALLS", None) != []:
    fail("the module must declare API_CALLS = []")
result = str(call(tool, {"ingredient": "mushrooms"}))
if "mushroom" not in result.lower():
    fail(f"the recipe does not use the ingredient: {result!r}")
print("ok: suggest_recipe")
