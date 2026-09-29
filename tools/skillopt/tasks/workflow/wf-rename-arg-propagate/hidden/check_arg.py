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

"""Hidden check: get_weather takes `city` (not `query`) and still answers."""

import inspect

from _toolkit import call, fail, function_of, tools_by_name

tools = tools_by_name()
if "get_weather" not in tools:
    fail(f"no tool named get_weather: {sorted(tools)}")
params = list(inspect.signature(function_of(tools["get_weather"])).parameters)
if "city" not in params or "query" in params:
    fail(f"get_weather parameters are {params}, expected city and no query")
result = str(call(tools["get_weather"], {"city": "Paris"}))
if "sunny" not in result:
    fail(f"get_weather(city='Paris') returned {result!r}")
print("ok: get_weather(city)")
