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

"""Hidden check: reserve_item exists and declares its POST reserveItem call."""

from _toolkit import declared, fail, tools_by_name

tools = tools_by_name()
if "reserve_item" not in tools:
    fail(f"no tool named reserve_item (tools: {sorted(tools)})")
calls = declared(tools["reserve_item"])
if not any(c.get("api") == "inventory" and c.get("method") == "POST" and (c.get("operation_id") == "reserveItem" or c.get("path") == "/items/{item_id}/reserve") for c in calls):
    fail(f"API_CALLS does not declare POST reserveItem: {calls}")
print("ok: reserve_item declares POST reserveItem")
