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

"""Hidden check: get_item sends GET /items/<id> through the policy client and declares it."""

import json
import os

from _toolkit import call, declared, fail, mock_api, tools_by_name

os.environ["INVENTORY_API_BASE_URL"] = "http://inventory.test"
os.environ["INVENTORY_API_TOKEN"] = "tok-123"
seen = []


def handler(request):
    seen.append(request)
    return __import__("httpx").Response(200, json={"id": "ABC-1", "name": "widget"})


tools = tools_by_name()
if "get_item" not in tools:
    fail(f"no tool named get_item (tools: {sorted(tools)})")
tool = tools["get_item"]
if not any(c.get("api") == "inventory" and c.get("method") == "GET" and (c.get("operation_id") == "getItem" or c.get("path") == "/items/{item_id}") for c in declared(tool)):
    fail(f"API_CALLS of the get_item module does not declare the getItem call: {declared(tool)}")
mock_api(handler)
result = call(tool, {"item_id": "ABC-1"}, "Show me item ABC-1")
if isinstance(result, Exception):
    fail(f"get_item raised {type(result).__name__}: {result}")
if len(seen) != 1:
    fail(f"expected one request through the policy client, saw {len(seen)}")
request = seen[0]
if request.method != "GET" or request.url.path != "/items/ABC-1":
    fail(f"sent {request.method} {request.url.path}")
if request.headers.get("authorization") != "Bearer tok-123":
    fail("the request did not carry the policy's bearer token")
if "ABC-1" not in json.dumps(result) and "ABC-1" not in str(result):
    fail(f"the result does not contain the record: {result!r}")
print("ok: GET /items/ABC-1 through the policy client")
