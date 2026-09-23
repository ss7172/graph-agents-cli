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

"""Policy-enforcing HTTP clients for the external APIs the agent's tools call.

`api-policy.yaml` (path from `API_POLICY_PATH`, default `./api-policy.yaml`,
else the one next to the project's `pyproject.toml`) declares every API a tool
may call: where it lives (`base_url_env`), how requests authenticate
(`auth: none | bearer | forward`) and which methods and operations are
allowed. `get_client(name)` returns a client for one declared API; every
request outside the policy raises `ApiPolicyError` before anything is sent.

Fail closed: without a policy file, with an invalid one, or for an API the
file does not declare, `get_client` raises `ApiPolicyError`; there is no
unrestricted fallback. `auth: bearer` sends `Authorization: Bearer
$<token_env>`. `auth: forward` sends the calling principal's own credential for
that API, `principal.attributes["credentials"][<name>]`, in `forward_header`
(default `Authorization`); the principal comes from the run context the server
sets for the graph run, and nothing is sent when the caller has no credential.

Every tool module declares `API_CALLS`, a module-level list of
`{"api", "method", "operation_id", "path"}` dicts naming each call it makes;
`graph-agents-cli lint` checks those declarations against the same rules.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import httpx
import yaml

logger = logging.getLogger(__name__)

POLICY_PATH_ENV = "API_POLICY_PATH"

# --- BEGIN SHARED API POLICY RULES ---
# Identical in graph-agents-cli (graph_agents_cli/_api_policy.py) and in every
# scaffolded project (app_utils/api_client.py). A CLI test keeps the two copies
# byte-identical: change both or neither.

POLICY_FILENAME = "api-policy.yaml"
AUTH_MODES = ("none", "bearer", "forward")
HTTP_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
ANY_METHOD = "*"
DEFAULT_FORWARD_HEADER = "Authorization"
DEFAULT_TIMEOUTS_MS = (("connect", 2000), ("read", 5000))

API_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
API_NAME_RULE = "lowercase letters, digits and underscores, starting with a letter, 1-32 characters"
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9-]+$")
_PATH_SEGMENT_RE = re.compile(r"^(?:[^/?#\s{}]|\{[A-Za-z_][A-Za-z0-9_]*\})+$")
_PLACEHOLDER_SPLIT_RE = re.compile(r"(\{[^/{}]+\})")

_POLICY_KEYS = ("apis",)
_API_KEYS = (
    "base_url_env",
    "auth",
    "token_env",
    "forward_header",
    "allowed_methods",
    "allowed_operations",
    "denied_operations",
    "openapi",
    "timeouts_ms",
    "pagination",
)
_OPERATION_KEYS = ("operationId", "path", "methods")
_TIMEOUT_KEYS = ("connect", "read")
_PAGINATION_KEYS = ("page_size_param", "max_page_size")

LEGACY_POLICY_HINT = (
    "product_api: is the retired single-API format: move its fields under "
    "apis: <name>: (for example apis: example:), add the now required "
    "allowed_methods (for example [GET]), write auth: forward instead of "
    "forwarded-session, and name the file api-policy.yaml"
)


def policy_errors(data: Any) -> list[str]:
    """Every schema error in a parsed api-policy.yaml document; empty when it is valid.

    Strict: unknown keys at any level are errors, so a typo can never widen access.
    """
    if not isinstance(data, Mapping):
        return ["the document must be a mapping with a top-level 'apis' key"]
    errors: list[str] = []
    if "product_api" in data:
        errors.append(LEGACY_POLICY_HINT)
    for key in sorted(set(data) - set(_POLICY_KEYS) - {"product_api"}, key=str):
        errors.append(f"unknown top-level key {key!r} (allowed: apis)")
    if "apis" not in data:
        if "product_api" not in data:
            errors.append("apis: required (a mapping of API name to its settings)")
        return errors
    apis = data["apis"]
    if not isinstance(apis, Mapping) or not apis:
        errors.append("apis: must be a non-empty mapping of API name to its settings")
        return errors
    for name, api in apis.items():
        errors.extend(_api_errors(name, api))
    return errors


def _is_env_name(value: Any) -> bool:
    return isinstance(value, str) and ENV_NAME_RE.match(value) is not None


def _api_errors(name: Any, api: Any) -> list[str]:
    where = f"apis.{name}"
    errors: list[str] = []
    if not isinstance(name, str) or not API_NAME_RE.match(name):
        errors.append(f"{where}: invalid API name ({API_NAME_RULE})")
    if not isinstance(api, Mapping):
        errors.append(f"{where}: must be a mapping")
        return errors
    for key in sorted(set(api) - set(_API_KEYS), key=str):
        errors.append(f"{where}: unknown key {key!r}")

    if "base_url_env" not in api:
        errors.append(f"{where}.base_url_env: required")
    elif not _is_env_name(api["base_url_env"]):
        errors.append(f"{where}.base_url_env: must be an environment variable name")

    auth = api.get("auth")
    if "auth" not in api:
        errors.append(f"{where}.auth: required (one of none, bearer, forward)")
    elif auth not in AUTH_MODES:
        errors.append(f"{where}.auth: must be one of none, bearer, forward (got {auth!r})")

    if auth == "bearer":
        if "token_env" not in api:
            errors.append(f"{where}.token_env: required when auth is bearer")
        elif not _is_env_name(api["token_env"]):
            errors.append(f"{where}.token_env: must be an environment variable name")
    elif "token_env" in api:
        errors.append(f"{where}.token_env: only valid with auth: bearer")

    if "forward_header" in api:
        header = api["forward_header"]
        if auth != "forward":
            errors.append(f"{where}.forward_header: only valid with auth: forward")
        elif not (isinstance(header, str) and HEADER_NAME_RE.match(header)):
            errors.append(f"{where}.forward_header: must be an HTTP header name")

    if "allowed_methods" not in api:
        errors.append(f'{where}.allowed_methods: required (a list of HTTP methods, or ["*"])')
    else:
        errors.extend(_methods_errors(f"{where}.allowed_methods", api["allowed_methods"], True))

    if "allowed_operations" in api:
        errors.extend(
            _operations_errors(f"{where}.allowed_operations", api["allowed_operations"], False)
        )
    if "denied_operations" in api:
        errors.extend(
            _operations_errors(f"{where}.denied_operations", api["denied_operations"], True)
        )

    if "openapi" in api:
        openapi = api["openapi"]
        if not (isinstance(openapi, str) and openapi.strip()):
            errors.append(f"{where}.openapi: must be a file path")

    if "timeouts_ms" in api:
        errors.extend(_timeouts_errors(f"{where}.timeouts_ms", api["timeouts_ms"]))
    if "pagination" in api:
        errors.extend(_pagination_errors(f"{where}.pagination", api["pagination"]))
    return errors


def _methods_errors(where: str, value: Any, allow_any: bool) -> list[str]:
    if not isinstance(value, list) or not value:
        return [f"{where}: must be a non-empty list of HTTP methods"]
    errors: list[str] = []
    if allow_any and ANY_METHOD in value and len(value) != 1:
        errors.append(f'{where}: "*" must be the only entry when present')
    for method in value:
        if allow_any and method == ANY_METHOD:
            continue
        if not isinstance(method, str) or method.upper() not in HTTP_METHODS:
            errors.append(
                f"{where}: unknown HTTP method {method!r} (allowed: {', '.join(HTTP_METHODS)})"
            )
    return errors


def _operations_errors(where: str, value: Any, allow_empty: bool) -> list[str]:
    if not isinstance(value, list):
        return [f"{where}: must be a list of operations"]
    if not value and not allow_empty:
        return [
            f"{where}: must not be empty; omit the key to allow every operation "
            "within allowed_methods"
        ]
    errors: list[str] = []
    for index, entry in enumerate(value):
        at = f"{where}[{index}]"
        if not isinstance(entry, Mapping):
            errors.append(f"{at}: must be a mapping with operationId and/or path")
            continue
        for key in sorted(set(entry) - set(_OPERATION_KEYS), key=str):
            errors.append(f"{at}: unknown key {key!r}")
        if "operationId" not in entry and "path" not in entry:
            errors.append(f"{at}: needs operationId and/or path")
        if "operationId" in entry:
            op_id = entry["operationId"]
            if not isinstance(op_id, str) or not op_id or any(c.isspace() for c in op_id):
                errors.append(f"{at}.operationId: must be a non-empty string without spaces")
        if "path" in entry:
            problem = path_template_problem(entry["path"])
            if problem:
                errors.append(f"{at}.path: {problem}")
        if "methods" in entry:
            errors.extend(_methods_errors(f"{at}.methods", entry["methods"], False))
    return errors


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _timeouts_errors(where: str, value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return [f"{where}: must be a mapping with connect and/or read (milliseconds)"]
    errors = [
        f"{where}: unknown key {key!r}" for key in sorted(set(value) - set(_TIMEOUT_KEYS), key=str)
    ]
    for key in _TIMEOUT_KEYS:
        if key in value and not _is_positive_int(value[key]):
            errors.append(f"{where}.{key}: must be a positive integer (milliseconds)")
    return errors


def _pagination_errors(where: str, value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return [f"{where}: must be a mapping with page_size_param and max_page_size"]
    errors = [
        f"{where}: unknown key {key!r}"
        for key in sorted(set(value) - set(_PAGINATION_KEYS), key=str)
    ]
    param = value.get("page_size_param")
    if "page_size_param" not in value:
        errors.append(f"{where}.page_size_param: required")
    elif not (isinstance(param, str) and param.strip()):
        errors.append(f"{where}.page_size_param: must be a non-empty string")
    if "max_page_size" not in value:
        errors.append(f"{where}.max_page_size: required")
    elif not _is_positive_int(value["max_page_size"]):
        errors.append(f"{where}.max_page_size: must be a positive integer")
    return errors


def path_template_problem(path: Any) -> str | None:
    """Why ``path`` is not a valid path template, or None.

    A template starts with ``/``; each segment holds literal characters and
    ``{name}`` placeholders only (no query, fragment, spaces, empty, ``.`` or
    ``..`` segments). One trailing slash is allowed.
    """
    if not isinstance(path, str) or not path.startswith("/"):
        return "must be a string starting with /"
    body = path[1:]
    if body.endswith("/"):
        body = body[:-1]
    if not body:
        return None
    for segment in body.split("/"):
        if segment in ("", ".", ".."):
            return "must not contain empty, '.' or '..' segments"
        if not _PATH_SEGMENT_RE.match(segment):
            return (
                "segments may hold literal characters and {name} placeholders only "
                "(no query, fragment or whitespace)"
            )
    return None


def path_matches(template: str, path: str) -> bool:
    """Whether ``path`` is covered by ``template``.

    A ``{name}`` placeholder matches exactly one non-empty segment, so
    ``/items/{item_id}`` covers ``/items/42``, ``/items/{id}`` and itself.
    """
    if template == path:
        return True
    pattern = "".join(
        "[^/]+" if part.startswith("{") and part.endswith("}") else re.escape(part)
        for part in _PLACEHOLDER_SPLIT_RE.split(template)
    )
    return re.fullmatch(pattern, path) is not None


def operation_matches(
    entry: Mapping[str, Any], method: str, operation_id: str | None, path: str | None
) -> bool:
    """Whether an allowed/denied operation entry covers the call.

    AND semantics: every field the entry pins (``operationId``, ``path``,
    ``methods``) must match. A call that does not name a pinned field (no
    operation id, or no path) does not match that entry.
    """
    methods = entry.get("methods")
    if methods and method.upper() not in {str(m).upper() for m in methods}:
        return False
    pinned_id = entry.get("operationId")
    if pinned_id is not None and operation_id != pinned_id:
        return False
    pinned_path = entry.get("path")
    if pinned_path is not None and (path is None or not path_matches(pinned_path, path)):
        return False
    return pinned_id is not None or pinned_path is not None


def describe_operation(entry: Mapping[str, Any]) -> str:
    """``operationId=getItem path=/items/{item_id} methods=[GET]`` for messages."""
    parts = []
    if entry.get("operationId") is not None:
        parts.append(f"operationId={entry['operationId']}")
    if entry.get("path") is not None:
        parts.append(f"path={entry['path']}")
    if entry.get("methods"):
        parts.append(f"methods={sorted(str(m).upper() for m in entry['methods'])}")
    return " ".join(parts)


def refusal_reason(
    api: Mapping[str, Any],
    method: str,
    operation_id: str | None = None,
    path: str | None = None,
) -> str | None:
    """Why the API's policy refuses the call, or None when it is allowed.

    Every rule must pass: the method is in ``allowed_methods`` (``["*"]``
    allows every method); no ``denied_operations`` entry matches (denials
    win); and, when ``allowed_operations`` is present, one of its entries
    matches.
    """
    method = method.upper()
    allowed = [str(m).upper() for m in api.get("allowed_methods") or []]
    if ANY_METHOD not in allowed and method not in allowed:
        return f"method {method} is not in allowed_methods {allowed}"
    for entry in api.get("denied_operations") or []:
        if operation_matches(entry, method, operation_id, path):
            return f"denied by denied_operations ({describe_operation(entry)})"
    allowed_operations = api.get("allowed_operations")
    if allowed_operations is not None and not any(
        operation_matches(entry, method, operation_id, path) for entry in allowed_operations
    ):
        return "not in allowed_operations"
    return None


# --- END SHARED API POLICY RULES ---


class ApiPolicyError(Exception):
    """Refused by the policy: no or invalid policy file, an undeclared API, or a
    request outside the policy. Always raised before anything is sent."""

    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.errors = list(errors or [])


class ApiCallError(Exception):
    """A declared call that could not be made or failed: missing configuration or
    credential, a transport error, or a non-2xx response."""


# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------


@dataclass
class ApiPolicy:
    """A validated `api-policy.yaml`: API name -> its settings."""

    apis: dict[str, dict[str, Any]]
    source: Path | None = None

    @property
    def file(self) -> str:
        return self.source.name if self.source else POLICY_FILENAME

    @classmethod
    def from_dict(cls, data: Any, source: Path | None = None) -> ApiPolicy:
        """Validate `data` with the strict schema; raise `ApiPolicyError` listing every error."""
        errors = policy_errors(data)
        if errors:
            name = source.name if source else POLICY_FILENAME
            raise ApiPolicyError(f"invalid {name}: " + "; ".join(errors), errors)
        return cls(apis={str(k): dict(v) for k, v in data["apis"].items()}, source=source)

    @classmethod
    def load(cls, path: str | Path) -> ApiPolicy:
        policy_path = Path(path)
        if not policy_path.is_file():
            raise ApiPolicyError(
                f"{policy_path} not found: outbound API calls are refused until the project "
                f"declares them in {POLICY_FILENAME} (set {POLICY_PATH_ENV} to use another path)."
            )
        try:
            data = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ApiPolicyError(f"cannot read {policy_path}: {exc}") from exc
        return cls.from_dict(data, source=policy_path)

    def api(self, name: str) -> dict[str, Any]:
        """The settings of API `name`; `ApiPolicyError` when it is not declared."""
        settings = self.apis.get(name)
        if settings is None:
            declared = ", ".join(sorted(self.apis)) or "none"
            raise ApiPolicyError(
                f"API {name!r} is not declared in {self.file} (declared: {declared})."
            )
        return settings

    def check(
        self,
        api_name: str,
        method: str,
        operation_id: str | None = None,
        path: str | None = None,
    ) -> None:
        """Raise `ApiPolicyError` when the call is outside the API's policy."""
        reason = refusal_reason(self.api(api_name), method, operation_id, path)
        if reason:
            what = operation_id or path or "<unnamed operation>"
            raise ApiPolicyError(
                f"{api_name}: {method.upper()} {what} refused: {reason} ({self.file})."
            )


