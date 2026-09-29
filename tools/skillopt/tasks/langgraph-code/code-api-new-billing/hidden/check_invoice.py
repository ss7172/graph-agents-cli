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

"""Hidden check: get_invoice sends GET /invoices/<id> through the policy client."""

import json
import os

from _toolkit import call, declared, fail, mock_api, tools_by_name

os.environ["BILLING_API_BASE_URL"] = "http://billing.test"
os.environ["BILLING_API_TOKEN"] = "tok-billing-9"
seen = []


def handler(request):
    seen.append(request)
    return __import__("httpx").Response(200, json={"id": "INV-7", "status": "open"})


tools = tools_by_name()
if "get_invoice" not in tools:
    fail(f"no tool named get_invoice (tools: {sorted(tools)})")
tool = tools["get_invoice"]
if not any(
    c.get("api") == "billing"
    and c.get("method") == "GET"
    and (c.get("operation_id") == "getInvoice" or c.get("path") == "/invoices/{invoice_id}")
    for c in declared(tool)
):
    fail(f"API_CALLS does not declare GET getInvoice: {declared(tool)}")
mock_api(handler)
result = call(tool, {"invoice_id": "INV-7"}, "Show me invoice INV-7")
if isinstance(result, Exception):
    fail(f"get_invoice raised {type(result).__name__}: {result}")
if len(seen) != 1 or seen[0].method != "GET" or seen[0].url.path != "/invoices/INV-7":
    fail(f"expected one GET /invoices/INV-7, saw {[(r.method, r.url.path) for r in seen]}")
if seen[0].headers.get("authorization") != "Bearer tok-billing-9":
    fail("the request did not carry the policy's bearer token")
if "INV-7" not in json.dumps(result) and "INV-7" not in str(result):
    fail(f"the result does not contain the invoice: {result!r}")
print("ok: GET /invoices/INV-7 through the policy client")
