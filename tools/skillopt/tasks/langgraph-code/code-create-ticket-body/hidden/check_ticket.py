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

"""Hidden check: create_ticket POSTs /tickets with the JSON body through the policy client."""

import json
import os

from _toolkit import call, declared, fail, mock_api, tools_by_name

os.environ["TICKETS_API_BASE_URL"] = "http://tickets.test"
os.environ["TICKETS_API_TOKEN"] = "tok-tickets-3"
seen = []


def handler(request):
    seen.append(request)
    return __import__("httpx").Response(201, json={"id": "T-100", "status": "open"})


tools = tools_by_name()
if "create_ticket" not in tools:
    fail(f"no tool named create_ticket (tools: {sorted(tools)})")
tool = tools["create_ticket"]
if not any(
    c.get("api") == "tickets"
    and c.get("method") == "POST"
    and (c.get("operation_id") == "createTicket" or c.get("path") == "/tickets")
    for c in declared(tool)
):
    fail(f"API_CALLS does not declare POST createTicket: {declared(tool)}")
mock_api(handler)
result = call(
    tool,
    {"title": "VPN down", "description": "The VPN drops every 5 minutes."},
    "Open a ticket: VPN down, the VPN drops every 5 minutes.",
)
if isinstance(result, Exception):
    fail(f"create_ticket raised {type(result).__name__}: {result}")
if len(seen) != 1 or seen[0].method != "POST" or seen[0].url.path != "/tickets":
    fail(f"expected one POST /tickets, saw {[(r.method, r.url.path) for r in seen]}")
body = json.loads(seen[0].content or b"null")
if body != {"title": "VPN down", "description": "The VPN drops every 5 minutes."}:
    fail(f"the JSON body was {body!r}")
if seen[0].headers.get("authorization") != "Bearer tok-tickets-3":
    fail("the request did not carry the policy's bearer token")
print(f"ok: POST /tickets {body}")
