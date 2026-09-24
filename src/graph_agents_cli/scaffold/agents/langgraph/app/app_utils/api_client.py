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

Every method the policy allows can be sent (`request()`, or `get`, `head`,
`post`, `put`, `patch`, `delete`, `options`), with a JSON body, query
parameters and extra headers. An API's optional `limits` cap the calls before
they are sent: `max_calls_per_run` counts the calls to that API within one
agent run (the run id of the LangGraph run, else the request's; calls made
outside any run share one count), and `rate_per_minute` is a token bucket per
process, so each replica allows that rate. Counters are dropped when a `/chat`
or A2A run ends (`end_run`), and otherwise (LangGraph Server runs included)
after `RUN_COUNTER_TTL_S` without a call or beyond `MAX_TRACKED_RUNS` runs, so
memory does not grow across runs.

An API's optional `approval` block names the calls a human must approve before
they are sent (`gated`, `ApiPolicy.gate`). Approval never widens access: a
gated call must pass the policy first. This client refuses a gated call
before sending it (fail closed), since it cannot pause the run for a decision.

Every tool module declares `API_CALLS`, a module-level list of
`{"api", "method", "operation_id", "path"}` dicts naming each call it makes;
`graph-agents-cli lint` checks those declarations against the same rules.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import httpx
import yaml

logger = logging.getLogger(__name__)
# This module logs each outbound call itself (API, method, operation, status);
# the HTTP client's own INFO lines would add the full URL with its values.
# Here as well as in the logging setup (`telemetry.QUIET_LOGGERS`, the same
# names): under langgraph-server the graph can run in a process that never
# loads the app.
for _name in ("httpx", "httpcore", "httpx2", "httpcore2"):
    logging.getLogger(_name).setLevel(logging.WARNING)

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

_ESCAPE_RE = re.compile(r"%[0-9A-Fa-f]{2}")
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")

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
    "limits",
    "approval",
)
_OPERATION_KEYS = ("operationId", "path", "methods")
_TIMEOUT_KEYS = ("connect", "read")
_PAGINATION_KEYS = ("page_size_param", "max_page_size")
_LIMIT_KEYS = ("max_calls_per_run", "rate_per_minute")
_APPROVAL_KEYS = ("required_for", "approvers", "timeout_s")
_REQUIRED_FOR_KEYS = ("methods", "operations")

# An API's `approval` block names the calls a human must approve before they are
# sent (`required_for`), who may approve them (`approvers`) and how long a
# pending approval waits before it expires, which rejects the call
# (`timeout_s`). It never widens access: a gated call must still be allowed,
# and denials still win. It belongs to the API only: on an operation entry the
# key is refused, with a pointer to `approval.required_for.operations`.
APPROVAL_KEY = "approval"
REQUESTER_APPROVER = "requester"  # the principal who started the run confirms
ROLE_APPROVER_PREFIX = "role:"  # any principal holding the role decides
DEFAULT_APPROVAL_TIMEOUT_S = 900
MIN_APPROVAL_TIMEOUT_S = 30
MAX_APPROVAL_TIMEOUT_S = 86400
_ROLE_NAME_RE = re.compile(r"[^\s,\x00-\x1f\x7f]{1,256}")

LEGACY_POLICY_HINT = (
    "product_api: is the retired single-API format: move its fields under "
    "apis: <name>: (for example apis: example:), add the now required "
    "allowed_methods (for example [GET]), write auth: forward instead of "
    "forwarded-session, and name the file api-policy.yaml"
)


class PolicyLoader(yaml.SafeLoader):
    """``yaml.SafeLoader`` that refuses a key repeated within one mapping, at any level.

    Plain ``safe_load`` silently keeps the last duplicate, so a reviewer reading
    ``allowed_methods: [GET, POST]`` would miss a later ``allowed_methods: ["*"]``
    that is the one applied. Merge keys (``<<: *anchor``) still work.
    """

    def construct_mapping(self, node: Any, deep: bool = False) -> Any:
        if isinstance(node, yaml.MappingNode):
            seen: set[Any] = set()
            for key_node, _value_node in node.value:
                if key_node.tag == "tag:yaml.org,2002:merge":
                    continue
                key = self.construct_object(key_node, deep=deep)
                try:
                    duplicate = key in seen
                except TypeError:  # an unhashable key: the base loader reports it
                    continue
                if duplicate:
                    raise yaml.constructor.ConstructorError(
                        "while constructing a mapping",
                        node.start_mark,
                        f"found duplicate key {key!r}",
                        key_node.start_mark,
                    )
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


def parse_policy_yaml(text: str) -> tuple[Any, list[str]]:
    """Parse api-policy.yaml text: ``(data, [])``, or ``(None, [error])`` when it is
    not valid YAML (a duplicate key included)."""
    try:
        return yaml.load(text, Loader=PolicyLoader), []
    except yaml.YAMLError as exc:
        return None, [f"not valid YAML: {exc}"]


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
            _operations_errors(
                f"{where}.allowed_operations",
                api["allowed_operations"],
                where,
                "must not be empty; omit the key to allow every operation within allowed_methods",
            )
        )
    if "denied_operations" in api:
        errors.extend(
            _operations_errors(f"{where}.denied_operations", api["denied_operations"], where)
        )

    if "openapi" in api:
        openapi = api["openapi"]
        if not (isinstance(openapi, str) and openapi.strip()):
            errors.append(f"{where}.openapi: must be a file path")

    if "timeouts_ms" in api:
        errors.extend(_timeouts_errors(f"{where}.timeouts_ms", api["timeouts_ms"]))
    if "pagination" in api:
        errors.extend(_pagination_errors(f"{where}.pagination", api["pagination"]))
    if "limits" in api:
        errors.extend(_limits_errors(f"{where}.limits", api["limits"]))
    if APPROVAL_KEY in api:
        errors.extend(_approval_errors(where, api[APPROVAL_KEY]))
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


