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

"""Hidden check: set_ticket_status PATCHes /tickets/<id> with {"status": ...}."""

import json
import os

from _toolkit import call, declared, fail, mock_api, tools_by_name

os.environ["TICKETS_API_BASE_URL"] = "http://tickets.test"
os.environ["TICKETS_API_TOKEN"] = "tok-tickets-5"
seen = []


def handler(request):
    seen.append(request)
    return __import__("httpx").Response(200, json={"id": "T-42", "status": "closed"})


tools = tools_by_name()
if "set_ticket_status" not in tools:
    fail(f"no tool named set_ticket_status (tools: {sorted(tools)})")
tool = tools["set_ticket_status"]
if not any(
    c.get("api") == "tickets"
    and c.get("method") == "PATCH"
    and (c.get("operation_id") == "updateTicket" or c.get("path") == "/tickets/{ticket_id}")
    for c in declared(tool)
):
    fail(f"API_CALLS does not declare PATCH updateTicket: {declared(tool)}")
mock_api(handler)
result = call(tool, {"ticket_id": "T-42", "status": "closed"}, "Close ticket T-42 please")
if isinstance(result, Exception):
    fail(f"set_ticket_status raised {type(result).__name__}: {result}")
if len(seen) != 1 or seen[0].method != "PATCH" or seen[0].url.path != "/tickets/T-42":
    fail(f"expected one PATCH /tickets/T-42, saw {[(r.method, r.url.path) for r in seen]}")
body = json.loads(seen[0].content or b"null")
if body != {"status": "closed"}:
    fail(f"the JSON body was {body!r}")
print(f"ok: PATCH /tickets/T-42 {body}")
