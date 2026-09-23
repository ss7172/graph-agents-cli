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
by registering its factory here (an `OIDCPolicy` is a documented future one).
"""

from __future__ import annotations

from collections.abc import Callable

from {{cookiecutter.agent_directory}}.app_utils.auth import (
    PRODUCT_SESSION,
    SHARED_BEARER,
    AuthPolicy,
    SharedBearerPolicy,
)


def _product_session() -> AuthPolicy:
    from {{cookiecutter.agent_directory}}.policies.product_session import ProductSessionPolicy

    return ProductSessionPolicy()


REGISTRY: dict[str, Callable[[], AuthPolicy]] = {
    SHARED_BEARER: SharedBearerPolicy,
    PRODUCT_SESSION: _product_session,
}


def build_policy(name: str) -> AuthPolicy:
    try:
        factory = REGISTRY[name]
    except KeyError:
        raise RuntimeError(
            f"Unknown AUTH_POLICY {name!r}; expected one of {sorted(REGISTRY)}."
        ) from None
    return factory()
