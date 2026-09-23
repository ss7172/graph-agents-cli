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

"""The only HTTP path from tools to the product's API (DECISIONS.md D28).

`product-policy.yaml` (path from `PRODUCT_POLICY_PATH`, default
`./product-policy.yaml`) is loaded once at import. `ProductClient.request`
refuses, before sending, any method or operation outside the policy and raises
`PolicyViolation`, which `agent.py` turns into a tool error the model can read.
Credentials are forwarded per the policy's `auth`: the caller's session cookie
or token (`forwarded-session`), `PRODUCT_API_TOKEN` (`bearer`), or nothing.

Without a policy file the client is unrestricted and logs one warning
(ASSUMPTIONS item 18). Every tool module declares `PRODUCT_CALLS`, a list of
``{"method", "operation_id", "path"}`` dicts (empty when it calls no product
API); `check_tool_declarations()` is what `graph-agents-cli lint` runs.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import pkgutil
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import httpx

logger = logging.getLogger(__name__)

POLICY_PATH_ENV = "PRODUCT_POLICY_PATH"
DEFAULT_POLICY_FILE = "product-policy.yaml"
TOOLS_PACKAGE = "{{cookiecutter.agent_directory}}.tools"

AUTH_MODES = ("forwarded-session", "bearer", "none")


class PolicyViolation(Exception):
    """A request outside `product-policy.yaml`. Raised before anything is sent."""


class ProductAPIError(Exception):
    """A configuration or HTTP error talking to the product API."""


@dataclass(frozen=True)
class OperationRule:
    operation_id: str | None = None
    path: str | None = None
    methods: frozenset[str] = frozenset()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> OperationRule:
        return cls(
            operation_id=data.get("operationId") or data.get("operation_id"),
            path=data.get("path"),
            methods=frozenset(str(m).upper() for m in data.get("methods") or []),
        )

    def matches(self, method: str, operation_id: str | None, path: str | None) -> bool:
        """Whether this rule covers the call.

        A rule that pins both `operationId` and `path` needs the id to match
        and, when a concrete path is given, that path to match the template
        too; the id alone never short-circuits a pinned path.
        """
        if self.methods and method not in self.methods:
            return False
        id_ok = (
            self.operation_id is not None
            and operation_id is not None
            and operation_id == self.operation_id
        )
        path_ok = self.path is not None and path is not None and _path_matches(self.path, path)
        if self.operation_id is not None and self.path is not None:
            return id_ok and (path is None or path_ok)
        return id_ok or path_ok

    def describe(self) -> str:
        parts = []
        if self.operation_id:
            parts.append(f"operationId={self.operation_id}")
        if self.path:
            parts.append(f"path={self.path}")
        if self.methods:
            parts.append(f"methods={sorted(self.methods)}")
        return " ".join(parts) or "<any>"


def _path_matches(template: str, path: str) -> bool:
    """`/sites/{siteId}/topology` matches `/sites/42/topology` and itself."""
    if template == path:
        return True
    pattern = re.sub(
        r"\{[^/]+\}", r"[^/]+", re.escape(template).replace(r"\{", "{").replace(r"\}", "}")
    )
    return re.fullmatch(pattern, path.split("?", 1)[0]) is not None


_PLACEHOLDER = re.compile(r"\{([^/{}]+)\}")


def render_path(template: str, path_params: Mapping[str, Any]) -> str:
    """Fill `{name}` placeholders with percent-encoded values; refuse anything that changes the shape.

    A value is one opaque path segment: an empty, `.` or `..` value (which
    would step out of the template) and a value containing `/` or `\\` (a
    server decoding `%2F` would traverse) are a `PolicyViolation`; other
    reserved characters are percent-encoded. Unknown or unfilled parameters
    are violations too.
    """
    names = _PLACEHOLDER.findall(template)
    unknown = sorted(set(path_params) - set(names))
    if unknown:
        raise PolicyViolation(f"path {template!r} has no parameter(s) {', '.join(unknown)}.")
    missing = [n for n in names if n not in path_params]
    if missing:
        raise PolicyViolation(f"path {template!r} needs value(s) for {', '.join(missing)}.")

    def _fill(match: re.Match[str]) -> str:
        name = match.group(1)
        value = str(path_params[name])
        if value in ("", ".", "..") or value.strip() != value or "/" in value or "\\" in value:
            raise PolicyViolation(
                f"path parameter {name}={value!r} is not a valid path segment (refused before sending)."
            )
        return quote(value, safe="")

    return _PLACEHOLDER.sub(_fill, template)


def validate_concrete_path(path: str) -> None:
    """Refuse a path that httpx would normalise or a server would resolve elsewhere.

    Dot segments (`.`/`..`, also percent-encoded), an encoded slash or
    backslash inside a segment, empty segments (`//`), and a query or fragment
    in the path (send them through `params=`) are violations: the policy check
    would otherwise pass a template while the wire path lands on another
    endpoint (for example `/items/1/../../admin` -> `/admin`).
    """
    if "?" in path or "#" in path:
        raise PolicyViolation(f"path {path!r} must not carry a query or fragment; use params=.")
    for segment in path.strip("/").split("/"):
        decoded = unquote(segment)
        if decoded in (".", ".."):
            raise PolicyViolation(f"path {path!r} contains a dot segment (refused).")
        if "/" in decoded or "\\" in decoded:
            raise PolicyViolation(f"path {path!r} contains an encoded slash (refused).")
        if segment == "":
            raise PolicyViolation(f"path {path!r} contains an empty segment (refused).")


@dataclass
class ProductPolicy:
    base_url_env: str = "PRODUCT_API_BASE_URL"
    auth: str = "none"
    token_env: str = "PRODUCT_API_TOKEN"
    allowed_methods: frozenset[str] = frozenset()
    allowed_operations: list[OperationRule] | None = None
    denied_operations: list[OperationRule] = field(default_factory=list)
    openapi: str | None = None
    timeouts_ms: dict[str, int] = field(default_factory=lambda: {"connect": 2000, "read": 5000})
    pagination: dict[str, Any] = field(default_factory=dict)
    source: Path | None = None

    @property
    def declared(self) -> bool:
        """True when a policy file was loaded (otherwise the client is unrestricted)."""
        return self.source is not None

    @classmethod
    def unrestricted(cls) -> ProductPolicy:
        return cls()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], source: Path | None = None) -> ProductPolicy:
        section = data.get("product_api", data) or {}
        auth = str(section.get("auth") or "none")
        if auth not in AUTH_MODES:
            raise ValueError(f"product-policy auth must be one of {AUTH_MODES}, got {auth!r}")
        allowed_ops_raw = section.get("allowed_operations")
        allowed_ops = (
            [OperationRule.from_dict(o) for o in allowed_ops_raw] if allowed_ops_raw else None
        )
        timeouts = {"connect": 2000, "read": 5000}
        timeouts.update({k: int(v) for k, v in (section.get("timeouts_ms") or {}).items()})
        return cls(
            base_url_env=str(section.get("base_url_env") or "PRODUCT_API_BASE_URL"),
            auth=auth,
            token_env=str(section.get("token_env") or "PRODUCT_API_TOKEN"),
            allowed_methods=frozenset(str(m).upper() for m in section.get("allowed_methods") or []),
            allowed_operations=allowed_ops,
            denied_operations=[
                OperationRule.from_dict(o) for o in section.get("denied_operations") or []
            ],
            openapi=section.get("openapi"),
            timeouts_ms=timeouts,
            pagination=dict(section.get("pagination") or {}),
            source=source,
        )

    @classmethod
    def load(cls, path: str | Path) -> ProductPolicy:
        import yaml

        p = Path(path)
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return cls.from_dict(data, source=p)

    def check(self, method: str, operation_id: str | None = None, path: str | None = None) -> None:
        """Raise `PolicyViolation` when the call is outside the policy."""
        method = method.upper()
        if not self.declared:
            return
        name = self.source.name if self.source else DEFAULT_POLICY_FILE
        what = operation_id or path or "<unnamed operation>"
        if self.allowed_methods and method not in self.allowed_methods:
            raise PolicyViolation(
                f"{method} {what} refused: {name} allows only {sorted(self.allowed_methods)}."
            )
        for rule in self.denied_operations:
            if rule.matches(method, operation_id, path):
                raise PolicyViolation(
                    f"{method} {what} refused: denied by {name} ({rule.describe()})."
                )
        if self.allowed_operations is not None and not any(
            rule.matches(method, operation_id, path) for rule in self.allowed_operations
        ):
            raise PolicyViolation(f"{method} {what} refused: not in the allow-list of {name}.")


_warned_unrestricted = False


def resolve_policy_path(path: str | Path | None = None) -> Path:
    return Path(path or os.environ.get(POLICY_PATH_ENV) or DEFAULT_POLICY_FILE)


def load_policy(path: str | Path | None = None) -> ProductPolicy:
    """Load the policy file, or an unrestricted policy with one warning when absent."""
    global _warned_unrestricted
    p = resolve_policy_path(path)
    if p.exists():
        return ProductPolicy.load(p)
    if not _warned_unrestricted:
        logger.warning(
            "No %s found (PRODUCT_POLICY_PATH=%s): product_client is unrestricted. Declare a "
            "policy with `graph-agents-cli create --product-policy <file>` before calling a product API.",
            DEFAULT_POLICY_FILE,
            p,
        )
        _warned_unrestricted = True
    return ProductPolicy.unrestricted()


POLICY: ProductPolicy = load_policy()


@dataclass
class ForwardedCredentials:
    """What `forwarded-session` sends: the caller's cookie header and/or session token."""

    cookie: str | None = None
    session_token: str | None = None

    @classmethod
    def from_attributes(cls, attributes: Mapping[str, Any]) -> ForwardedCredentials:
        cookie = attributes.get("session_cookie")
        if isinstance(cookie, Mapping):
            cookie = "; ".join(f"{k}={v}" for k, v in cookie.items())
        return cls(cookie=cookie, session_token=attributes.get("session_token"))

    def headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.cookie:
            headers["Cookie"] = self.cookie
        if self.session_token:
            headers["X-Session-Token"] = self.session_token
        return headers