def _operations_errors(
    where: str, value: Any, api_where: str, empty_error: str | None = None
) -> list[str]:
    """Errors of a list of operation entries; ``empty_error`` refuses an empty list."""
    if not isinstance(value, list):
        return [f"{where}: must be a list of operations"]
    if not value and empty_error:
        return [f"{where}: {empty_error}"]
    errors: list[str] = []
    for index, entry in enumerate(value):
        at = f"{where}[{index}]"
        if not isinstance(entry, Mapping):
            errors.append(f"{at}: must be a mapping with operationId and/or path")
            continue
        for key in sorted(set(entry) - set(_OPERATION_KEYS) - {APPROVAL_KEY}, key=str):
            errors.append(f"{at}: unknown key {key!r}")
        if APPROVAL_KEY in entry:
            errors.append(
                f"{at}.{APPROVAL_KEY}: not valid on an operation entry; gate the operation "
                f"with {api_where}.{APPROVAL_KEY}.required_for.operations"
            )
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


def _limits_errors(where: str, value: Any) -> list[str]:
    if not isinstance(value, Mapping) or not value:
        return [f"{where}: must be a mapping with max_calls_per_run and/or rate_per_minute"]
    errors = [
        f"{where}: unknown key {key!r}" for key in sorted(set(value) - set(_LIMIT_KEYS), key=str)
    ]
    for key in _LIMIT_KEYS:
        if key in value and not _is_positive_int(value[key]):
            errors.append(f"{where}.{key}: must be an integer >= 1")
    return errors


def _approval_errors(api_where: str, value: Any) -> list[str]:
    where = f"{api_where}.{APPROVAL_KEY}"
    if not isinstance(value, Mapping):
        return [f"{where}: must be a mapping with required_for and approvers"]
    errors = [
        f"{where}: unknown key {key!r}" for key in sorted(set(value) - set(_APPROVAL_KEYS), key=str)
    ]
    if "required_for" not in value:
        errors.append(f"{where}.required_for: required (the methods and/or operations it gates)")
    else:
        errors.extend(
            _required_for_errors(f"{where}.required_for", value["required_for"], api_where)
        )
    if "approvers" not in value:
        errors.append(f'{where}.approvers: required (a list of "requester" and/or "role:<name>")')
    else:
        errors.extend(_approvers_errors(f"{where}.approvers", value["approvers"]))
    if "timeout_s" in value:
        timeout = value["timeout_s"]
        if not (
            isinstance(timeout, int)
            and not isinstance(timeout, bool)
            and MIN_APPROVAL_TIMEOUT_S <= timeout <= MAX_APPROVAL_TIMEOUT_S
        ):
            errors.append(
                f"{where}.timeout_s: must be an integer from {MIN_APPROVAL_TIMEOUT_S} to "
                f"{MAX_APPROVAL_TIMEOUT_S} (seconds)"
            )
    return errors


def _required_for_errors(where: str, value: Any, api_where: str) -> list[str]:
    if not isinstance(value, Mapping) or not value:
        return [f"{where}: must be a mapping with methods and/or operations"]
    errors = [
        f"{where}: unknown key {key!r}"
        for key in sorted(set(value) - set(_REQUIRED_FOR_KEYS), key=str)
    ]
    if "methods" not in value and "operations" not in value:
        errors.append(f"{where}: needs methods and/or operations")
    if "methods" in value:
        errors.extend(_methods_errors(f"{where}.methods", value["methods"], True))
    if "operations" in value:
        errors.extend(
            _operations_errors(
                f"{where}.operations",
                value["operations"],
                api_where,
                "must not be empty; omit the key when no operation needs approval",
            )
        )
    return errors


