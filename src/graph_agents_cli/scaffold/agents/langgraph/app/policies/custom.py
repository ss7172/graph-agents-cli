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

"""`custom` policy: the interface the project implements, shipped as a fail-closed stub.

Use it when callers authenticate with something the built-in policies do not
cover, for example the session cookie of an existing application. Implement
`CustomPolicy` so that, on every request:

* `authenticate` validates the caller's credential (a cookie, a header, a
  token) against the system that issued it and returns a `Principal` with a
  stable `id`, the caller's `roles` and `permissions`, and optional
  `attributes` (for example `tenant`). A missing or invalid credential raises
  `HTTPException(401)`.
* `authorize` allows or refuses `action` (see `app_utils.auth.ACTIONS`) on
  `resource` (a thread id or None) with `HTTPException(403)`.

To let tools call an `auth: forward` API of `api-policy.yaml` with the caller's
own credential, put it in `attributes["credentials"][<api name>]`; that is the
only attribute that may hold a secret (`Principal.public_attributes()` is what
gets persisted, logged or traced). Thread ownership is then enforced by
`app_utils.threads` (fastapi) or the owner filters in `app_utils.auth`
(langgraph-server), with `AUTH_READ_ACROSS_ROLES` listing the roles that may
read others' threads.

Until it is implemented every request fails with 503, and
`graph-agents-cli deploy --env staging|prod` refuses while the manifest says
`auth_policy_implemented: false`. Flip that flag after replacing this stub.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from {{cookiecutter.agent_directory}}.app_utils.auth import Principal

NOT_IMPLEMENTED = (
    "AUTH_POLICY=custom is scaffolded as a fail-closed stub. Implement CustomPolicy in "
    "{{cookiecutter.agent_directory}}/policies/custom.py (authenticate the caller, load roles "
    "and permissions) and set auth_policy_implemented: true in graph-agents-cli-manifest.yaml."
)


class CustomPolicy:
    """The project's own authentication and authorization. Stub: fails closed."""

    async def authenticate(self, request: Request) -> Principal:
        raise HTTPException(status_code=503, detail=NOT_IMPLEMENTED)

    async def authorize(self, principal: Principal, action: str, resource: str | None) -> None:
        raise HTTPException(status_code=503, detail=NOT_IMPLEMENTED)
