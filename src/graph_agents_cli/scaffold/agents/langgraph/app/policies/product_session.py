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

"""`product-session` policy: interface plus a fail-closed stub (D23, ASSUMPTIONS 14).

The consuming project implements it: validate the caller's existing product
session (a forwarded cookie or `X-Session-Token`) by calling the product's
session or current-user endpoint through `app_utils.product_client` (that
call must be allowed by `product-policy.yaml`), load the caller's roles and
permissions from the response on every request, and set
`attributes["tenant"]`, `attributes["session_cookie"]` / `attributes["session_token"]`
so tools can forward the same credential. Thread ownership is then enforced by
`app_utils.threads` (fastapi) or the owner filters in `app_utils.auth` (server),
with `AUTH_READ_ACROSS_ROLES` listing the roles that may read others' threads.

Until it is implemented every request fails with 503 and
`graph-agents-cli deploy --env staging|prod` refuses while the manifest says
`auth_policy_implemented: false`. Flip that flag after replacing this stub.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from {{cookiecutter.agent_directory}}.app_utils.auth import Principal

NOT_IMPLEMENTED = (
    "AUTH_POLICY=product-session is scaffolded as a fail-closed stub. Implement "
    "ProductSessionPolicy in {{cookiecutter.agent_directory}}/policies/product_session.py (validate the "
    "session through app_utils.product_client, load roles and permissions) and set "
    "auth_policy_implemented: true in graph-agents-cli-manifest.yaml."
)


class ProductSessionPolicy:
    """Validates the product session on every request. Stub: fails closed."""

    async def authenticate(self, request: Request) -> Principal:
        raise HTTPException(status_code=503, detail=NOT_IMPLEMENTED)

    async def authorize(self, principal: Principal, action: str, resource: str | None) -> None:
        raise HTTPException(status_code=503, detail=NOT_IMPLEMENTED)