def _approvers_errors(where: str, value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        return [f'{where}: must be a non-empty list of "requester" and/or "role:<name>"']
    errors: list[str] = []
    for index, approver in enumerate(value):
        if approver == REQUESTER_APPROVER:
            continue
        if (
            isinstance(approver, str)
            and approver.startswith(ROLE_APPROVER_PREFIX)
            and _ROLE_NAME_RE.fullmatch(approver[len(ROLE_APPROVER_PREFIX) :])
        ):
            continue
        errors.append(
            f'{where}[{index}]: {approver!r} is not an approver ("requester", or "role:<name>" '
            "with a role name of 1-256 characters without spaces or commas)"
        )
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


def normalize_path(path: str) -> str:
    """``path`` in the form policy paths are compared in.

    Percent-encoded unreserved characters are decoded (``/%61dmin`` is
    ``/admin``), other escapes are upper-cased (``%2f`` is ``%2F``), and one
    trailing slash is dropped (``/items/1/`` is ``/items/1``), so equivalent
    spellings of a path match the same entries.
    """

    def _escape(match: re.Match[str]) -> str:
        char = chr(int(match.group(0)[1:], 16))
        return char if char in _UNRESERVED else match.group(0).upper()

    path = _ESCAPE_RE.sub(_escape, path)
    return path[:-1] if len(path) > 1 and path.endswith("/") else path


def path_matches(template: str, path: str, *, ignore_case: bool = False) -> bool:
    """Whether ``path`` is covered by ``template``.

    A ``{name}`` placeholder matches exactly one non-empty segment, so
    ``/items/{item_id}`` covers ``/items/42``, ``/items/{id}`` and itself.
    Both sides are compared normalised (``normalize_path``). Letter case
    counts unless ``ignore_case``: denials ignore it, so ``/ADMIN/1`` cannot
    slip past a denial of ``/admin/{x}`` on a case-insensitive server.
    """
    template, path = normalize_path(template), normalize_path(path)
    if template == path or (ignore_case and template.casefold() == path.casefold()):
        return True
    pattern = "".join(
        "[^/]+" if part.startswith("{") and part.endswith("}") else re.escape(part)
        for part in _PLACEHOLDER_SPLIT_RE.split(template)
    )
    return re.fullmatch(pattern, path, re.IGNORECASE if ignore_case else 0) is not None


def _methods_match(entry: Mapping[str, Any], method: str) -> bool:
    methods = entry.get("methods")
    return not methods or method.upper() in {str(m).upper() for m in methods}


def operation_matches(
    entry: Mapping[str, Any], method: str, operation_id: str | None, path: str | None
) -> bool:
    """Whether an ``allowed_operations`` entry covers the call.

    AND semantics: every field the entry pins (``operationId``, ``path``,
    ``methods``) must match. A call that does not name a pinned field (no
    operation id, or no path) does not match: an allow must be shown.
    """
    if not _methods_match(entry, method):
        return False
    pinned_id = entry.get("operationId")
    if pinned_id is not None and operation_id != pinned_id:
        return False
    pinned_path = entry.get("path")
    if pinned_path is not None and (path is None or not path_matches(pinned_path, path)):
        return False
    return pinned_id is not None or pinned_path is not None


def denial_match(
    entry: Mapping[str, Any], method: str, operation_id: str | None, path: str | None
) -> str | None:
    """How a ``denied_operations`` entry covers the call: None when it does not.

    A denial names an endpoint and must hold whatever label a call gives it,
    so, unlike an allow, the fields it pins are alternatives, not
    requirements. With its ``methods`` (when pinned) covering the call's
    method, it covers a call whose path its ``path`` covers, whatever
    operation id the call names, and a call that names its ``operationId``
    (both return ``""``). Failing closed, it also covers a call that leaves
    out what the denial knows the operation by: no path when it pins
    ``path`` (returns ``"path"``), no operation id when it pins
    ``operationId`` alone (returns ``"operation_id"``). A denial by
    ``operationId`` alone knows only that label: pin ``path`` too so it holds
    on the wire. Operation ids and paths are compared ignoring letter case.
    """
    if not _methods_match(entry, method):
        return None
    pinned_id = entry.get("operationId")
    pinned_path = entry.get("path")
    if (
        pinned_path is not None
        and path is not None
        and path_matches(pinned_path, path, ignore_case=True)
    ):
        return ""
    if (
        pinned_id is not None
        and operation_id is not None
        and str(operation_id).casefold() == str(pinned_id).casefold()
    ):
        return ""
    if pinned_path is not None and path is None:
        return "path"
    if pinned_id is not None and pinned_path is None and operation_id is None:
        return "operation_id"
    return None


def denial_matches(
    entry: Mapping[str, Any], method: str, operation_id: str | None, path: str | None
) -> bool:
    """Whether a ``denied_operations`` entry covers the call (see ``denial_match``)."""
    return denial_match(entry, method, operation_id, path) is not None


def describe_operation(entry: Mapping[str, Any]) -> str:
    """``operationId=updateOrder path=/orders/{order_id} methods=['PATCH']`` for messages."""
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
    allows every method); no ``denied_operations`` entry may cover the call
    (denials win: a denial pinning a path refuses every call to that path,
    whatever operation id it names, and a call that leaves out what a denial
    knows the operation by is refused by it: ``denial_match``); and, when
    ``allowed_operations`` is present, one of its entries matches
    (``operation_matches``: every field it pins).
    """
    method = method.upper()
    operation_id = operation_id or None
    path = path or None
    allowed = [str(m).upper() for m in api.get("allowed_methods") or []]
    if ANY_METHOD not in allowed and method not in allowed:
        return f"method {method} is not in allowed_methods {allowed}"
    for entry in api.get("denied_operations") or []:
        unnamed = denial_match(entry, method, operation_id, path)
        if unnamed is not None:
            reason = f"denied by denied_operations ({describe_operation(entry)})"
            if unnamed:
                reason += (
                    f": the call names no {unnamed}, so it cannot be ruled out; name it on "
                    "the call and in API_CALLS"
                )
            return reason
    allowed_operations = api.get("allowed_operations")
    if allowed_operations is not None and not any(
        operation_matches(entry, method, operation_id, path) for entry in allowed_operations
    ):
        return "not in allowed_operations"
    return None


@dataclass(frozen=True)
class ApprovalGate:
    """The human approval an API's policy requires before a call is sent (``gated``)."""

    # "requester" and/or "role:<name>" entries, in the policy's order.
    approvers: tuple[str, ...]
    # Seconds a pending approval waits for a decision; then it expires (= rejected).
    timeout_s: int
    # The approval.required_for rule that gates the call, for messages.
    rule: str