def resolve_policy_path() -> Path:
    """`API_POLICY_PATH` when set; else `./api-policy.yaml`, else the one beside `pyproject.toml`."""
    configured = os.environ.get(POLICY_PATH_ENV, "").strip()
    if configured:
        return Path(configured)
    local = Path(POLICY_FILENAME)
    if local.is_file():
        return local
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return parent / POLICY_FILENAME
    return local


_cache: dict[tuple[str, int], ApiPolicy] = {}


def load_policy() -> ApiPolicy:
    """The project's policy (cached until the file changes); `ApiPolicyError` when absent or invalid."""
    path = resolve_policy_path()
    try:
        key = (str(path.resolve()), path.stat().st_mtime_ns)
    except OSError:
        return ApiPolicy.load(path)  # raises the not-found error
    policy = _cache.get(key)
    if policy is None:
        policy = ApiPolicy.load(path)
        _cache.clear()
        _cache[key] = policy
    return policy


def reset_policy_cache() -> None:
    """For tests that switch `API_POLICY_PATH` or rewrite the file."""
    _cache.clear()


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_PLACEHOLDER = re.compile(r"\{([^/{}]+)\}")


def render_path(template: str, path_params: Mapping[str, Any]) -> str:
    """Fill `{name}` placeholders with percent-encoded values; refuse anything that changes the shape.

    A value is one opaque path segment: an empty, `.` or `..` value (which
    would step out of the template) and a value containing `/` or `\\` (a
    server decoding `%2F` would traverse) are refused; other reserved
    characters are percent-encoded. Unknown or unfilled parameters are refused too.
    """
    names = _PLACEHOLDER.findall(template)
    unknown = sorted(set(path_params) - set(names))
    if unknown:
        raise ApiPolicyError(f"path {template!r} has no parameter(s) {', '.join(unknown)}.")
    missing = [n for n in names if n not in path_params]
    if missing:
        raise ApiPolicyError(f"path {template!r} needs value(s) for {', '.join(missing)}.")

    def _fill(match: re.Match[str]) -> str:
        name = match.group(1)
        value = str(path_params[name])
        if value in ("", ".", "..") or value.strip() != value or "/" in value or "\\" in value:
            raise ApiPolicyError(
                f"path parameter {name}={value!r} is not a valid path segment (refused before sending)."
            )
        return quote(value, safe="")

    return _PLACEHOLDER.sub(_fill, template)


