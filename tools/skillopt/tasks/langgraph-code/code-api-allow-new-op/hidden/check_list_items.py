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

"""Hidden check: list_items exists and declares GET listItems in API_CALLS."""

from _toolkit import declared, fail, tools_by_name

tools = tools_by_name()
if "list_items" not in tools:
    fail(f"no tool named list_items (tools: {sorted(tools)})")
calls = declared(tools["list_items"])
if not any(
    c.get("api") == "inventory"
    and c.get("method") == "GET"
    and (c.get("operation_id") == "listItems" or c.get("path") == "/items")
    for c in calls
):
    fail(f"API_CALLS does not declare GET listItems: {calls}")
print("ok: list_items declares GET listItems")