def gated(
    api: Mapping[str, Any],
    method: str,
    operation_id: str | None = None,
    path: str | None = None,
) -> ApprovalGate | None:
    """The approval the API's policy (a validated one) requires before the call, or None.

    Ask it only about a call ``refusal_reason`` allows: approval never widens
    access, so a refused call stays refused whatever its gate, and denials
    still win. The call is gated when ``approval.required_for.methods`` holds
    its method (``["*"]``: every method), or when an entry of
    ``approval.required_for.operations`` covers it. Such an entry fails
    closed, as a denial does (``denial_match``), not as an allow: with its
    ``methods`` (when pinned) covering the call's method, its ``path`` gates
    every call to that path whatever operation id the call names, its
    ``operationId`` gates the calls that name it, and a call that leaves out
    what the entry knows the operation by is gated too. Paths are compared
    normalised and ignoring letter case. At runtime, ask with the path that is
    sent (and with the template too, when there is one: gated if either is).
    """
    approval = api.get(APPROVAL_KEY)
    if approval is None:
        return None
    required_for = approval.get("required_for") or {}
    method = method.upper()
    operation_id = operation_id or None
    path = path or None
    rule = None
    methods = [str(m).upper() for m in required_for.get("methods") or []]
    if ANY_METHOD in methods or method in methods:
        rule = f"approval.required_for.methods {methods}"
    else:
        for entry in required_for.get("operations") or []:
            unnamed = denial_match(entry, method, operation_id, path)
            if unnamed is None:
                continue
            rule = f"approval.required_for.operations ({describe_operation(entry)})"
            if unnamed:
                rule += f": the call names no {unnamed}, so it cannot be ruled out"
            break
    if rule is None:
        return None
    return ApprovalGate(
        approvers=tuple(str(a) for a in approval.get("approvers") or ()),
        timeout_s=int(approval.get("timeout_s", DEFAULT_APPROVAL_TIMEOUT_S)),
        rule=rule,
    )


# --- END SHARED API POLICY RULES ---


class ApiPolicyError(Exception):
    """Refused by the policy: no or invalid policy file, an undeclared API, or a
    request outside the policy. Always raised before anything is sent."""

    def __init__(
        self, message: str, errors: list[str] | None = None, *, reason: str | None = None
    ) -> None:
        super().__init__(message)
        self.errors = list(errors or [])
        # The policy rule that refused a call (policy-derived text only, never the
        # call's arguments), for the log line; None for other refusals.
        self.reason = reason