class ProductClient:
    """Policy-enforcing HTTP client for the product API."""

    def __init__(
        self,
        policy: ProductPolicy | None = None,
        *,
        base_url: str | None = None,
        credentials: ForwardedCredentials | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.policy = policy if policy is not None else POLICY
        self._base_url = base_url
        self.credentials = credentials or ForwardedCredentials()
        self._transport = transport

    @property
    def base_url(self) -> str:
        url = self._base_url or os.environ.get(self.policy.base_url_env, "")
        if not url:
            raise ProductAPIError(
                f"{self.policy.base_url_env} is not set; cannot reach the product API."
            )
        return url.rstrip("/")

    def for_principal(self, principal: Any) -> ProductClient:
        """A client forwarding `principal.attributes` session credentials."""
        attributes = getattr(principal, "attributes", None) or {}
        return ProductClient(
            self.policy,
            base_url=self._base_url,
            credentials=ForwardedCredentials.from_attributes(attributes),
            transport=self._transport,
        )

    def auth_headers(self) -> dict[str, str]:
        mode = self.policy.auth
        if mode == "bearer":
            token = os.environ.get(self.policy.token_env, "")
            if not token:
                raise ProductAPIError(
                    f"{self.policy.token_env} is not set (product-policy auth: bearer)."
                )
            return {"Authorization": f"Bearer {token}"}
        if mode == "forwarded-session":
            headers = self.credentials.headers()
            if not headers:
                logger.debug(
                    "forwarded-session: the caller carried no session credential to forward."
                )
            return headers
        return {}

    def timeout(self) -> httpx.Timeout:
        t = self.policy.timeouts_ms
        return httpx.Timeout(
            connect=t.get("connect", 2000) / 1000,
            read=t.get("read", 5000) / 1000,
            write=10.0,
            pool=10.0,
        )

    async def request(
        self,
        method: str,
        operation_id: str | None = None,
        path: str | None = None,
        *,
        path_params: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Send `method` on `path` after the policy check; returns JSON or text.

        Pass `path` as the template declared in `PRODUCT_CALLS` (for example
        `/items/{item_id}`) with the values in `path_params`: the policy is
        checked against the template and the client renders and encodes the
        values, so model-chosen input can never change which endpoint is hit.
        A concrete `path` is still accepted but validated (no dot segments,
        encoded slashes, empty segments, query or fragment). Raises
        `PolicyViolation` (never sent) or `ProductAPIError` (configuration or
        non-2xx response).
        """
        if operation_id is None and path is None:
            raise ValueError("request() needs an operation_id and/or a path.")
        self.policy.check(method, operation_id, path)
        if path is None:
            raise ProductAPIError(f"operation {operation_id} needs a path to be sent.")
        if path_params is not None:
            wire_path = render_path(path, path_params)
        elif _PLACEHOLDER.search(path):
            raise PolicyViolation(f"path {path!r} has unfilled parameters; pass path_params=.")
        else:
            wire_path = path
        validate_concrete_path(wire_path)
        url = f"{self.base_url}/{wire_path.lstrip('/')}"
        if httpx.URL(url).raw_path.decode().split("?", 1)[0] != "/" + wire_path.lstrip("/"):
            raise PolicyViolation(
                f"path {wire_path!r} would be normalised before sending (refused)."
            )
        request_headers = {**self.auth_headers(), **(headers or {})}
        async with httpx.AsyncClient(transport=self._transport, timeout=self.timeout()) as client:
            try:
                response = await client.request(
                    method.upper(),
                    url,
                    params=params,
                    json=json_body,
                    headers=request_headers,
                    **kwargs,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise ProductAPIError(
                    f"{method.upper()} {wire_path} -> HTTP {exc.response.status_code}"
                ) from exc
            except httpx.HTTPError as exc:
                raise ProductAPIError(
                    f"{method.upper()} {wire_path} failed: {type(exc).__name__}"
                ) from exc
        if "json" in response.headers.get("content-type", ""):
            return response.json()
        return response.text


_default_client: ProductClient | None = None


def get_product_client(principal: Any = None) -> ProductClient:
    """The process-wide client, scoped to `principal` when given."""
    global _default_client
    if _default_client is None:
        _default_client = ProductClient(POLICY)
    return _default_client.for_principal(principal) if principal is not None else _default_client


# ---------------------------------------------------------------------------
# Build-time check (run by `graph-agents-cli lint`)
# ---------------------------------------------------------------------------


def check_tool_declarations(
    package: str = TOOLS_PACKAGE, policy: ProductPolicy | None = None
) -> list[str]:
    """Import every tool module and verify its `PRODUCT_CALLS` against the policy.

    Returns a list of violations (empty means the check passed). When the
    policy sets `openapi`, every call must also exist in that spec.
    """
    policy = policy if policy is not None else POLICY
    violations: list[str] = []
    try:
        pkg = importlib.import_module(package)
    except ImportError as exc:
        return [f"cannot import {package}: {exc}"]
    spec = _load_openapi(policy) if policy.openapi else None
    for info in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda i: i.name):
        modname = f"{package}.{info.name}"
        try:
            module = importlib.import_module(modname)
        except Exception as exc:  # report, do not crash lint
            violations.append(f"{modname}: import failed: {type(exc).__name__}: {exc}")
            continue
        calls = getattr(module, "PRODUCT_CALLS", None)
        if calls is None:
            violations.append(
                f"{modname}: missing module-level PRODUCT_CALLS (use [] when no product API is called)"
            )
            continue
        for call in calls:
            method = str(call.get("method", "")).upper()
            operation_id = call.get("operation_id") or call.get("operationId")
            path = call.get("path")
            if not method or (operation_id is None and path is None):
                violations.append(
                    f"{modname}: PRODUCT_CALLS entry needs method and operation_id/path: {call}"
                )
                continue
            try:
                policy.check(method, operation_id, path)
            except PolicyViolation as exc:
                violations.append(f"{modname}: {exc}")
            if spec is not None and not _openapi_has(spec, method, operation_id, path):
                violations.append(
                    f"{modname}: {method} {operation_id or path} is not in {policy.openapi}"
                )
    return violations


def _load_openapi(policy: ProductPolicy) -> dict[str, Any]:
    path = Path(policy.openapi or "")
    if policy.source is not None and not path.is_absolute():
        candidate = policy.source.parent / path
        path = candidate if candidate.exists() else path
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return json.loads(text)
    import yaml

    return yaml.safe_load(text) or {}


def _openapi_has(
    spec: Mapping[str, Any], method: str, operation_id: str | None, path: str | None
) -> bool:
    for spec_path, item in (spec.get("paths") or {}).items():
        op = (item or {}).get(method.lower())
        if not isinstance(op, Mapping):
            continue
        if operation_id is not None and op.get("operationId") == operation_id:
            return True
        if path is not None and _path_matches(spec_path, path):
            return True
    return False
