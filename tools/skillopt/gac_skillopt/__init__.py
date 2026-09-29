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

"""gac-bench: a benchmark of graph-agents-cli skill tasks, and a SkillOpt environment for it.

Contributor tooling only. Nothing here is imported by the CLI or by a generated project, and
nothing here is packaged into the wheel. Only ``adapter`` and ``run`` import SkillOpt; every
other module works with the standard library (plus PyYAML) so the verifier, the tasks and the
scripted solutions can be checked without it.
"""