# How much of an error response an `ApiCallError` keeps (`body`) and puts in
# its message (the part the model reads).
ERROR_BODY_MAX_CHARS = 2000
ERROR_MESSAGE_BODY_CHARS = 300
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ApiCallError(Exception):
    """A declared call that could not be made or failed: missing configuration or
    credential, a transport error, or a non-2xx response.

    For a non-2xx response, `status_code` is the HTTP status and `body` the
    start of the response body (at most `ERROR_BODY_MAX_CHARS` characters,
    with the credential the call sent replaced by `<redacted>`), so a tool can
    branch on the status (a 404 as "not found", a 409 as a conflict) and the
    model reads the upstream's reason from the message. Both are None when no
    response came back. The body is the upstream's text, as untrusted as any
    other API data.
    """

    def __init__(
        self, message: str, *, status_code: int | None = None, body: str | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def error_body_excerpt(text: str, secrets: tuple[str, ...] = ()) -> str:
    """`text` bounded to `ERROR_BODY_MAX_CHARS`, control characters dropped, `secrets` redacted."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    text = _CONTROL_CHARS.sub("", text)
    return text[:ERROR_BODY_MAX_CHARS]


# Request headers a tool may not set: they would change the request's routing
# or method after the policy check (`Host`, `X-HTTP-Method-Override` and the
# like), or belong to the connection rather than the request (hop-by-hop).
# They are dropped before sending, with a warning naming them.
FORBIDDEN_HEADERS = frozenset(
    {
        "host",
        "x-http-method-override",
        "x-http-method",
        "x-method-override",
        "x-original-url",
        "x-rewrite-url",
        "x-original-method",
        "forwarded",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
    }
)
FORBIDDEN_HEADER_PREFIXES = ("x-forwarded-",)
# A `_method` query parameter or top-level JSON body key turns a POST into
# another method on servers that honour it (Rails, Laravel, method-override
# middleware); the policy checks the request's own method, so it is refused.
METHOD_OVERRIDE_PARAM = "_method"


def forbidden_header(name: str) -> bool:
    """Whether a tool may not set header `name`.

    Underscores count as hyphens: CGI and WSGI servers (and proxies that do
    not drop such headers) map `X_HTTP_METHOD_OVERRIDE` and
    `X-HTTP-Method-Override` to the same variable.
    """
    lowered = name.strip().lower().replace("_", "-")
    return lowered in FORBIDDEN_HEADERS or lowered.startswith(FORBIDDEN_HEADER_PREFIXES)


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
            text = policy_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ApiPolicyError(f"cannot read {policy_path}: {exc}") from exc
        data, errors = parse_policy_yaml(text)
        if errors:
            raise ApiPolicyError(f"invalid {policy_path.name}: " + "; ".join(errors), errors)
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
        """Raise `ApiPolicyError` when the call is outside the API's policy.

        The message is what the model reads (a tool error): the API, the
        call and the rule that refused it, without file names.
        """
        reason = refusal_reason(self.api(api_name), method, operation_id, path)
        if reason:
            what = operation_id or path or "<unnamed operation>"
            raise ApiPolicyError(
                f"{api_name}: {method.upper()} {what} refused by the API policy: {reason}.",
                reason=reason,
            )

    def gate(
        self,
        api_name: str,
        method: str,
        operation_id: str | None = None,
        path: str | None = None,
    ) -> ApprovalGate | None:
        """The human approval the API's policy requires before the call is sent, or None.

        See `gated`. Ask only after `check` passed: approval never widens access.
        """
        return gated(self.api(api_name), method, operation_id, path)


def _beside_pyproject() -> Path | None:
    """The policy path beside the project's `pyproject.toml` (found once, at import)."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return parent / POLICY_FILENAME
    return None


_PROJECT_POLICY_PATH = _beside_pyproject()


def resolve_policy_path() -> Path:
    """`API_POLICY_PATH` when set; else `./api-policy.yaml`, else the one beside `pyproject.toml`."""
    configured = os.environ.get(POLICY_PATH_ENV, "").strip()
    if configured:
        return Path(configured)
    local = Path(POLICY_FILENAME)
    if local.is_file():
        return local
    return _PROJECT_POLICY_PATH or local


_cache: dict[tuple[int, int, int, int], ApiPolicy] = {}


def load_policy() -> ApiPolicy:
    """The project's policy (cached until the file changes); `ApiPolicyError` when absent or invalid.

    Tools call this inside the server's event loop: it never resolves the
    working directory (LangGraph's dev server refuses `os.getcwd()` there as
    a blocking call). The cache is keyed by the file's identity and mtime.
    """
    path = resolve_policy_path()
    try:
        stat = path.stat()
    except OSError:
        return ApiPolicy.load(path)  # raises the not-found error
    key = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)
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
# Limits (`limits: {max_calls_per_run, rate_per_minute}`)
# ---------------------------------------------------------------------------

# A run's counters are dropped after this long without a call to a limited API
# (and when the run ends, see `end_run`); at most MAX_TRACKED_RUNS runs are
# tracked, the least recently active dropped first.
RUN_COUNTER_TTL_S = 3600.0
MAX_TRACKED_RUNS = 10_000
# Calls made outside any run (no LangGraph run id, no request context) share
# this one count: never looser than counting per run.
NO_RUN = "<no run>"


