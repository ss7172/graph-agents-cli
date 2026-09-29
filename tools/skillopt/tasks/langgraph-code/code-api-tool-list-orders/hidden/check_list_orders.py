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

"""Hidden check: list_orders sends GET /orders?status=... through the policy client."""

import os

from _toolkit import call, declared, fail, mock_api, tools_by_name

os.environ["ORDERS_API_BASE_URL"] = "http://orders.test"
os.environ["ORDERS_API_TOKEN"] = "tok-456"
seen = []


def handler(request):
    seen.append(request)
    return __import__("httpx").Response(200, json=[{"id": "ORD-1", "status": "open"}])


tools = tools_by_name()
if "list_orders" not in tools:
    fail(f"no tool named list_orders (tools: {sorted(tools)})")
tool = tools["list_orders"]
if not any(c.get("api") == "orders" and c.get("method") == "GET" and (c.get("operation_id") == "listOrders" or c.get("path") == "/orders") for c in declared(tool)):
    fail(f"API_CALLS of the list_orders module does not declare listOrders: {declared(tool)}")
mock_api(handler)
result = call(tool, {"status": "open"}, "List my open orders")
if isinstance(result, Exception):
    fail(f"list_orders raised {type(result).__name__}: {result}")
if len(seen) != 1 or seen[0].method != "GET" or seen[0].url.path != "/orders":
    fail(f"expected one GET /orders, saw {[(r.method, r.url.path) for r in seen]}")
if seen[0].url.params.get("status") != "open":
    fail(f"the status is not sent as a query parameter: {seen[0].url}")
if "ORD-1" not in str(result):
    fail(f"the result does not contain the orders: {result!r}")
print("ok: GET /orders?status=open through the policy client")
