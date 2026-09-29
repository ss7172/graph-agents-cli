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

"""Hidden check: convert_temperature is registered, declares no API calls and converts."""

from _toolkit import call, fail, module_of, tools_by_name

tools = tools_by_name()
if "convert_temperature" not in tools or "get_weather" not in tools:
    fail(f"expected convert_temperature and get_weather among the tools: {sorted(tools)}")
tool = tools["convert_temperature"]
if getattr(module_of(tool), "API_CALLS", None) != []:
    fail("the module must declare API_CALLS = [] (it calls no external API)")
hot = str(call(tool, {"value": 100, "unit": "C"}))
cold = str(call(tool, {"value": 32, "unit": "F"}))
if "212" not in hot:
    fail(f"100 C should be 212 F: {hot!r}")
if "0" not in cold.replace("32", ""):
    fail(f"32 F should be 0 C: {cold!r}")
print(f"ok: {hot} / {cold}")