class CallLimiter:
    """Per-run call counts and per-API token buckets, in this process only."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        # run key -> (last call time, {api name: calls})
        self._runs: OrderedDict[str, tuple[float, dict[str, int]]] = OrderedDict()
        # (api name, rate) -> (tokens, last refill time)
        self._buckets: dict[tuple[str, int], tuple[float, float]] = {}

    def acquire(self, api: str, limits: Mapping[str, Any], run_key: str) -> str | None:
        """Count one call to `api` in `run_key`; the reason it is refused, or None."""
        max_calls = limits.get("max_calls_per_run")
        rate = limits.get("rate_per_minute")
        with self._lock:
            now = self._clock()
            self._evict(now)
            counts = self._runs.get(run_key, (now, {}))[1]
            if max_calls and counts.get(api, 0) >= max_calls:
                return (
                    f"limits.max_calls_per_run ({max_calls}) reached: this run already made "
                    f"{counts.get(api, 0)} call(s) to this API"
                )
            if rate:
                tokens, updated = self._buckets.get((api, rate), (float(rate), now))
                tokens = min(float(rate), tokens + (now - updated) * rate / 60.0)
                if tokens < 1.0:
                    self._buckets[(api, rate)] = (tokens, now)
                    wait = (1.0 - tokens) * 60.0 / rate
                    return (
                        f"limits.rate_per_minute ({rate}) exceeded in this process; "
                        f"retry in {wait:.1f} s"
                    )
                self._buckets[(api, rate)] = (tokens - 1.0, now)
            if max_calls:
                counts[api] = counts.get(api, 0) + 1
                self._runs[run_key] = (now, counts)
                self._runs.move_to_end(run_key)
                self._evict(now)
            return None

    def end_run(self, run_key: str) -> None:
        with self._lock:
            self._runs.pop(run_key, None)

    def tracked_runs(self) -> int:
        with self._lock:
            return len(self._runs)

    def _evict(self, now: float) -> None:
        while self._runs:
            key, (seen, _counts) = next(iter(self._runs.items()))
            if len(self._runs) <= MAX_TRACKED_RUNS and now - seen <= RUN_COUNTER_TTL_S:
                break
            del self._runs[key]


_limiter = CallLimiter()


def reset_limits(clock: Callable[[], float] = time.monotonic) -> CallLimiter:
    """Start from empty counters (tests); returns the new limiter."""
    global _limiter
    _limiter = CallLimiter(clock)
    return _limiter


def end_run(run_id: str | None) -> None:
    """Drop the call counts of a finished run (the chat runtime calls it)."""
    if run_id:
        _limiter.end_run(str(run_id))


def current_run_id() -> str | None:
    """The id of the agent run making the call.

    The LangGraph run's (`run_id` in the run's config metadata, which the chat
    runtime and LangGraph Server set), else the request context's (the
    `run_id`, then the `request_id`, bound for the current request's logs),
    else None.
    """
    try:
        from langgraph.config import get_config

        config = get_config()
    except Exception:  # outside a graph run
        config = None
    if isinstance(config, Mapping):
        for section in ("metadata", "configurable"):
            values = config.get(section)
            if isinstance(values, Mapping) and values.get("run_id"):
                return str(values["run_id"])
    try:
        from .telemetry import LOG_CONTEXT
    except ImportError:  # loaded outside its package
        return None
    for name in ("run_id", "request_id"):
        value = LOG_CONTEXT[name].get()
        if value:
            return str(value)
    return None


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
        run_id: str | None = None,
    ) -> None:
        self.policy = policy
        self.name = name
        self.settings = policy.api(name)
        self._credential = credential
        self._transport = transport
        self._run_id = run_id

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

    def query(self, params: Any) -> httpx.QueryParams | None:
        """`params` as the query that will be sent, with `pagination.max_page_size` enforced.

        `params` may be anything httpx accepts (a mapping, a list of pairs or a
        query string); it is converted once and the converted query is what is
        sent. Every value of the page-size parameter is checked, whatever the
        letter case of its name, and each must be a plain number from 1 to the cap.
        A `_method` parameter (a method override) is refused.
        """
        if params is None:
            return None
        try:
            query = httpx.QueryParams(params)
        except (TypeError, ValueError) as exc:
            raise ApiPolicyError(f"{self.name}: params are not a valid query ({exc}).") from exc
        for key in query:
            if key.casefold() == METHOD_OVERRIDE_PARAM:
                raise ApiPolicyError(
                    f"{self.name}: the query parameter {key!r} refused: it overrides the "
                    "request method on some servers, and the policy checks the method sent."
                )
        pagination = self.settings.get("pagination")
        if not pagination:
            return query
        param = pagination["page_size_param"]
        cap = pagination["max_page_size"]
        for key, value in query.multi_items():
            if key.casefold() != param.casefold():
                continue
            digits = value.isascii() and value.isdigit() and len(value) <= len(str(cap))
            if not (digits and 1 <= int(value) <= cap):
                raise ApiPolicyError(
                    f"{self.name}: {key}={value!r} refused: page size must be 1-{cap} "
                    "(the API's pagination.max_page_size)."
                )
        return query

    def take_limits(self, method: str, what: str) -> None:
        """Count the call against the API's `limits`; `ApiPolicyError` when one is exceeded."""
        limits = self.settings.get("limits")
        if not limits:
            return
        run_key = self._run_id or current_run_id() or NO_RUN
        reason = _limiter.acquire(self.name, limits, run_key)
        if reason:
            raise ApiPolicyError(
                f"{self.name}: {method} {what} refused by the API policy: {reason}.", reason=reason
            )

    # -- requests -------------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        operation_id: str | None = None,
        path_params: Mapping[str, Any] | None = None,
        params: Any = None,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """Send `method` on `path` after the policy check; return the JSON body or the text.

        Pass `path` as the template declared in `API_CALLS` (for example
        `/orders/{order_id}`) with the values in `path_params`: the policy is
        checked against the template and against the rendered path, and the
        client encodes each value as one segment, so model-chosen input cannot
        change which endpoint is hit. A concrete `path` is accepted too but
        validated (no dot segments, encoded slashes, empty segments, query or
        fragment); policy paths match it after decoding percent-encoded
        unreserved characters and ignoring one trailing slash, and denials
        also ignore letter case. A denial that pins a path refuses every call
        to that path whatever `operation_id` it names; name `operation_id`
        whenever the API has a denial by `operationId` alone, which refuses a
        call without one.
        `params` (a mapping, a list of pairs or a query string) is checked
        against `pagination.max_page_size`. The API's base URL may carry a
        path prefix (`https://host/v2`); `path` is joined under it.
        `json_body` is sent as JSON with any method the policy allows. The
        API's `limits` are counted last, just before sending. Redirects are
        never followed. An empty response body returns "". Raises
        `ApiPolicyError` (nothing sent) or `ApiCallError` (with `status_code`
        and a bounded `body` for a non-2xx response).

        `headers` may not change where the request goes or which method the
        server applies: `Host`, method-override (`X-HTTP-Method-Override`,
        `X-HTTP-Method`, `X-Method-Override`), `X-Forwarded-*`, `Forwarded`,
        `X-Original-URL`, `X-Rewrite-URL` and hop-by-hop headers are dropped
        (`FORBIDDEN_HEADERS`), and a `_method` query parameter or top-level
        JSON body key is refused. Each call is logged by API, method,
        operation id and path template (never the query, the concrete path
        or the body).
        """
        method = method.upper()
        label = operation_id or (path if path_params is not None else "<concrete path>")
        log_fields = {
            "api": self.name,
            "method": method,
            "operation_id": operation_id,
            "path_template": path if path_params is not None else None,
        }
        try:
            wire_path, url, query, request_headers, secrets = self._prepare(
                method, path, operation_id, path_params, params, json_body, headers
            )
        except ApiPolicyError as exc:
            logger.warning(
                "api call refused: %s %s %s: %s",
                self.name,
                method,
                label,
                exc.reason or "invalid request",
                extra=log_fields,
            )
            raise
        except ApiCallError:
            logger.warning("api call not sent: %s %s %s: not configured", self.name, method, label)
            raise
        started = time.perf_counter()
        async with httpx.AsyncClient(
            transport=self._transport, timeout=self.timeout(), follow_redirects=False
        ) as client:
            try:
                response = await client.request(
                    method, url, params=query, json=json_body, headers=request_headers
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                self._log_call(label, status, started, log_fields, failed=True)
                body = error_body_excerpt(exc.response.text, secrets) or None
                reason = f": {' '.join(body.split())[:ERROR_MESSAGE_BODY_CHARS]}" if body else ""
                raise ApiCallError(
                    f"{self.name}: {method} {wire_path} -> HTTP {status}{reason}",
                    status_code=status,
                    body=body,
                ) from exc
            except httpx.HTTPError as exc:
                self._log_call(label, type(exc).__name__, started, log_fields, failed=True)
                raise ApiCallError(
                    f"{self.name}: {method} {wire_path} failed: {type(exc).__name__}"
                ) from exc
        self._log_call(label, response.status_code, started, log_fields)
        if response.content and "json" in response.headers.get("content-type", ""):
            try:
                return response.json()
            except ValueError as exc:
                raise ApiCallError(
                    f"{self.name}: {method} {wire_path} returned invalid JSON",
                    status_code=response.status_code,
                ) from exc
        return response.text

    def _prepare(
        self,
        method: str,
        path: str,
        operation_id: str | None,
        path_params: Mapping[str, Any] | None,
        params: Any,
        json_body: Any,
        headers: Mapping[str, str] | None,
    ) -> tuple[str, httpx.URL, httpx.QueryParams | None, httpx.Headers, tuple[str, ...]]:
        """Every check before sending: the path, URL, query, headers and the credential sent."""
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
        query = self.query(params)
        if isinstance(json_body, Mapping) and any(
            isinstance(k, str) and k.casefold() == METHOD_OVERRIDE_PARAM for k in json_body
        ):
            raise ApiPolicyError(
                f"{self.name}: the JSON body key {METHOD_OVERRIDE_PARAM!r} refused: it overrides "
                "the request method on some servers, and the policy checks the method sent."
            )

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
        dropped = sorted({name for name in request_headers if forbidden_header(name)})
        for name in dropped:
            del request_headers[name]
        if dropped:
            logger.warning(
                "api call: dropped header(s) a tool may not set: %s",
                ", ".join(dropped),
                extra={"api": self.name},
            )
        credentials = self.auth_headers()
        for name, value in credentials.items():
            request_headers[name] = value  # the policy's credential always wins
        secrets = tuple(credentials.values()) + tuple(
            value.split(" ", 1)[1] for value in credentials.values() if " " in value
        )
        self.refuse_gated(method, operation_id, path, wire_path, path_params is not None)
        self.take_limits(method, operation_id or path)
        return wire_path, url, query, request_headers, secrets

    def refuse_gated(
        self,
        method: str,
        operation_id: str | None,
        path: str,
        wire_path: str,
        templated: bool,
    ) -> None:
        """Refuse (`ApiPolicyError`, nothing sent) a call the API's `approval` gates.

        Gated when the sent path is, or the template it was rendered from.
        This client cannot yet pause the run for a human decision, and a
        policy must never count on an approval step that is skipped, so a
        gated call fails closed.
        """
        gate = self.policy.gate(self.name, method, operation_id, wire_path)
        if gate is None and templated:
            gate = self.policy.gate(self.name, method, operation_id, path)
        if gate is None:
            return
        raise ApiPolicyError(
            f"{self.name}: {method} {operation_id or path} needs human approval "
            f"({', '.join(gate.approvers)}) before it is sent, which this agent cannot request: "
            "refused, nothing was sent.",
            reason=f"approval required by {gate.rule}",
        )

    def _log_call(
        self,
        label: str,
        outcome: int | str,
        started: float,
        fields: Mapping[str, Any],
        *,
        failed: bool = False,
    ) -> None:
        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.log(
            logging.WARNING if failed else logging.INFO,
            "api call %s: %s %s %s -> %s (%d ms)",
            "failed" if failed else "done",
            self.name,
            fields["method"],
            label,
            outcome,
            latency_ms,
            extra={**fields, "outcome": str(outcome), "latency_ms": latency_ms},
        )

    async def get(self, path: str, **kwargs: Any) -> Any:
        return await self.request("GET", path, **kwargs)

    async def head(self, path: str, **kwargs: Any) -> Any:
        return await self.request("HEAD", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> Any:
        return await self.request("POST", path, **kwargs)

    async def put(self, path: str, **kwargs: Any) -> Any:
        return await self.request("PUT", path, **kwargs)

    async def patch(self, path: str, **kwargs: Any) -> Any:
        return await self.request("PATCH", path, **kwargs)

    async def delete(self, path: str, **kwargs: Any) -> Any:
        return await self.request("DELETE", path, **kwargs)

    async def options(self, path: str, **kwargs: Any) -> Any:
        return await self.request("OPTIONS", path, **kwargs)


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
    run_id: str | None = None,
) -> ApiClient:
    """A policy-enforcing client for API `api_name` of `api-policy.yaml`.

    `context` is the run context holding the calling principal (a tool's
    `runtime.context`); by default it is read from the current graph run.
    `run_id` names the run `limits.max_calls_per_run` counts against; by
    default it is read from the current run (`current_run_id`).
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
    return ApiClient(policy, api_name, credential=credential, transport=transport, run_id=run_id)


# ---------------------------------------------------------------------------
# The caller: act only on what the calling user may act on
# ---------------------------------------------------------------------------
#
# The API policy decides which endpoints a tool may call, not on whose behalf.
# Text a tool returns (a customer's free-text note, an upstream error) reaches
# the model too, and can ask it to act on some other record. Prefer per-user
# authorization upstream (`auth: forward`, so the API itself refuses what the
# user may not do); where a shared service token is used, the tool is the only
# place that knows the caller, so write tools check it themselves with these.

# The principal of a run context the server did not fill in (AgentContext's default).
ANONYMOUS_PRINCIPAL = "anonymous"


@dataclass(frozen=True)
class Caller:
    """The principal a run acts for, as tools see it (from the run context)."""

    principal_id: str
    roles: frozenset[str]

    def has_role(self, *roles: str) -> bool:
        return bool(self.roles.intersection(roles))


def current_caller(context: Any = None) -> Caller:
    """The calling principal of the current run (or of `context`, a tool's `runtime.context`).

    Fails closed: without an authenticated principal in the run context it
    raises `ApiPolicyError` (which the agent turns into a tool error), so a
    tool never acts on anyone's behalf by default.
    """
    ctx = context if context is not None else current_context()
    if isinstance(ctx, Mapping):
        principal_id, roles = ctx.get("principal_id"), ctx.get("roles")
    else:
        principal_id, roles = getattr(ctx, "principal_id", None), getattr(ctx, "roles", None)
    if not isinstance(principal_id, str) or principal_id in ("", ANONYMOUS_PRINCIPAL):
        raise ApiPolicyError(
            "refused: this run has no authenticated caller, so no tool may act on anyone's behalf."
        )
    names = roles if isinstance(roles, list | tuple | set | frozenset) else ()
    return Caller(principal_id, frozenset(r for r in names if isinstance(r, str)))


def require_owner(owner: Any, *, context: Any = None, allow_roles: tuple[str, ...] = ()) -> Caller:
    """Refuse (`ApiPolicyError`) unless the record `owner` (its owner's principal id, as the
    upstream API reports it) is the caller, or the caller holds one of `allow_roles`.

    The ids must match exactly (no case folding). The refusal names neither
    the owner nor the caller. Under the `shared-bearer` auth policy every
    caller is the one principal `shared`, so this check needs a per-user
    policy (`jwt` or `custom`).
    """
    caller = current_caller(context)
    if isinstance(owner, str) and owner and owner == caller.principal_id:
        return caller
    if allow_roles and caller.has_role(*allow_roles):
        return caller
    raise ApiPolicyError(
        "refused: the record belongs to someone other than the caller; act only on the "
        "caller's own records."
    )


_MENTION_MAX_CHARS = 80


def latest_user_message(runtime: Any) -> str:
    """The text of the last user (human) message in the run's state (`runtime.state`)."""
    state = getattr(runtime, "state", None)
    messages = state.get("messages") if isinstance(state, Mapping) else None
    for message in reversed(messages or []):
        kind = message.get("type") if isinstance(message, Mapping) else getattr(message, "type", "")
        if kind not in ("human", "user"):
            continue
        content = (
            message.get("content")
            if isinstance(message, Mapping)
            else getattr(message, "content", "")
        )
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                str(b.get("text", "")) if isinstance(b, Mapping) else str(b) for b in content
            )
        return ""
    return ""


def require_user_mentioned(value: Any, runtime: Any) -> None:
    """Refuse (`ApiPolicyError`) unless `value` appears in the user's latest message.

    For write tools acting on a record the model chose (an order id, say):
    the user's own message cannot be forged by text a tool returned, so an
    instruction planted in upstream data ("also cancel ORD-17") cannot make
    the agent write to a record the user never named. Matching ignores
    letter case and needs the whole id (letters, digits, `_` and `-` around
    it end it: `ORD-1` does not match `ORD-17`, nor `17` match `ORD-17`).
    `runtime` is the tool's `ToolRuntime`.
    """
    token = str(value if value is not None else "").strip()
    text = latest_user_message(runtime)
    pattern = rf"(?<![A-Za-z0-9_-]){re.escape(token)}(?![A-Za-z0-9_-])"
    if not token or not re.search(pattern, text, re.IGNORECASE):
        shown = token[:_MENTION_MAX_CHARS]
        raise ApiPolicyError(
            f"refused: {shown!r} is not named in the user's latest message; ask the user to "
            "confirm it before acting on it."
        )