def validate_concrete_path(path: str) -> None:
    """Refuse a path that httpx would normalise or a server would resolve elsewhere.

    Dot segments (`.`/`..`, also percent-encoded), an encoded slash or
    backslash inside a segment, empty segments (`//`), and a query or fragment
    in the path (send them through `params=`) are refused: the policy check
    would otherwise pass a template while the wire path lands on another
    endpoint (for example `/items/1/../../admin` -> `/admin`).
    """
    if not path.startswith("/"):
        raise ApiPolicyError(f"path {path!r} must start with /.")
    if "?" in path or "#" in path:
        raise ApiPolicyError(f"path {path!r} must not carry a query or fragment; use params=.")
    body = path[1:]
    if body.endswith("/"):
        body = body[:-1]
    if not body:
        return
    for segment in body.split("/"):
        decoded = unquote(segment)
        if segment == "":
            raise ApiPolicyError(f"path {path!r} contains an empty segment (refused).")
        if decoded in (".", ".."):
            raise ApiPolicyError(f"path {path!r} contains a dot segment (refused).")
        if "/" in decoded or "\\" in decoded:
            raise ApiPolicyError(f"path {path!r} contains an encoded slash (refused).")


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


class ApiClient:
    """Policy-enforcing async HTTP client for one declared API."""

    def __init__(
        self,
        policy: ApiPolicy,
        name: str,
        *,
        credential: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.policy = policy
        self.name = name
        self.settings = policy.api(name)
        self._credential = credential
        self._transport = transport

    # -- configuration ------------------------------------------------------

    def base_url(self) -> httpx.URL:
        env = self.settings["base_url_env"]
        raw = os.environ.get(env, "").strip()
        if not raw:
            raise ApiCallError(f"{env} is not set; cannot reach API {self.name!r}.")
        try:
            url = httpx.URL(raw)
        except httpx.InvalidURL as exc:
            raise ApiCallError(f"{env} is not a valid URL: {exc}") from exc
        if url.scheme not in ("http", "https") or not url.host:
            raise ApiCallError(f"{env} must be an absolute http(s) URL.")
        if url.query or url.fragment or url.userinfo:
            raise ApiCallError(f"{env} must not carry credentials, a query or a fragment.")
        return url

    def auth_headers(self) -> dict[str, str]:
        mode = self.settings["auth"]
        if mode == "bearer":
            env = self.settings["token_env"]
            token = os.environ.get(env, "")
            if not token:
                raise ApiCallError(
                    f"{env} is not set (API {self.name!r} uses auth: bearer); nothing was sent."
                )
            return {"Authorization": f"Bearer {token}"}
        if mode == "forward":
            if not self._credential:
                raise ApiCallError(
                    f"the caller has no credential for API {self.name!r} "
                    f"(principal.attributes['credentials'][{self.name!r}]); nothing was sent."
                )
            header = self.settings.get("forward_header") or DEFAULT_FORWARD_HEADER
            return {header: self._credential}
        return {}

    def timeout(self) -> httpx.Timeout:
        timeouts = dict(DEFAULT_TIMEOUTS_MS)
        timeouts.update(self.settings.get("timeouts_ms") or {})
        return httpx.Timeout(
            connect=timeouts["connect"] / 1000,
            read=timeouts["read"] / 1000,
            write=10.0,
            pool=10.0,
        )

    def _check_page_size(self, params: Mapping[str, Any] | None) -> None:
        pagination = self.settings.get("pagination")
        if not pagination or not params or pagination["page_size_param"] not in params:
            return
        param = pagination["page_size_param"]
        cap = pagination["max_page_size"]
        value = params[param]
        try:
            size = int(str(value))
        except ValueError:
            size = 0
        if size < 1 or size > cap:
            raise ApiPolicyError(
                f"{self.name}: {param}={value!r} refused: page size must be 1-{cap} "
                f"(pagination.max_page_size in {self.policy.file})."
            )

    # -- requests -------------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        operation_id: str | None = None,
        path_params: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """Send `method` on `path` after the policy check; return the JSON body or the text.

        Pass `path` as the template declared in `API_CALLS` (for example
        `/items/{item_id}`) with the values in `path_params`: the policy is
        checked against the template and against the rendered path, and the
        client encodes each value as one segment, so model-chosen input cannot
        change which endpoint is hit. A concrete `path` is accepted too but
        validated (no dot segments, encoded slashes, empty segments, query or
        fragment). The API's base URL may carry a path prefix
        (`https://host/v2`); `path` is joined under it. Redirects are never
        followed. Raises `ApiPolicyError` (nothing sent) or `ApiCallError`.
        """
        method = method.upper()
        if method not in HTTP_METHODS:
            raise ApiPolicyError(f"{self.name}: unknown HTTP method {method!r}.")
        if path_params is not None:
            self.policy.check(self.name, method, operation_id, path)
            wire_path = render_path(path, path_params)
        elif _PLACEHOLDER.search(path):
            raise ApiPolicyError(f"path {path!r} has unfilled parameters; pass path_params=.")
        else:
            wire_path = path
        validate_concrete_path(wire_path)
        self.policy.check(self.name, method, operation_id, wire_path)
        self._check_page_size(params)

        base = self.base_url()
        prefix = base.raw_path.decode("ascii").split("?", 1)[0].rstrip("/")
        expected = f"{prefix}/{wire_path.lstrip('/')}"
        url = httpx.URL(f"{str(base).rstrip('/')}/{wire_path.lstrip('/')}")
        if (
            url.raw_path.decode("ascii").split("?", 1)[0] != expected
            or url.host != base.host
            or url.port != base.port
            or url.scheme != base.scheme
        ):
            raise ApiPolicyError(f"path {wire_path!r} would be rewritten before sending (refused).")

        request_headers = httpx.Headers(headers or {})
        for name, value in self.auth_headers().items():
            request_headers[name] = value  # the policy's credential always wins

        async with httpx.AsyncClient(
            transport=self._transport, timeout=self.timeout(), follow_redirects=False
        ) as client:
            try:
                response = await client.request(
                    method, url, params=params, json=json_body, headers=request_headers
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise ApiCallError(
                    f"{self.name}: {method} {wire_path} -> HTTP {exc.response.status_code}"
                ) from exc
            except httpx.HTTPError as exc:
                raise ApiCallError(
                    f"{self.name}: {method} {wire_path} failed: {type(exc).__name__}"
                ) from exc
        if "json" in response.headers.get("content-type", ""):
            return response.json()
        return response.text

    async def get(self, path: str, **kwargs: Any) -> Any:
        return await self.request("GET", path, **kwargs)


# ---------------------------------------------------------------------------
# Entry point for tools
# ---------------------------------------------------------------------------


def current_context() -> Any:
    """The run context LangGraph set for the current graph run (who is calling), or None."""
    try:
        from langgraph.runtime import get_runtime

        return get_runtime().context
    except Exception:  # outside a graph run
        return None


def forwarded_credential(api_name: str, context: Any) -> str | None:
    """`attributes["credentials"][api_name]` of the calling principal, or None."""
    attributes = getattr(context, "attributes", None)
    if attributes is None and isinstance(context, Mapping):
        attributes = context.get("attributes")
    credentials = attributes.get("credentials") if isinstance(attributes, Mapping) else None
    value = credentials.get(api_name) if isinstance(credentials, Mapping) else None
    return value if isinstance(value, str) and value else None


def get_client(
    api_name: str,
    *,
    context: Any = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ApiClient:
    """A policy-enforcing client for API `api_name` of `api-policy.yaml`.

    `context` is the run context holding the calling principal (a tool's
    `runtime.context`); by default it is read from the current graph run.
    Raises `ApiPolicyError` when the policy file is missing or invalid, or does
    not declare `api_name`.
    """
    policy = load_policy()
    settings = policy.api(api_name)
    credential = None
    if settings["auth"] == "forward":
        credential = forwarded_credential(
            api_name, context if context is not None else current_context()
        )
    return ApiClient(policy, api_name, credential=credential, transport=transport)
