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

"""AuthPolicy registry: `AUTH_POLICY` value -> policy factory.

Agent code, never touched by `graph-agents-cli scaffold upgrade`. Add a policy
by registering its factory here.
"""

from __future__ import annotations

from collections.abc import Callable

from {{cookiecutter.agent_directory}}.app_utils.auth import (
    CUSTOM,
    JWT,
    SHARED_BEARER,
    AuthPolicy,
    JwtPolicy,
    SharedBearerPolicy,
)


def _custom() -> AuthPolicy:
    from {{cookiecutter.agent_directory}}.policies.custom import CustomPolicy

    return CustomPolicy()


REGISTRY: dict[str, Callable[[], AuthPolicy]] = {
    SHARED_BEARER: SharedBearerPolicy,
    JWT: JwtPolicy,
    CUSTOM: _custom,
}


def build_policy(name: str) -> AuthPolicy:
    try:
        factory = REGISTRY[name]
    except KeyError:
        raise RuntimeError(
            f"Unknown AUTH_POLICY {name!r}; expected one of {sorted(REGISTRY)}."
        ) from None
    return factory()
