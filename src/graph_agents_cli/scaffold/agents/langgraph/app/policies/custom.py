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
cover, for example the session cookie of an existing application (for OIDC
bearer tokens use the built-in `jwt` policy instead). The same policy object
guards every surface: the chat and thread routes, the A2A card and JSON-RPC
endpoint, and, under `langgraph-server`, the server's native API.

Implement `CustomPolicy` so that, on every request:

* `authenticate(request)` validates the caller's credential (a cookie, a
  header, a token) against the system that issued it and returns a
  `Principal`:
  - `id`: stable and unique per caller. It owns threads and A2A tasks (two
    callers with the same id see each other's conversations), and is hashed
    into traces and run records.
  - `roles`: matched against `AUTH_READ_ACROSS_ROLES` (may read other
    principals' threads) and `AUTH_ADMIN_ROLES` (may manage assistants, crons
    and the store under langgraph-server). Role names must not contain commas.
  - `permissions`: usually `set(ACTIONS)`; `authorize` decides per action.
  - `attributes`: optional, for example `tenant`. Secrets go only under
    `attributes["credentials"][<api name>]` (see below).
  A missing or invalid credential raises `HTTPException(401)` with a
  `WWW-Authenticate` header; a server-side problem (the issuer cannot be
  reached) raises `HTTPException(503)`. Never put the credential itself in an
  error detail or a log line.
* `authorize(principal, action, resource)` allows or refuses `action` (see
  `app_utils.auth.ACTIONS`) on `resource` (a thread id or None) with
  `HTTPException(403)`. Thread ownership is enforced separately, by
  `app_utils.threads` (fastapi) and the owner filters in `app_utils.auth`
  (langgraph-server).
* Optionally `startup_problems() -> list[str]`: configuration problems found
  when the app starts. Any problem stops the process outside `APP_ENV=dev`
  (see `app_utils.auth.check_startup`).

Both methods are awaited on the request path: do blocking I/O through an
async client, and cache what you can (sessions, keys) with a short TTL.

To let tools call an `auth: forward` API of `api-policy.yaml` with the caller's
own credential, put it in `attributes["credentials"][<api name>]`; that is the
only attribute that may hold a secret (`Principal.public_attributes()` is what
gets persisted, logged or traced).

Until it is implemented every request fails with 503, and
`graph-agents-cli deploy --env staging|prod` refuses while the manifest says
`auth_policy_implemented: false`. Flip that flag after replacing this stub,
and add tests beside `tests/unit/test_policy.py` (a valid credential, a
missing one, an invalid one, and two principals that must not see each
other's threads).
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
