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

"""Hidden check: the case `berlin-temperature` accepts any temperature in degrees and rejects
replies without one. Applies the case's reply checks (contains, not_contains, regex) the way the
eval grader does to three sample replies."""

import json
import re
import sys
from pathlib import Path

data = json.loads(Path("tests/eval/datasets/basic-dataset.json").read_text())
cases = [c for c in data.get("cases", []) if c.get("id") == "berlin-temperature"]
if len(cases) != 1:
    print("FAIL: expected one case berlin-temperature")
    sys.exit(1)
expect = cases[0].get("expect") or {}
if "berlin" not in json.dumps(cases[0].get("messages")).lower():
    print("FAIL: the case does not ask about Berlin")
    sys.exit(1)
fold = expect.get("case_insensitive", True)


def norm(s):
    return str(s).casefold() if fold else str(s)


def passes(reply):
    for s in expect.get("contains") or []:
        if norm(s) not in norm(reply):
            return False
    for s in expect.get("not_contains") or []:
        if norm(s) in norm(reply):
            return False
    pattern = expect.get("regex")
    if pattern and not re.search(pattern, reply):
        return False
    return True


checks = [k for k in ("contains", "not_contains", "regex") if expect.get(k)]
if not checks:
    print("FAIL: the case checks nothing about the reply")
    sys.exit(1)
good = [
    "Here is what I found: It's 72 degrees and cloudy.",
    "Here is what I found: It's 90 degrees and sunny.",
    "In Berlin it is 12 degrees right now.",
]
bad = [
    "It is warm and sunny in Berlin today.",
    "Temperatures are measured in degrees.",
    "I am a fake model. You said: What is the weather in Berlin?",
]
wrong_good = [r for r in good if not passes(r)]
wrong_bad = [r for r in bad if passes(r)]
if wrong_good:
    print(f"FAIL: the case rejects a reply that states a temperature: {wrong_good[0]!r}")
    sys.exit(1)
if wrong_bad:
    print(f"FAIL: the case accepts a reply without a temperature: {wrong_bad[0]!r}")
    sys.exit(1)
print(f"ok: {checks} accept any number of degrees and reject replies without one")
