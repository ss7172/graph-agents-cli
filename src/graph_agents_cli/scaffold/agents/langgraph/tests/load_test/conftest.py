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

"""`load_test.py` is a Locust file run by `locust`, not a pytest module.

Its name matches pytest's `*_test.py` pattern and it imports locust (not a
project dependency), so a plain `pytest` would fail to collect it.
"""

collect_ignore = ["load_test.py"]
