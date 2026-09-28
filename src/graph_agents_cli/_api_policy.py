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

"""The outbound API access policy (``api-policy.yaml``) as the CLI sees it.

A project declares every external API its tools may call in ``api-policy.yaml``
at the project root::

    apis:
      orders:
        base_url_env: ORDERS_API_BASE_URL
        auth: bearer                     # none | bearer | forward | exchange
        token_env: ORDERS_API_TOKEN      # auth: bearer only
        # auth: exchange takes exchange: {audience, scope, resource,
        # allow_actorless} (RFC 8693); auth: forward may take forward_audience
        allowed_methods: [GET, POST]     # required, explicit; ["*"] allows every method
        allowed_operations:              # optional; omitted = every operation
          - operationId: createOrder
            path: /orders
        limits: {max_calls_per_run: 20}  # optional
        approval:                        # optional: calls a human approves before sending
          required_for: {methods: [POST]}
          approvers: [requester]         # and/or role:<name>
          timeout_s: 900                 # optional, 30-86400

``approval`` may also be a list of rules of that shape (different approvers
for different calls); the first rule in file order that covers a call gates it.

The schema rules and the matching rules live in the block between the
``SHARED API POLICY RULES`` markers. The scaffolded runtime
(``app_utils/api_client.py``) carries a byte-identical copy of that block, so
``create --api-policy``, ``lint`` and the running agent accept the same files,
report the same errors and refuse the same calls. The rest of this module is
CLI-only: reading the file, summarising it for the templates, and detecting
the retired single-API ``product_api`` format. ``graph-agents-cli api`` edits
the file (``graph_agents_cli.api``).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import click
import yaml

# --- BEGIN SHARED API POLICY RULES ---
# Identical in graph-agents-cli (graph_agents_cli/_api_policy.py) and in every
# scaffolded project (app_utils/api_client.py). A CLI test keeps the two copies
# byte-identical: change both or neither.

POLICY_FILENAME = "api-policy.yaml"
AUTH_MODES = ("none", "bearer", "forward", "exchange")
# The modes that send the caller's identity in `forward_header` (default Authorization):
# `forward` the caller's own credential, `exchange` a token the issuer mints for the API in
# exchange for the caller's (RFC 8693, configured by the API's `exchange` block).
HEADER_AUTH_MODES = ("forward", "exchange")
EXCHANGE_KEY = "exchange"
# `exchange.allow_actorless: true` lets an `auth: exchange` API be called with an exchanged
# token that names no actor (no `act` claim, or one that is not a readable JWT); the calling
# agent refuses such tokens otherwise, since the agent behind the API would read them as the
# user's own unless it sets AUTH_JWT_DIRECT_CLIENTS.
ALLOW_ACTORLESS_KEY = "allow_actorless"
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
_SPACE_BY_DOT_RE = re.compile(r"\s\.|\.\s")

_ESCAPE_RE = re.compile(r"%[0-9A-Fa-f]{2}")
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")

# An API's `protocol` says how its calls are judged: `http` (the default) by method, path
# and the operation id the tool names; `jsonrpc` also by the JSON-RPC request every POST
# sends, whose method (`rpc_method`) the policy client reads from the body, never from the
# tool; `a2a` (another agent, over A2A 1.0 JSON-RPC) as `jsonrpc`, plus what a message
# decides (`a2a_operation`: `approve` or `reject` a pending approval of that agent). A
# JSON-RPC API allows GET, POST and HEAD only, and an `a2a` one names its endpoint
# (`a2a.path`) and must gate or deny `a2a_operation: approve` if it can send messages.
PROTOCOL_KEY = "protocol"
PROTOCOL_HTTP = "http"
PROTOCOL_JSONRPC = "jsonrpc"
PROTOCOL_A2A = "a2a"
PROTOCOLS = (PROTOCOL_HTTP, PROTOCOL_JSONRPC, PROTOCOL_A2A)
DEFAULT_PROTOCOL = PROTOCOL_HTTP
RPC_PROTOCOLS = (PROTOCOL_JSONRPC, PROTOCOL_A2A)
RPC_HTTP_METHODS = ("GET", "POST", "HEAD")
A2A_KEY = "a2a"
_A2A_KEYS = ("path",)
DESCRIPTION_KEY = "description"
DESCRIPTION_MAX_CHARS = 300
RPC_METHOD_KEY = "rpc_method"
A2A_OPERATION_KEY = "a2a_operation"
A2A_APPROVE = "approve"
A2A_REJECT = "reject"
A2A_OPERATIONS = (A2A_APPROVE, A2A_REJECT)
# A JSON-RPC method name as an operation entry pins it.
_RPC_METHOD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_/.]{0,63}")
# The A2A 0.3 method names and the A2A 1.0 names they are read as under `protocol: a2a`,
# so a 0.3 spelling of a call cannot slip past an entry that names it.
A2A_V03_METHODS = {
    "message/send": "SendMessage",
    "message/stream": "SendStreamingMessage",
    "tasks/get": "GetTask",
    "tasks/list": "ListTasks",
    "tasks/cancel": "CancelTask",
    "tasks/resubscribe": "SubscribeToTask",
    "tasks/pushNotificationConfig/set": "CreateTaskPushNotificationConfig",
    "tasks/pushNotificationConfig/get": "GetTaskPushNotificationConfig",
    "tasks/pushNotificationConfig/list": "ListTaskPushNotificationConfigs",
    "tasks/pushNotificationConfig/delete": "DeleteTaskPushNotificationConfig",
    "agent/getAuthenticatedExtendedCard": "GetExtendedAgentCard",
}
# The A2A methods that send a message, which may carry a decision on an approval.
A2A_MESSAGE_METHODS = ("SendMessage", "SendStreamingMessage")
# Their names in any letter case, 0.3 spellings included: a message sent under any of them
# is read for a decision (failing closed toward a server that matched names loosely).
_A2A_MESSAGE_NAMES = frozenset(
    name.casefold() for name in (*A2A_MESSAGE_METHODS, "message/send", "message/stream")
)
# The members of one JSON-RPC 2.0 request object.
_JSONRPC_KEYS = ("jsonrpc", "method", "params", "id")

_POLICY_KEYS = ("apis",)
_API_KEYS = (
    DESCRIPTION_KEY,
    PROTOCOL_KEY,
    A2A_KEY,
    "base_url_env",
    "auth",
    "token_env",
    "forward_header",
    "forward_audience",
    EXCHANGE_KEY,
    "allowed_methods",
    "allowed_operations",
    "denied_operations",
    "openapi",
    "timeouts_ms",
    "pagination",
    "limits",
    "approval",
)
_OPERATION_KEYS = ("operationId", "path", "methods", RPC_METHOD_KEY, A2A_OPERATION_KEY)
_TIMEOUT_KEYS = ("connect", "read")
_PAGINATION_KEYS = ("page_size_param", "max_page_size")
_LIMIT_KEYS = ("max_calls_per_run", "rate_per_minute", "max_response_bytes")
# `limits.max_response_bytes`: the most a response body may hold (decoded) before the
# client stops reading it and discards it. Unset: no cap (as in 0.2).
MAX_RESPONSE_BYTES_LIMIT = 67108864
_APPROVAL_KEYS = ("required_for", "approvers", "timeout_s", "decide_with", "relayers")
_REQUIRED_FOR_KEYS = ("methods", "operations")
_EXCHANGE_KEYS = ("audience", "scope", "resource", ALLOW_ACTORLESS_KEY)
# An RFC 6749 scope: space-separated scope tokens (printable ASCII but space, " " and "\").
_SCOPE_RE = re.compile(r"[\x21\x23-\x5b\x5d-\x7e]+(?: [\x21\x23-\x5b\x5d-\x7e]+)*")
# An absolute URI (RFC 8707 `resource`): a scheme, then no whitespace and no fragment.
_ABSOLUTE_URI_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:[^\s#\x00-\x1f\x7f]+")

# An API's `approval` block names the calls a human must approve before they are
# sent (`required_for`), who may approve them (`approvers`) and how long a
# pending approval waits before it expires, which rejects the call
# (`timeout_s`). It is one such rule (a mapping), or a non-empty list of rules
# of that same shape when different calls need different approvers: a call is
# gated by the FIRST rule, in file order, whose `required_for` covers it, and a
# later rule that also covers it does not apply to it. A call that an earlier
# rule covers only because it leaves out what the rule knows the operation by
# (no operation id, no path), and that a later rule with other approvers also
# covers, is refused (`ApprovalRuleConflict`): it could be either rule's call.
# It never widens access: a gated call must still be allowed, and denials still
# win. It belongs to the API only: on an operation entry the key is refused,
# with a pointer to `approval.required_for.operations`. A rule may also say how
# the requester decides (`decide_with`): `direct` (the default: with their own
# credentials, at this agent), or `relayed`, where the agents `relayers` names
# (by their actor ids) may deliver the requester's decision from another agent.
APPROVAL_KEY = "approval"
REQUESTER_APPROVER = "requester"  # the principal who started the run confirms
ROLE_APPROVER_PREFIX = "role:"  # any principal holding the role decides
DEFAULT_APPROVAL_TIMEOUT_S = 900
MIN_APPROVAL_TIMEOUT_S = 30
MAX_APPROVAL_TIMEOUT_S = 86400
# How approvers decide once an approval rule takes `decide_with` (0.3): `direct` by
# default, each with their own credential. `relayed`, with the `relayers` it names, lets
# those agents deliver the requester's decision: an opt-in that each callee's gate reviews.
DEFAULT_DECIDE_WITH = "direct"
DECIDE_DIRECT = "direct"
DECIDE_RELAYED = "relayed"
DECIDE_WITH_VALUES = (DECIDE_DIRECT, DECIDE_RELAYED)
# A value kept for a later release: a decision signed by the identity provider.
DECIDE_STEP_UP = "step_up"
# Role names and actor ids (`relayers`): 1-256 characters, no whitespace, commas or
# control characters.
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


def _is_audience(value: Any) -> bool:
    """An audience (a token's `aud`): 1-256 characters, no whitespace, commas or control
    characters (the target's `AUTH_JWT_AUDIENCE` is a comma list of them)."""
    return isinstance(value, str) and _ROLE_NAME_RE.fullmatch(value) is not None


def _exchange_errors(where: str, value: Any) -> list[str]:
    """Errors of an API's `exchange` block (`auth: exchange`, RFC 8693)."""
    if not isinstance(value, Mapping):
        return [
            f"{where}: must be a mapping with audience, and optionally scope, resource and "
            f"{ALLOW_ACTORLESS_KEY}"
        ]
    errors = [
        f"{where}: unknown key {key!r}" for key in sorted(set(value) - set(_EXCHANGE_KEYS), key=str)
    ]
    if "audience" not in value:
        errors.append(
            f"{where}.audience: required (the audience the issuer mints the token for: the "
            "target's AUTH_JWT_AUDIENCE)"
        )
    elif not _is_audience(value["audience"]):
        errors.append(
            f"{where}.audience: must be an audience (1-256 characters without spaces, commas or "
            "control characters)"
        )
    if "scope" in value:
        scope = value["scope"]
        if not (isinstance(scope, str) and _SCOPE_RE.fullmatch(scope)):
            errors.append(
                f"{where}.scope: must be scopes separated by single spaces (RFC 6749: printable "
                "ASCII, no quotes or backslashes)"
            )
    if "resource" in value:
        resource = value["resource"]
        if not (isinstance(resource, str) and _ABSOLUTE_URI_RE.fullmatch(resource)):
            errors.append(
                f"{where}.resource: must be an absolute URI without a fragment (RFC 8707), "
                "such as https://orders.example.com"
            )
    if ALLOW_ACTORLESS_KEY in value and not isinstance(value[ALLOW_ACTORLESS_KEY], bool):
        errors.append(
            f"{where}.{ALLOW_ACTORLESS_KEY}: must be true or false (true: accept exchanged "
            "tokens that name no actor, once the agent behind the API sets "
            "AUTH_JWT_DIRECT_CLIENTS)"
        )
    return errors


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
    modes = ", ".join(AUTH_MODES)
    if "auth" not in api:
        errors.append(f"{where}.auth: required (one of {modes})")
    elif auth not in AUTH_MODES:
        errors.append(f"{where}.auth: must be one of {modes} (got {auth!r})")

    if auth == "bearer":
        if "token_env" not in api:
            errors.append(f"{where}.token_env: required when auth is bearer")
        elif not _is_env_name(api["token_env"]):
            errors.append(f"{where}.token_env: must be an environment variable name")
    elif "token_env" in api:
        errors.append(f"{where}.token_env: only valid with auth: bearer")

    if "forward_header" in api:
        header = api["forward_header"]
        if auth not in HEADER_AUTH_MODES:
            errors.append(f"{where}.forward_header: only valid with auth: forward or exchange")
        elif not (isinstance(header, str) and HEADER_NAME_RE.match(header)):
            errors.append(f"{where}.forward_header: must be an HTTP header name")

    if "forward_audience" in api:
        if auth != "forward":
            errors.append(f"{where}.forward_audience: only valid with auth: forward")
        elif not _is_audience(api["forward_audience"]):
            errors.append(
                f"{where}.forward_audience: must be an audience (1-256 characters without "
                "spaces, commas or control characters)"
            )

    if auth == "exchange":
        if EXCHANGE_KEY not in api:
            errors.append(
                f"{where}.{EXCHANGE_KEY}: required when auth is exchange (a mapping with the "
                "audience the issuer mints the token for, and optionally scope, resource and "
                f"{ALLOW_ACTORLESS_KEY})"
            )
        else:
            errors.extend(_exchange_errors(f"{where}.{EXCHANGE_KEY}", api[EXCHANGE_KEY]))
    elif EXCHANGE_KEY in api:
        errors.append(f"{where}.{EXCHANGE_KEY}: only valid with auth: exchange")

    protocol = api.get(PROTOCOL_KEY, DEFAULT_PROTOCOL)
    errors.extend(_protocol_errors(where, api, protocol, auth))

    if "allowed_methods" not in api:
        errors.append(f'{where}.allowed_methods: required (a list of HTTP methods, or ["*"])')
    else:
        method_errors = _methods_errors(f"{where}.allowed_methods", api["allowed_methods"], True)
        errors.extend(method_errors)
        if protocol in RPC_PROTOCOLS and not method_errors:
            outside = [
                str(m).upper()
                for m in api["allowed_methods"]
                if str(m).upper() not in RPC_HTTP_METHODS
            ]
            if outside:
                errors.append(
                    f"{where}.allowed_methods: protocol {protocol} allows GET, POST and HEAD "
                    f"only (a JSON-RPC request is a POST), not {', '.join(outside)}"
                )

    if "allowed_operations" in api:
        errors.extend(
            _operations_errors(
                f"{where}.allowed_operations",
                api["allowed_operations"],
                where,
                "must not be empty; omit the key to allow every operation within allowed_methods",
                protocol=protocol,
            )
        )
    if "denied_operations" in api:
        errors.extend(
            _operations_errors(
                f"{where}.denied_operations", api["denied_operations"], where, protocol=protocol
            )
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
        errors.extend(_approval_errors(where, api[APPROVAL_KEY], protocol=protocol))
    if not errors:
        errors.extend(_approve_errors(where, api))
    return errors


def _protocol_errors(where: str, api: Mapping[str, Any], protocol: Any, auth: Any) -> list[str]:
    """Errors of an API's `protocol`, `a2a` and `description`."""
    errors: list[str] = []
    if protocol not in PROTOCOLS:
        errors.append(
            f"{where}.{PROTOCOL_KEY}: must be one of {', '.join(PROTOCOLS)} (got {protocol!r})"
        )
    if protocol == PROTOCOL_A2A:
        if A2A_KEY not in api:
            errors.append(
                f"{where}.{A2A_KEY}: required with protocol a2a (a mapping with path, the "
                "agent's A2A endpoint, such as /a2a/orders)"
            )
        else:
            errors.extend(_a2a_errors(f"{where}.{A2A_KEY}", api[A2A_KEY]))
        if auth == "none":
            errors.append(
                f"{where}.auth: protocol a2a needs a credential (bearer, forward or exchange): "
                "an agent's A2A endpoint authenticates its callers"
            )
    elif A2A_KEY in api:
        errors.append(f"{where}.{A2A_KEY}: only valid with protocol a2a")
    if DESCRIPTION_KEY in api:
        description = api[DESCRIPTION_KEY]
        if not (
            isinstance(description, str)
            and description.strip()
            and len(description) <= DESCRIPTION_MAX_CHARS
            and not any(ord(c) < 0x20 or 0x7F <= ord(c) < 0xA0 for c in description)
        ):
            errors.append(
                f"{where}.{DESCRIPTION_KEY}: must be text of 1-{DESCRIPTION_MAX_CHARS} "
                "characters without control characters"
            )
    return errors


def _a2a_errors(where: str, value: Any) -> list[str]:
    """Errors of an API's `a2a` block: `path`, the literal path of the agent's A2A endpoint."""
    if not isinstance(value, Mapping):
        return [f"{where}: must be a mapping with path (the agent's A2A endpoint, /a2a/<name>)"]
    errors = [
        f"{where}: unknown key {key!r}" for key in sorted(set(value) - set(_A2A_KEYS), key=str)
    ]
    if "path" not in value:
        errors.append(f"{where}.path: required (the agent's A2A endpoint, such as /a2a/orders)")
        return errors
    path = value["path"]
    problem = path_template_problem(path)
    if problem:
        errors.append(f"{where}.path: {problem}")
    elif "{" in path or path.rstrip("/") == "":
        errors.append(
            f"{where}.path: must be the literal path of one endpoint (no placeholders), such "
            "as /a2a/orders"
        )
    return errors


def api_protocol(api: Mapping[str, Any]) -> str:
    """An API's `protocol`: `http` when it sets none."""
    return str(api.get(PROTOCOL_KEY) or DEFAULT_PROTOCOL)


def _rpc_pins(entry: Mapping[str, Any]) -> bool:
    """Whether an operation entry pins what a JSON-RPC request is (`rpc_method`, `a2a_operation`)."""
    return entry.get(RPC_METHOD_KEY) is not None or entry.get(A2A_OPERATION_KEY) is not None


def _may_send_approve(api: Mapping[str, Any]) -> bool:
    """Whether an A2A API's allow-list may let through a message that approves (`approve`)."""
    methods = {str(m).upper() for m in api.get("allowed_methods") or []}
    if "POST" not in methods and ANY_METHOD not in methods:
        return False
    allowed = api.get("allowed_operations")
    if allowed is None:
        return True
    return any(
        _methods_match(entry, "POST")
        and entry.get(RPC_METHOD_KEY) in (None, *A2A_MESSAGE_METHODS)
        and entry.get(A2A_OPERATION_KEY) in (None, A2A_APPROVE)
        for entry in allowed
    )


def _covers_every_approve(entry: Mapping[str, Any]) -> bool:
    """Whether a denial or gate entry covers every message that approves, on any path."""
    return entry.get(A2A_OPERATION_KEY) == A2A_APPROVE and _methods_match(entry, "POST")


def approve_is_held(api: Mapping[str, Any]) -> bool:
    """Whether every message that approves waits for a human approval, or is denied.

    An approval rule gating POST (or `"*"`), or an entry `a2a_operation: approve` (with no
    methods, or POST among them) in a rule's `required_for.operations` or in
    `denied_operations`. An entry pinning `rpc_method: SendMessage` does not count: it
    leaves `SendStreamingMessage` out.
    """
    for rule in approval_rules(api):
        required_for = rule.get("required_for") or {}
        methods = {str(m).upper() for m in required_for.get("methods") or []}
        if "POST" in methods or ANY_METHOD in methods:
            return True
        if any(_covers_every_approve(entry) for entry in required_for.get("operations") or []):
            return True
    return any(_covers_every_approve(entry) for entry in api.get("denied_operations") or [])


def _approve_errors(where: str, api: Mapping[str, Any]) -> list[str]:
    """A `protocol: a2a` API that may send a message must gate or deny `approve`: otherwise
    this agent could decide, on its own, the approvals the agent behind it waits for."""
    if api_protocol(api) != PROTOCOL_A2A or not _may_send_approve(api) or approve_is_held(api):
        return []
    name = where.split(".", 1)[1] if "." in where else where
    agent = str((api.get(A2A_KEY) or {}).get("path") or "").rstrip("/").rsplit("/", 1)[-1]
    return [
        f"{where}: protocol a2a allows SendMessage, so this agent could decide approvals at "
        f"{agent or name}: gate them (graph-agents-cli api approval {name} --a2a-operations "
        f"approve --approvers requester) or deny them (graph-agents-cli api deny {name} "
        "--a2a-operation approve)"
    ]


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
    where: str,
    value: Any,
    api_where: str,
    empty_error: str | None = None,
    *,
    protocol: Any = DEFAULT_PROTOCOL,
) -> list[str]:
    """Errors of a list of operation entries; ``empty_error`` refuses an empty list.

    ``rpc_method`` is valid only with `protocol` jsonrpc or a2a, and ``a2a_operation``
    only with a2a.
    """
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
        if protocol in RPC_PROTOCOLS:
            if not any(key in entry for key in _OPERATION_KEYS if key != "methods"):
                errors.append(f"{at}: needs operationId, path, rpc_method and/or a2a_operation")
        elif "operationId" not in entry and "path" not in entry:
            errors.append(f"{at}: needs operationId and/or path")
        errors.extend(_rpc_entry_errors(at, entry, protocol))
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


def _rpc_entry_errors(at: str, entry: Mapping[str, Any], protocol: Any) -> list[str]:
    """Errors of an operation entry's `rpc_method` and `a2a_operation`."""
    errors: list[str] = []
    rpc_method = entry.get(RPC_METHOD_KEY)
    if RPC_METHOD_KEY in entry:
        if protocol not in RPC_PROTOCOLS:
            errors.append(f"{at}.{RPC_METHOD_KEY}: only valid with protocol jsonrpc or a2a")
        elif not (isinstance(rpc_method, str) and _RPC_METHOD_RE.fullmatch(rpc_method)):
            errors.append(
                f"{at}.{RPC_METHOD_KEY}: must be a JSON-RPC method name (a letter, then up to 63 "
                "letters, digits, '_', '/' or '.')"
            )
        elif protocol == PROTOCOL_A2A and rpc_method in A2A_V03_METHODS:
            errors.append(
                f"{at}.{RPC_METHOD_KEY}: {rpc_method} is the A2A 0.3 name; write "
                f"{A2A_V03_METHODS[rpc_method]} (a 0.3 name in a request is read as its 1.0 name)"
            )
    if A2A_OPERATION_KEY in entry:
        operation = entry[A2A_OPERATION_KEY]
        if protocol != PROTOCOL_A2A:
            errors.append(f"{at}.{A2A_OPERATION_KEY}: only valid with protocol a2a")
        elif operation not in A2A_OPERATIONS:
            errors.append(f"{at}.{A2A_OPERATION_KEY}: must be approve or reject")
        elif rpc_method is not None and rpc_method not in A2A_MESSAGE_METHODS:
            errors.append(
                f"{at}.{A2A_OPERATION_KEY}: goes with rpc_method SendMessage or "
                f"SendStreamingMessage (the messages that decide an approval), not {rpc_method}"
            )
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
        return [
            f"{where}: must be a mapping with max_calls_per_run, rate_per_minute and/or "
            "max_response_bytes"
        ]
    errors = [
        f"{where}: unknown key {key!r}" for key in sorted(set(value) - set(_LIMIT_KEYS), key=str)
    ]
    for key in _LIMIT_KEYS:
        if key in value and not _is_positive_int(value[key]):
            errors.append(f"{where}.{key}: must be an integer >= 1")
    size = value.get("max_response_bytes")
    if _is_positive_int(size) and size > MAX_RESPONSE_BYTES_LIMIT:
        errors.append(
            f"{where}.max_response_bytes: must be an integer from 1 to "
            f"{MAX_RESPONSE_BYTES_LIMIT} (bytes; 64 MiB at most)"
        )
    return errors


def _approval_errors(api_where: str, value: Any, *, protocol: Any = DEFAULT_PROTOCOL) -> list[str]:
    """Errors of an API's `approval`: one rule (a mapping), or a non-empty list of rules."""
    where = f"{api_where}.{APPROVAL_KEY}"
    if isinstance(value, list):
        if not value:
            return [f"{where}: must not be empty; omit the key when no call needs approval"]
        errors: list[str] = []
        for index, rule in enumerate(value):
            errors.extend(
                _approval_rule_errors(f"{where}[{index}]", rule, api_where, protocol=protocol)
            )
        return errors
    if not isinstance(value, Mapping):
        return [
            f"{where}: must be a mapping with required_for and approvers, or a non-empty "
            "list of such mappings (rules; the first that covers a call gates it)"
        ]
    return _approval_rule_errors(where, value, api_where, protocol=protocol)


def _approval_rule_errors(
    where: str, value: Any, api_where: str, *, protocol: Any = DEFAULT_PROTOCOL
) -> list[str]:
    """Errors of one approval rule (``where``: ``apis.<name>.approval`` or ``...approval[i]``)."""
    if not isinstance(value, Mapping):
        return [f"{where}: must be a mapping with required_for and approvers"]
    errors = [
        f"{where}: unknown key {key!r}" for key in sorted(set(value) - set(_APPROVAL_KEYS), key=str)
    ]
    if "required_for" not in value:
        errors.append(f"{where}.required_for: required (the methods and/or operations it gates)")
    else:
        errors.extend(
            _required_for_errors(
                f"{where}.required_for", value["required_for"], api_where, protocol=protocol
            )
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
    errors.extend(_decide_with_errors(where, value))
    return errors


def _decide_with_errors(where: str, rule: Mapping[str, Any]) -> list[str]:
    """Errors of a rule's `decide_with` and `relayers`."""
    decide_with = rule.get("decide_with", DEFAULT_DECIDE_WITH)
    errors: list[str] = []
    if decide_with == DECIDE_STEP_UP:
        errors.append(f"{where}.decide_with: step_up is not supported yet (direct or relayed)")
    elif decide_with not in DECIDE_WITH_VALUES:
        errors.append(f"{where}.decide_with: must be direct or relayed (got {decide_with!r})")
    if decide_with != DECIDE_RELAYED:
        if "relayers" in rule:
            errors.append(f"{where}.relayers: only valid with decide_with: relayed")
        return errors
    approvers = rule.get("approvers")
    if isinstance(approvers, list) and REQUESTER_APPROVER not in approvers:
        errors.append(
            f"{where}.decide_with: relayed needs requester in approvers (role approvers always "
            "decide with their own direct credentials, never relayed)"
        )
    if "relayers" not in rule:
        errors.append(
            f"{where}.relayers: required with decide_with: relayed (the agents, by actor id, "
            "that may deliver the requester's decision)"
        )
        return errors
    relayers = rule["relayers"]
    if not isinstance(relayers, list) or not relayers:
        return [*errors, f"{where}.relayers: must be a non-empty list of agent (actor) ids"]
    for index, relayer in enumerate(relayers):
        if not (isinstance(relayer, str) and _ROLE_NAME_RE.fullmatch(relayer)):
            errors.append(
                f"{where}.relayers[{index}]: {relayer!r} is not an agent id (1-256 characters "
                "without spaces, commas or control characters)"
            )
    return errors


def _required_for_errors(
    where: str, value: Any, api_where: str, *, protocol: Any = DEFAULT_PROTOCOL
) -> list[str]:
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
                protocol=protocol,
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


def segment_text_problem(text: str) -> str | None:
    """Why a path segment's text (percent-decoded) is refused, or None.

    Each of these would send a call to an endpoint other than the one the
    policy judged: a control character anywhere (``%00``: servers that end a
    path at a NUL route it to the part before); whitespace at either end
    (``cancel%20``: servers that trim path segments route it to ``cancel``)
    or next to a dot (``cancel%20.json``, ``cancel%20%2e``: servers that
    trim the name before a format suffix, or strip trailing dots and spaces,
    route it to ``cancel``); a backslash or a slash (``%5C``, ``%2F``:
    servers that decode them before routing split the segment); a ``;``
    (``%3B``: servers that strip path parameters route ``cancel;x`` to
    ``cancel``). Whitespace elsewhere in a segment (``red%20shirt``) is kept:
    trimming does not touch it.
    """
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        return "holds a control character (also percent-encoded, such as %00)"
    if text[:1].isspace() or text[-1:].isspace():
        return "starts or ends with whitespace (also percent-encoded, such as %20)"
    if _SPACE_BY_DOT_RE.search(text):
        return "has whitespace next to a dot (also percent-encoded, such as cancel%20.json)"
    if "/" in text or "\\" in text:
        return "holds a backslash or a percent-encoded slash (%5C, %2F)"
    if ";" in text:
        return "holds ';' (also percent-encoded, %3B), which some servers strip with what follows"
    return None


def path_template_problem(path: Any) -> str | None:
    """Why ``path`` is not a valid path template, or None.

    A template starts with ``/``; each segment holds literal characters and
    ``{name}`` placeholders only (no query, fragment, spaces, empty, ``.`` or
    ``..`` segments, also percent-encoded), and none of the characters a
    sent path is refused for, percent-encoded or not (``segment_text_problem``:
    control characters, whitespace at either end or next to a dot, a
    backslash or an encoded slash, ``;``), so lint passes no declared call the
    client would refuse to send. One trailing slash is allowed.
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
        text = unquote(_PLACEHOLDER_SPLIT_RE.sub("x", segment))
        if text in (".", ".."):
            return "must not contain '.' or '..' segments, also percent-encoded (%2E)"
        problem = segment_text_problem(text)
        if problem:
            return f"has a segment that {problem}"
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


def path_matches(
    template: str, path: str, *, ignore_case: bool = False, suffixes: bool = False
) -> bool:
    """Whether ``path`` is covered by ``template``.

    A ``{name}`` placeholder matches exactly one non-empty segment, so
    ``/items/{item_id}`` covers ``/items/42``, ``/items/{id}`` and itself.
    Both sides are compared normalised (``normalize_path``). Letter case
    counts unless ``ignore_case``: denials ignore it, so ``/ADMIN/1`` cannot
    slip past a denial of ``/admin/{x}`` on a case-insensitive server. With
    ``suffixes`` (denials and approval gates, which fail closed), a segment
    that ends in literal text also covers that segment with a dot suffix:
    ``/orders/{id}/cancel`` covers ``/orders/7/cancel.json`` and
    ``/orders/7/cancel.`` (also spelled ``cancel%2e``), which servers that
    route format suffixes (``.json``) or drop a trailing dot send to the
    same endpoint. An allow never matches that way: it must be shown.
    """
    template, path = normalize_path(template), normalize_path(path)
    if template == path or (ignore_case and template.casefold() == path.casefold()):
        return True
    segments = []
    for segment in template.split("/"):
        parts = _PLACEHOLDER_SPLIT_RE.split(segment)
        pattern = "".join(
            "[^/]+" if part.startswith("{") and part.endswith("}") else re.escape(part)
            for part in parts
        )
        if suffixes and parts[-1] and not parts[-1].endswith("}"):
            pattern += r"(?:\.[^/]*)?"
        segments.append(pattern)
    flags = re.IGNORECASE if ignore_case else 0
    return re.fullmatch("/".join(segments), path, flags) is not None


def _methods_match(entry: Mapping[str, Any], method: str) -> bool:
    methods = entry.get("methods")
    return not methods or method.upper() in {str(m).upper() for m in methods}


def operation_matches(
    entry: Mapping[str, Any],
    method: str,
    operation_id: str | None,
    path: str | None,
    *,
    rpc_method: str | None = None,
    a2a_operation: str | None = None,
) -> bool:
    """Whether an ``allowed_operations`` entry covers the call.

    AND semantics: every field the entry pins (``operationId``, ``path``,
    ``methods``, and on a JSON-RPC API ``rpc_method`` and ``a2a_operation``,
    compared with the values derived from the request body) must match. A
    call that does not name a pinned field (no operation id, no path, no
    JSON-RPC method or no decision) does not match: an allow must be shown.
    """
    if not _methods_match(entry, method):
        return False
    pinned_id = entry.get("operationId")
    if pinned_id is not None and operation_id != pinned_id:
        return False
    pinned_path = entry.get("path")
    if pinned_path is not None and (path is None or not path_matches(pinned_path, path)):
        return False
    pinned_rpc = entry.get(RPC_METHOD_KEY)
    if pinned_rpc is not None and rpc_method != pinned_rpc:
        return False
    pinned_operation = entry.get(A2A_OPERATION_KEY)
    if pinned_operation is not None and a2a_operation != pinned_operation:
        return False
    return (
        pinned_id is not None
        or pinned_path is not None
        or pinned_rpc is not None
        or pinned_operation is not None
    )


def denial_match(
    entry: Mapping[str, Any],
    method: str,
    operation_id: str | None,
    path: str | None,
    *,
    rpc_method: str | None = None,
    a2a_operation: str | None = None,
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
    on the wire. Operation ids and paths are compared ignoring letter case,
    and a path's literal segments also cover their dot-suffixed spellings
    (``cancel.json``, ``cancel.``: ``path_matches`` with ``suffixes``).

    An entry of a JSON-RPC API that pins ``rpc_method`` or ``a2a_operation``
    (the values derived from the request body) covers, with its ``methods``,
    a call whose JSON-RPC method is its ``rpc_method`` (ignoring letter case),
    a call that decides as its ``a2a_operation`` says, and a call that names
    its ``operationId``, whatever the path: its ``path``, if any, neither
    widens nor narrows it. Every POST to such an API names its method (a body
    that does not is refused first), so there is nothing left unnamed.
    """
    if not _methods_match(entry, method):
        return None
    pinned_id = entry.get("operationId")
    if _rpc_pins(entry):
        pinned_rpc = entry.get(RPC_METHOD_KEY)
        if (
            pinned_rpc is not None
            and rpc_method is not None
            and str(rpc_method).casefold() == str(pinned_rpc).casefold()
        ):
            return ""
        pinned_operation = entry.get(A2A_OPERATION_KEY)
        if pinned_operation is not None and a2a_operation == pinned_operation:
            return ""
        if (
            pinned_id is not None
            and operation_id is not None
            and str(operation_id).casefold() == str(pinned_id).casefold()
        ):
            return ""
        return None
    pinned_path = entry.get("path")
    if (
        pinned_path is not None
        and path is not None
        and path_matches(pinned_path, path, ignore_case=True, suffixes=True)
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
    entry: Mapping[str, Any],
    method: str,
    operation_id: str | None,
    path: str | None,
    *,
    rpc_method: str | None = None,
    a2a_operation: str | None = None,
) -> bool:
    """Whether a ``denied_operations`` entry covers the call (see ``denial_match``)."""
    return (
        denial_match(
            entry, method, operation_id, path, rpc_method=rpc_method, a2a_operation=a2a_operation
        )
        is not None
    )


def describe_operation(entry: Mapping[str, Any]) -> str:
    """``operationId=updateOrder path=/orders/{order_id} methods=['PATCH']`` for messages
    (and ``rpc_method=GetTask a2a_operation=approve`` for an entry that pins them)."""
    parts = []
    if entry.get("operationId") is not None:
        parts.append(f"operationId={entry['operationId']}")
    if entry.get("path") is not None:
        parts.append(f"path={entry['path']}")
    if entry.get("methods"):
        parts.append(f"methods={sorted(str(m).upper() for m in entry['methods'])}")
    for key in (RPC_METHOD_KEY, A2A_OPERATION_KEY):
        if entry.get(key) is not None:
            parts.append(f"{key}={entry[key]}")
    return " ".join(parts)


def refusal_reason(
    api: Mapping[str, Any],
    method: str,
    operation_id: str | None = None,
    path: str | None = None,
    *,
    rpc_method: str | None = None,
    a2a_operation: str | None = None,
) -> str | None:
    """Why the API's policy refuses the call, or None when it is allowed.

    Every rule must pass: the method is in ``allowed_methods`` (``["*"]``
    allows every method); no ``denied_operations`` entry may cover the call
    (denials win: a denial pinning a path refuses every call to that path,
    whatever operation id it names, and a call that leaves out what a denial
    knows the operation by is refused by it: ``denial_match``); and, when
    ``allowed_operations`` is present, one of its entries matches
    (``operation_matches``: every field it pins). On a JSON-RPC API
    (``protocol: jsonrpc|a2a``) ``rpc_method`` and ``a2a_operation`` are the
    values ``derive_rpc`` reads from the request body.
    """
    method = method.upper()
    operation_id = operation_id or None
    path = path or None
    rpc = {"rpc_method": rpc_method or None, "a2a_operation": a2a_operation or None}
    allowed = [str(m).upper() for m in api.get("allowed_methods") or []]
    if ANY_METHOD not in allowed and method not in allowed:
        return f"method {method} is not in allowed_methods {allowed}"
    for entry in api.get("denied_operations") or []:
        unnamed = denial_match(entry, method, operation_id, path, **rpc)
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
        operation_matches(entry, method, operation_id, path, **rpc) for entry in allowed_operations
    ):
        return "not in allowed_operations"
    return None


@dataclass(frozen=True)
class ApprovalGate:
    """The human approval an API's policy requires before a call is sent (``gated``)."""

    # "requester" and/or "role:<name>" entries of the rule that gates the call,
    # in the policy's order.
    approvers: tuple[str, ...]
    # Seconds a pending approval waits for a decision; then it expires (= rejected).
    timeout_s: int
    # The rule and the part of its required_for that gates the call, for messages
    # ("approval.required_for.methods ['POST']", "approval[1].required_for.operations (...)").
    rule: str
    # The gating rule's index when `approval` is a list of rules; None for one mapping.
    index: int | None = None
    # Later rules that also cover the call; they do not apply to it (the first one does).
    also: tuple[int, ...] = ()
    # How the requester decides: `direct`, or `relayed` by the agents `relayers` names.
    decide_with: str = DEFAULT_DECIDE_WITH
    relayers: tuple[str, ...] = ()

    def deciders(self) -> tuple[frozenset[str], str, frozenset[str]]:
        """Who decides, and how: what an approval is bound to (`rule_deciders`)."""
        return frozenset(self.approvers), self.decide_with, frozenset(self.relayers)


def approval_rules(api: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """An API's approval rules in file order: none, its one ``approval`` mapping, or its list."""
    approval = api.get(APPROVAL_KEY)
    if approval is None:
        return []
    return list(approval) if isinstance(approval, list) else [approval]


def rule_deciders(rule: Mapping[str, Any]) -> tuple[frozenset[str], str, frozenset[str]]:
    """Who decides the calls a rule gates, and how: its approvers, `decide_with` and
    relayers. Two rules with other deciders are two gates, and an approval taken under one
    does not cover the other's calls."""
    return (
        frozenset(str(a) for a in rule.get("approvers") or ()),
        str(rule.get("decide_with", DEFAULT_DECIDE_WITH)),
        frozenset(str(r) for r in rule.get("relayers") or ()),
    )


def describe_deciders(rule: Mapping[str, Any]) -> str:
    """``requester, role:ops``, or ``requester; relayed by concierge`` for a relayed rule."""
    approvers = ", ".join(str(a) for a in rule.get("approvers") or ())
    if rule.get("decide_with", DEFAULT_DECIDE_WITH) != DECIDE_RELAYED:
        return approvers
    return f"{approvers}; relayed by {', '.join(str(r) for r in rule.get('relayers') or ())}"


def approval_rule_label(api: Mapping[str, Any], index: int) -> str:
    """``approval`` for an API's one approval mapping, ``approval[<index>]`` in a list of rules."""
    if isinstance(api.get(APPROVAL_KEY), list):
        return f"{APPROVAL_KEY}[{index}]"
    return APPROVAL_KEY


class ApprovalRuleConflict(ValueError):
    """``gated``: the call cannot be given to one approval rule, so it is refused.

    The message says why (the rule that cannot rule the call out, the later
    rule with other approvers that covers it) and what to name. ``unnamed``
    is what the call leaves out (``"operation_id"`` or ``"path"``), ``index``
    and ``later`` are the two rules' indexes.
    """

    def __init__(self, message: str, *, unnamed: str, index: int, later: int) -> None:
        super().__init__(message)
        self.unnamed = unnamed
        self.index = index
        self.later = later


def _rule_match(
    rule: Mapping[str, Any],
    method: str,
    operation_id: str | None,
    path: str | None,
    rpc: Mapping[str, str | None] | None = None,
) -> tuple[str, str] | None:
    """How one approval rule covers the call: ``(part, unnamed)``, or None.

    ``part`` names what covers it, for messages. ``unnamed`` is ``""`` when
    the rule surely covers the call, else what the call leaves out
    (``"operation_id"``, ``"path"``) that the covering entry knows the
    operation by: the rule covers it only because it cannot be ruled out. A
    sure match wins over one that only cannot be ruled out.
    """
    required_for = rule.get("required_for") or {}
    methods = [str(m).upper() for m in required_for.get("methods") or []]
    if ANY_METHOD in methods or method in methods:
        return f"required_for.methods {methods}", ""
    unsure: tuple[str, str] | None = None
    for entry in required_for.get("operations") or []:
        unnamed = denial_match(entry, method, operation_id, path, **(rpc or {}))
        if unnamed is None:
            continue
        part = f"required_for.operations ({describe_operation(entry)})"
        if not unnamed:
            return part, ""
        if unsure is None:
            unsure = (part, unnamed)
    return unsure


def _unsure(part: str, unnamed: str) -> str:
    """``part``, and why it covers the call when the call only leaves out what it names."""
    return f"{part}: the call names no {unnamed}, so it cannot be ruled out" if unnamed else part


def rule_covers(
    rule: Mapping[str, Any],
    method: str,
    operation_id: str | None,
    path: str | None,
    *,
    rpc_method: str | None = None,
    a2a_operation: str | None = None,
) -> str | None:
    """Which part of one approval rule's ``required_for`` covers the call, or None.

    ``required_for.methods`` covers a call with one of its methods (``["*"]``:
    every method). An entry of ``required_for.operations`` covers a call as a
    denial does (``denial_match``: fail closed), not as an allow; an entry
    that surely covers it is named before one that only cannot rule it out.
    """
    rpc = {"rpc_method": rpc_method or None, "a2a_operation": a2a_operation or None}
    match = _rule_match(rule, method.upper(), operation_id or None, path or None, rpc)
    return None if match is None else _unsure(*match)


def gated(
    api: Mapping[str, Any],
    method: str,
    operation_id: str | None = None,
    path: str | None = None,
    *,
    template: str | None = None,
    rpc_method: str | None = None,
    a2a_operation: str | None = None,
) -> ApprovalGate | None:
    """The approval the API's policy (a validated one) requires before the call, or None.

    Ask it only about a call ``refusal_reason`` allows: approval never widens
    access, so a refused call stays refused whatever its gate, and denials
    still win. A rule covers the call when its ``required_for.methods`` holds
    the call's method (``["*"]``: every method), or when an entry of its
    ``required_for.operations`` covers it (``rule_covers``). Such an entry
    fails closed, as a denial does (``denial_match``), not as an allow: with
    its ``methods`` (when pinned) covering the call's method, its ``path``
    gates every call to that path whatever operation id the call names, its
    ``operationId`` gates the calls that name it, and a call that leaves out
    what the entry knows the operation by is gated too. Paths are compared
    normalised and ignoring letter case, and a literal segment also covers its
    dot-suffixed spellings (``cancel.json``, ``cancel.``), as for a denial.

    ``approval`` is one rule, or a list of rules: the FIRST rule in file order
    that covers the call gates it, with that rule's approvers and timeout, and
    the later rules that also cover it are listed in ``also`` (they do not
    apply to it). Failing closed across rules: when the first rule covers the
    call only because the call leaves out what the rule knows the operation by
    (no operation id, no path), and a later rule with other approvers also
    covers it, the call may be that later rule's, so neither rule's approvers
    get it: ``ApprovalRuleConflict`` is raised and the call is refused. At
    runtime, ask with the path that is sent and, when there is one, the
    ``template`` it was rendered from: a rule covers the call when it covers
    either (surely, when it surely covers either). On a JSON-RPC API an entry
    pinning ``rpc_method`` or ``a2a_operation`` covers the call as a denial
    does (``denial_match``), by the values derived from the request body.
    """
    rules = approval_rules(api)
    if not rules:
        return None
    method = method.upper()
    operation_id = operation_id or None
    path = path or None
    template = template or None
    rpc = {"rpc_method": rpc_method or None, "a2a_operation": a2a_operation or None}
    covering: list[tuple[int, str, str]] = []
    for index, rule in enumerate(rules):
        match = _rule_match(rule, method, operation_id, path, rpc)
        if template is not None and (match is None or match[1]):
            other = _rule_match(rule, method, operation_id, template, rpc)
            if other is not None and (match is None or not other[1]):
                match = other
        if match is not None:
            covering.append((index, *match))
    if not covering:
        return None
    index, part, unnamed = covering[0]
    rule = rules[index]
    approvers = tuple(str(a) for a in rule.get("approvers") or ())
    label = approval_rule_label(api, index)
    if unnamed:
        pin = f", or pin path and methods in {label}" if unnamed == "operation_id" else ""
        for later, _part, _unnamed in covering[1:]:
            if rule_deciders(rules[later]) != rule_deciders(rule):
                raise ApprovalRuleConflict(
                    f"{label} (approved by {describe_deciders(rule)}) covers it only because the "
                    f"call names no {unnamed} ({label}.{part}), and "
                    f"{approval_rule_label(api, later)} (approved by "
                    f"{describe_deciders(rules[later])}) also covers it: it could be either "
                    "rule's call, so neither rule's approvers are asked; name the "
                    f"{unnamed} on the call and in API_CALLS{pin}",
                    unnamed=unnamed,
                    index=index,
                    later=later,
                )
    listed = isinstance(api.get(APPROVAL_KEY), list)
    return ApprovalGate(
        approvers=approvers,
        timeout_s=int(rule.get("timeout_s", DEFAULT_APPROVAL_TIMEOUT_S)),
        rule=f"{label}.{_unsure(part, unnamed)}",
        index=index if listed else None,
        also=tuple(i for i, _, _ in covering[1:]),
        decide_with=str(rule.get("decide_with", DEFAULT_DECIDE_WITH)),
        relayers=tuple(str(r) for r in rule.get("relayers") or ()),
    )


class RpcRequestError(ValueError):
    """A request a JSON-RPC API (``protocol: jsonrpc|a2a``) refuses to send (``derive_rpc``)."""


@dataclass(frozen=True)
class RpcCall:
    """What a request to a JSON-RPC API is, read from its body (``derive_rpc``).

    ``rpc_method``: the JSON-RPC method of a POST (an A2A 0.3 name read as its 1.0
    name under ``protocol: a2a``); None for GET and HEAD, and for any call to an
    ``http`` API. ``a2a_operation``: under ``protocol: a2a``, ``approve`` or
    ``reject`` for a message that decides a pending approval, else None.
    """

    rpc_method: str | None = None
    a2a_operation: str | None = None


def canonical_rpc_method(protocol: str, name: str) -> str:
    """``name`` as the policy compares it: an A2A 0.3 name as its 1.0 name under a2a."""
    return A2A_V03_METHODS.get(name, name) if protocol == PROTOCOL_A2A else name


def _is_rpc_id(value: Any) -> bool:
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


def _names_approval(data: Any) -> bool:
    """Whether a message part's data names an approval (as the called agent reads it)."""
    return isinstance(data, Mapping) and ("approval_id" in data or "decision" in data)


def _a2a_operation(protocol: str, params: Any) -> str | None:
    """What an A2A message decides: ``reject`` only when every part that names an approval
    says ``reject``, ``approve`` when any other does (approve wins), None when none does."""
    message = params.get("message") if isinstance(params, Mapping) else None
    parts = message.get("parts", []) if isinstance(message, Mapping) else None
    if not isinstance(parts, list):
        raise RpcRequestError(
            f"protocol {protocol}: a message request needs params.message with a list of parts"
        )
    decisions = [
        part["data"].get("decision")
        for part in parts
        if isinstance(part, Mapping) and _names_approval(part.get("data"))
    ]
    if not decisions:
        return None
    return A2A_REJECT if all(d == A2A_REJECT for d in decisions) else A2A_APPROVE


def _rpc_body_problem(sent: Any) -> str | None:
    """Why a parsed JSON body is not one JSON-RPC 2.0 request object, or None."""
    if isinstance(sent, list):
        return "a batch"
    if not isinstance(sent, dict):
        return "not a JSON-RPC request object"
    if set(sent) - set(_JSONRPC_KEYS):
        return "members other than jsonrpc, method, params and id"
    if sent.get("jsonrpc") != "2.0":
        return 'jsonrpc is not "2.0"'
    if not isinstance(sent.get("method"), str) or not sent["method"]:
        return "no method name"
    if "id" not in sent:
        return "a notification (no id)"
    if not _is_rpc_id(sent["id"]):
        return "an id that is not a string or an integer"
    if "params" in sent and not isinstance(sent["params"], dict | list):
        return "params that are not an object or an array"
    return None


def derive_rpc(api: Mapping[str, Any], method: str, body: Any) -> RpcCall:
    """What a request to ``api`` is, read from the body sent (never from the tool's labels).

    Nothing for an ``http`` API. For ``protocol: jsonrpc|a2a``: a POST must send
    one JSON-RPC 2.0 request object (``jsonrpc: "2.0"``, a method name, an ``id``
    that is a string or an integer, optional ``params`` that are an object or an
    array, and no other member), read as the server reads the JSON sent; a
    batch, a notification (no ``id``), a body that is not plain JSON or any
    other body raises ``RpcRequestError``, as does a GET or HEAD with a body.
    Its ``rpc_method`` is the request's method (under ``a2a``, an A2A 0.3 name as
    its 1.0 name). Under ``a2a``, a ``SendMessage`` or ``SendStreamingMessage``
    whose parts name an approval (a data part with ``approval_id`` or
    ``decision``) is ``a2a_operation: reject`` only when every such part says
    ``reject``, and ``approve`` otherwise: failing closed, approve wins. A
    message method in another letter case (``sendmessage``) is read for a
    decision too, though an A2A server answers it as an unknown method.
    """
    protocol = api_protocol(api)
    if protocol not in RPC_PROTOCOLS:
        return RpcCall()
    method = method.upper()
    if method != "POST":
        if body is not None:
            raise RpcRequestError(
                f"protocol {protocol}: a {method} sends no body (a JSON-RPC request is a POST)"
            )
        return RpcCall()
    try:
        # The JSON the server reads: tuples become lists, keys strings (never trust a
        # Python object that serializes as something other than it looks).
        sent = json.loads(json.dumps(body, allow_nan=False))
    except (TypeError, ValueError):
        sent = None
        problem: str | None = "not plain JSON"
    else:
        problem = _rpc_body_problem(sent)
    if problem is not None:
        raise RpcRequestError(
            f"protocol {protocol} sends one JSON-RPC request per call (a batch or non-request "
            f"body refused: {problem})"
        )
    name = canonical_rpc_method(protocol, sent["method"])
    if protocol != PROTOCOL_A2A or name.casefold() not in _A2A_MESSAGE_NAMES:
        return RpcCall(rpc_method=name)
    return RpcCall(rpc_method=name, a2a_operation=_a2a_operation(protocol, sent.get("params")))


def _operation_entries(api: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Every operation entry of an API: allowed, denied and those its approval rules gate."""
    entries = [*(api.get("allowed_operations") or []), *(api.get("denied_operations") or [])]
    for rule in approval_rules(api):
        entries.extend((rule.get("required_for") or {}).get("operations") or [])
    return [entry for entry in entries if isinstance(entry, Mapping)]


def label_problem(
    api: Mapping[str, Any],
    operation_id: str | None,
    rpc_method: str | None = None,
    a2a_operation: str | None = None,
) -> str | None:
    """Why a tool's ``operation_id`` does not name the request it labels, or None.

    On a JSON-RPC API, an entry that pins ``operationId`` with ``rpc_method``
    or ``a2a_operation`` says what a call so labelled is. A call labelled so
    whose request (``derive_rpc``) is something else is refused, so a label
    never carries a decision past a rule written for another request.
    Operation ids and JSON-RPC methods are compared ignoring letter case.
    """
    if not operation_id or api_protocol(api) not in RPC_PROTOCOLS:
        return None
    label = str(operation_id).casefold()
    for entry in _operation_entries(api):
        pinned_id = entry.get("operationId")
        if pinned_id is None or str(pinned_id).casefold() != label:
            continue
        pinned_rpc = entry.get(RPC_METHOD_KEY)
        if pinned_rpc is not None and (
            rpc_method is None or str(pinned_rpc).casefold() != str(rpc_method).casefold()
        ):
            return (
                f"operation_id {operation_id!r} does not match the request (rpc_method "
                f"{rpc_method or 'none'}); refused"
            )
        pinned_operation = entry.get(A2A_OPERATION_KEY)
        if pinned_operation is not None and pinned_operation != a2a_operation:
            return (
                f"operation_id {operation_id!r} does not match the request (a2a_operation "
                f"{a2a_operation or 'none'}); refused"
            )
    return None


# --- END SHARED API POLICY RULES ---

# ---------------------------------------------------------------------------
# CLI-only helpers
# ---------------------------------------------------------------------------

MANIFEST_KEY = "api_policy"
LEGACY_POLICY_FILENAME = "product-policy.yaml"
LEGACY_MANIFEST_KEY = "product_api"
LEGACY_CALLS_NAME = "PRODUCT_CALLS"
CALLS_NAME = "API_CALLS"


class ApiPolicyFileError(click.ClickException):
    """An ``api-policy.yaml`` that cannot be read or breaks the schema (exit 3)."""

    exit_code = 3

    def __init__(self, path: str | Path, errors: list[str]) -> None:
        self.path = Path(path)
        self.errors = list(errors)
        lines = "\n".join(f"  - {e}" for e in self.errors)
        super().__init__(f"Invalid API policy {self.path}:\n{lines}")


class LegacyApiPolicyError(click.ClickException):
    """The project still uses the retired product API policy (exit 3)."""

    exit_code = 3


class ApiPolicyConfigError(click.ClickException):
    """The manifest names a policy file the agent would not load (exit 3)."""

    exit_code = 3


def load_policy_document(path: str | Path) -> dict[str, Any]:
    """Read and validate an api-policy file; raise ``ApiPolicyFileError`` listing every problem."""
    policy_path = Path(path)
    try:
        text = policy_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ApiPolicyFileError(policy_path, [f"cannot read the file: {exc}"]) from exc
    data, errors = parse_policy_yaml(text)
    errors = errors or policy_errors(data)
    if errors:
        raise ApiPolicyFileError(policy_path, errors)
    return dict(data)


def manifest_policy_file_problem(value: Any, manifest_name: str) -> str | None:
    """Why the manifest's ``api_policy.policy_file`` cannot be used, or None.

    The agent loads ``api-policy.yaml`` from the project root (or
    ``API_POLICY_PATH``) and the Dockerfiles copy only that file, so a manifest
    naming another file would make ``lint`` check a file the agent never
    enforces.
    """
    if not value or Path(str(value)) == Path(POLICY_FILENAME):
        return None
    return (
        f"api_policy.policy_file in {manifest_name} is {str(value)!r}, but the agent loads "
        f"{POLICY_FILENAME} from the project root (the Dockerfiles copy only that file), so "
        f"lint would check a file the agent never enforces. Rename the file to "
        f"{POLICY_FILENAME} and set api_policy: {{policy_file: {POLICY_FILENAME}}}."
    )


@dataclass(frozen=True)
class ApiSummary:
    """What the templates need to know about one declared API."""

    name: str
    base_url_env: str
    auth: str
    token_env: str = ""

    def as_context(self) -> dict[str, str]:
        return {
            "name": self.name,
            "base_url_env": self.base_url_env,
            "auth": self.auth,
            "token_env": self.token_env,
        }


def summarize(document: Mapping[str, Any]) -> tuple[ApiSummary, ...]:
    """One summary per declared API, in file order (the document must be valid)."""
    return tuple(
        ApiSummary(
            name=str(name),
            base_url_env=str(api["base_url_env"]),
            auth=str(api["auth"]),
            token_env=str(api.get("token_env") or ""),
        )
        for name, api in document["apis"].items()
    )


# Methods whose example call sends a JSON body (the example tool takes a `body` argument).
BODY_METHODS = ("POST", "PUT", "PATCH")


@dataclass(frozen=True)
class ExampleCall:
    """The call that the rendered ``tools/example_api.py`` makes.

    Chosen when the project is rendered (``dev.policy_check.example_call``):
    the first operation the policy's first API allows, whatever its method, so
    the example passes ``lint`` and the project's policy test from the first
    commit.
    """

    api: str
    method: str
    path: str
    operation_id: str | None = None

    @property
    def params(self) -> tuple[str, ...]:
        """The path's ``{name}`` placeholders in order, each once: the tool's parameters."""
        return tuple(dict.fromkeys(_PLACEHOLDER_NAME_RE.findall(self.path)))

    @property
    def has_body(self) -> bool:
        """True when the call sends a JSON body (POST, PUT, PATCH)."""
        return self.method in BODY_METHODS

    def as_context(self) -> dict[str, Any]:
        return {
            "api": self.api,
            "method": self.method,
            "operation_id": self.operation_id or "",
            "path": self.path,
            "params": list(self.params),
            "has_body": self.has_body,
        }


_PLACEHOLDER_NAME_RE = re.compile(r"\{([^/{}]+)\}")


def _effective_rule(rule: Mapping[str, Any]) -> dict[str, Any]:
    required_for = rule["required_for"]
    effective: dict[str, Any] = {}
    if "methods" in required_for:
        effective["methods"] = [str(m).upper() for m in required_for["methods"]]
    if "operations" in required_for:
        effective["operations"] = [dict(entry) for entry in required_for["operations"]]
    out: dict[str, Any] = {
        "required_for": effective,
        "approvers": [str(a) for a in rule["approvers"]],
        "timeout_s": int(rule.get("timeout_s", DEFAULT_APPROVAL_TIMEOUT_S)),
    }
    # How the requester decides, when the rule says (absent: `direct`).
    if "decide_with" in rule:
        out["decide_with"] = str(rule["decide_with"])
    if "relayers" in rule:
        out["relayers"] = [str(r) for r in rule["relayers"]]
    return out


def effective_approval(api: Mapping[str, Any]) -> dict[str, Any] | list[dict[str, Any]] | None:
    """An API's ``approval`` (the API must be valid) with defaults filled in, or None.

    Shaped as the file writes it: one rule ``{"required_for": {"methods": [...],
    "operations": [...]}, "approvers": [...], "timeout_s": N}``, or a list of
    such rules; ``required_for`` holds only the keys the policy sets, methods
    upper-cased. ``decide_with`` and ``relayers`` are there when the rule sets
    them (absent: ``direct``).
    """
    approval = api.get(APPROVAL_KEY)
    if approval is None:
        return None
    if isinstance(approval, list):
        return [_effective_rule(rule) for rule in approval]
    return _effective_rule(approval)


def effective_approval_rules(api: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every approval rule of a valid API in file order, defaults filled in (empty: none).

    Each is ``_effective_rule``'s mapping plus ``rule``, its name in messages:
    ``approval`` for the one mapping, ``approval[<index>]`` in a list.
    """
    return [
        {"rule": approval_rule_label(api, index), **_effective_rule(rule)}
        for index, rule in enumerate(approval_rules(api))
    ]


def gate_payload(gate: ApprovalGate | None) -> dict[str, Any] | None:
    """A gate as JSON-ready data (``api show --json``), or None.

    ``rule_index`` is the gating rule's index in a list of rules (None for one
    mapping); ``also_covered_by`` names the later rules that also cover the
    call, which do not apply to it. A relayed gate adds ``decide_with`` and
    ``relayers`` (a gate without them is ``direct``).
    """
    if gate is None:
        return None
    payload: dict[str, Any] = {
        "approvers": list(gate.approvers),
        "timeout_s": gate.timeout_s,
        "rule": gate.rule,
        "rule_index": gate.index,
        "also_covered_by": [f"{APPROVAL_KEY}[{i}]" for i in gate.also],
    }
    if gate.decide_with != DEFAULT_DECIDE_WITH:
        payload["decide_with"] = gate.decide_with
        payload["relayers"] = list(gate.relayers)
    return payload


def describe_gate(gate: ApprovalGate) -> str:
    """``requester, role:ops (approval.required_for.methods ['POST']; expires after 900 s)``.

    With overlapping rules it also names the later ones that cover the call
    and says they do not apply (the first rule in file order gates it).
    """
    also = ""
    if gate.also:
        later = ", ".join(f"{APPROVAL_KEY}[{i}]" for i in gate.also)
        also = f"; also covered by {later}, which does not apply: the first rule gates the call"
    deciders = describe_deciders(
        {"approvers": gate.approvers, "decide_with": gate.decide_with, "relayers": gate.relayers}
    )
    return f"{deciders} ({gate.rule}; expires after {gate.timeout_s} s{also})"


def _rule_methods(rule: Mapping[str, Any]) -> set[str]:
    """The methods one rule's ``required_for.methods`` gates outright (``"*"``: every one)."""
    methods = {str(m).upper() for m in (rule.get("required_for") or {}).get("methods") or []}
    return set(HTTP_METHODS) if ANY_METHOD in methods else methods


def _entry_covers(earlier: Mapping[str, Any], later: Mapping[str, Any]) -> bool:
    """Whether gate entry ``earlier`` covers every call gate entry ``later`` covers.

    Entries cover calls as denials do (``denial_match``): with the same
    operationId and path, the one pinning at least the other's methods (none:
    every method) covers the same calls, and more.
    """
    if earlier.get("operationId") != later.get("operationId"):
        return False
    # JSON-RPC entries (0.3) cover by the request's method and decision: the same ones.
    if any(earlier.get(key) != later.get(key) for key in (RPC_METHOD_KEY, A2A_OPERATION_KEY)):
        return False
    paths = [earlier.get("path"), later.get("path")]
    if (paths[0] is None) != (paths[1] is None):
        return False
    if paths[0] is not None and normalize_path(str(paths[0])) != normalize_path(str(paths[1])):
        return False
    if not earlier.get("methods"):
        return True
    if not later.get("methods"):
        return False
    return {str(m).upper() for m in earlier["methods"]} >= {
        str(m).upper() for m in later["methods"]
    }


def rule_never_applies(api: Mapping[str, Any], index: int) -> bool:
    """Whether every call approval rule ``index`` covers is covered by an earlier rule.

    The first rule in file order that covers a call gates it, so such a rule
    never gates anything: its approvers never decide a call. Sound, not
    complete: True only when earlier methods, or equal or wider earlier
    entries, provably cover each part of it.
    """
    rules = approval_rules(api)
    if not 0 < index < len(rules):
        return False
    earlier = rules[:index]
    methods = set().union(*(_rule_methods(r) for r in earlier))
    rule = rules[index]
    if not _rule_methods(rule) <= methods:
        return False
    earlier_entries = [
        entry for r in earlier for entry in (r.get("required_for") or {}).get("operations") or []
    ]
    for entry in (rule.get("required_for") or {}).get("operations") or []:
        pinned = {str(m).upper() for m in entry.get("methods") or []} or set(HTTP_METHODS)
        if pinned <= methods or any(_entry_covers(e, entry) for e in earlier_entries):
            continue
        return False
    return True


def _rule_cover_methods(rule: Mapping[str, Any]) -> set[str]:
    """Every method some call one rule covers may have (its methods and its entries')."""
    methods = _rule_methods(rule)
    for entry in (rule.get("required_for") or {}).get("operations") or []:
        methods |= {str(m).upper() for m in entry.get("methods") or []} or set(HTTP_METHODS)
    return methods


def rule_conflicts(api: Mapping[str, Any]) -> list[tuple[int, list[Mapping[str, Any]], list[int]]]:
    """Rules that may leave a call no operation id names to either of two approver sets.

    ``gated`` refuses a call that the first covering rule covers only because
    the call names no operation id (an entry pinning ``operationId`` without
    ``path``) when a later rule with other approvers (or approvers who decide
    otherwise: ``decide_with``, ``relayers``) also covers it. For each such
    rule: its index, those entries, and the later rules with other deciders
    that may cover the same calls (by method; conservative).
    """
    rules = approval_rules(api)
    allowed = {str(m).upper() for m in api.get("allowed_methods") or []}
    allowed = set(HTTP_METHODS) if ANY_METHOD in allowed else allowed
    found = []
    sure: set[str] = set()  # methods an earlier (or this) rule gates outright
    for index, rule in enumerate(rules):
        sure |= _rule_methods(rule)
        deciders = rule_deciders(rule)
        entries = [
            entry
            for entry in (rule.get("required_for") or {}).get("operations") or []
            # A JSON-RPC entry never leaves a call unnamed (every POST names its method).
            if entry.get("operationId") is not None
            and entry.get("path") is None
            and not _rpc_pins(entry)
        ]
        methods = {
            m
            for entry in entries
            for m in ({str(x).upper() for x in entry.get("methods") or []} or set(HTTP_METHODS))
        } & (allowed - sure)
        later = [
            j
            for j in range(index + 1, len(rules))
            if rule_deciders(rules[j]) != deciders and methods & _rule_cover_methods(rules[j])
        ]
        if entries and methods and later:
            found.append((index, entries, later))
    return found


def approval_notes(name: str, api: Mapping[str, Any]) -> list[str]:
    """What an API's valid ``approval`` names that can never take effect, or refuses.

    Approval never widens access, so a gate on a method outside
    ``allowed_methods`` changes nothing: those calls stay refused. In a list
    of rules, a rule whose every call an earlier rule covers first never
    gates anything (rules apply in file order), and a rule that names
    operations by ``operationId`` alone, before a rule with other approvers,
    has the calls that name no operation id and that both may cover refused
    (``rule_conflicts``).
    """
    notes: list[str] = []
    for index, entries, later in rule_conflicts(api):
        label = approval_rule_label(api, index)
        others = ", ".join(
            f"{approval_rule_label(api, j)} (approved by "
            f"{describe_deciders(approval_rules(api)[j])})"
            for j in later
        )
        named = "; ".join(describe_operation(entry) for entry in entries)
        notes.append(
            f"apis.{name}.{label} names operations by operationId alone ({named}), so it "
            f"cannot rule out a call that names no operation_id, and {others} may also cover "
            "such a call: the agent refuses it (it could be either rule's call). Name the "
            f"operation_id on every call to {name} and in API_CALLS, or pin path and methods "
            f"in {label} (api approval --operations pins them from the API's openapi: spec)"
        )
    allowed = [str(m).upper() for m in api.get("allowed_methods") or []]
    for index, rule in enumerate(approval_rules(api)):
        label = approval_rule_label(api, index)
        if ANY_METHOD not in allowed:
            required_for = rule["required_for"]
            named = [str(m).upper() for m in required_for.get("methods") or []]
            for entry in required_for.get("operations") or []:
                named.extend(str(m).upper() for m in entry.get("methods") or [])
            outside = [m for m in dict.fromkeys(named) if m != ANY_METHOD and m not in allowed]
            if outside:
                notes.append(
                    f"apis.{name}.{label} gates {', '.join(outside)}, which allowed_methods does "
                    "not allow: approval never widens access, so those calls stay refused"
                )
        if rule_never_applies(api, index):
            notes.append(
                f"apis.{name}.{label} never gates a call: an earlier rule covers every call it "
                "covers, and the first rule in file order gates a call. Move it above that rule, "
                "narrow the earlier rule, or remove it"
            )
    return notes


def read_policy_document(path: str | Path | None) -> dict[str, Any] | None:
    """An existing project policy, validated; None (with a warning) when absent or invalid.

    Used when a project is re-rendered: the policy belongs to the project and
    ``lint`` reports its problems, so a broken file must not stop the re-render.
    """
    if not path or not Path(path).is_file():
        return None
    try:
        return load_policy_document(path)
    except ApiPolicyFileError as exc:
        logging.warning("%s", exc.format_message())
        return None


def read_summaries(path: str | Path | None) -> tuple[ApiSummary, ...]:
    """Summaries of an existing project policy; empty (with a warning) when unreadable."""
    document = read_policy_document(path)
    return summarize(document) if document is not None else ()


def bearer_token_envs(summaries: tuple[ApiSummary, ...] | list[ApiSummary]) -> list[str]:
    """The ``token_env`` of every ``auth: bearer`` API, first occurrence first."""
    envs: list[str] = []
    for summary in summaries:
        if summary.auth == "bearer" and summary.token_env and summary.token_env not in envs:
            envs.append(summary.token_env)
    return envs


# `auth: exchange` (RFC 8693): the issuer's token endpoint and this agent's client there. The
# URL and the client id are plain settings (.env, the chart's values); the secret joins
# `secrets.keys`, so it reaches the Secret and never the values files.
TOKEN_EXCHANGE_URL_ENV = "TOKEN_EXCHANGE_URL"
TOKEN_EXCHANGE_CLIENT_ID_ENV = "TOKEN_EXCHANGE_CLIENT_ID"
TOKEN_EXCHANGE_SECRET_ENV = "TOKEN_EXCHANGE_CLIENT_SECRET"


def uses_exchange(summaries: tuple[ApiSummary, ...] | list[ApiSummary]) -> bool:
    """Whether an API of the policy uses ``auth: exchange``."""
    return any(summary.auth == "exchange" for summary in summaries)


def secret_envs(summaries: tuple[ApiSummary, ...] | list[ApiSummary]) -> list[str]:
    """The secrets the policy's APIs need in ``secrets.keys``, first occurrence first.

    Every ``auth: bearer`` API's ``token_env``, and ``TOKEN_EXCHANGE_CLIENT_SECRET``
    once when an API uses ``auth: exchange``.
    """
    envs = bearer_token_envs(summaries)
    if uses_exchange(summaries) and TOKEN_EXCHANGE_SECRET_ENV not in envs:
        envs.append(TOKEN_EXCHANGE_SECRET_ENV)
    return envs


def forward_runtime_problem(
    summaries: tuple[ApiSummary, ...] | list[ApiSummary], runtime: str
) -> str | None:
    """Why ``auth: forward`` or ``auth: exchange`` cannot be used with ``runtime``, or None."""
    carrying = [s for s in summaries if s.auth in HEADER_AUTH_MODES]
    if runtime != "langgraph-server" or not carrying:
        return None
    modes = [mode for mode in HEADER_AUTH_MODES if any(s.auth == mode for s in carrying)]
    stored = (
        "forwarded credentials" if modes == ["forward"] else "the caller's credentials (tokens)"
    )
    return (
        f"{' and '.join(f'auth: {mode}' for mode in modes)} (apis: "
        f"{', '.join(s.name for s in carrying)}) is not supported with runtime "
        f"langgraph-server: LangGraph Server persists the run context, so {stored} would be "
        "stored. Use auth: bearer or none, or the fastapi runtime."
    )


def auth_policy_findings(
    document: Mapping[str, Any], auth_policy: str
) -> tuple[list[str], list[str]]:
    """The APIs that act with the caller's identity against the project's auth policy.

    Returns ``(errors, notes)``, the compatibility matrix of `lint` and `api add`:

    * ``auth: exchange`` under ``shared-bearer``: an error (there is no user token
      to exchange).
    * ``auth: forward`` under ``shared-bearer``: an error (there is no user
      credential to forward: every caller is the one principal ``shared``).
    * ``auth: forward`` under ``jwt`` without ``forward_audience``: an error (jwt
      sets no per-API credential); with it, a note to prefer ``auth: exchange``.
    * ``custom``: every mode is the policy's to serve (``keep_subject_token`` for
      exchange, ``attributes["credentials"]`` for forward).
    * ``auth: exchange`` with ``exchange.allow_actorless: true`` (``jwt`` or
      ``custom``): a note naming what the agent behind the API must set, since
      the calling agent then sends tokens that name no actor.
    """
    apis = document.get("apis") or {}
    exchange = [str(n) for n, a in apis.items() if a.get("auth") == "exchange"]
    forward = [str(n) for n, a in apis.items() if a.get("auth") == "forward"]
    actorless = [
        str(n)
        for n, a in apis.items()
        if a.get("auth") == "exchange"
        and isinstance(a.get(EXCHANGE_KEY), Mapping)
        and a[EXCHANGE_KEY].get(ALLOW_ACTORLESS_KEY) is True
    ]
    errors: list[str] = []
    notes: list[str] = []
    if actorless and auth_policy != "shared-bearer":
        notes.append(
            f"auth: exchange (apis: {', '.join(actorless)}) sets exchange.allow_actorless: this "
            "agent sends exchanged tokens that name no actor, so the agent behind each must set "
            "AUTH_JWT_DIRECT_CLIENTS to the clients people sign in with and list this agent in "
            "AUTH_ALLOWED_ACTORS as client:<its client id>; without them it reads this agent's "
            "calls as the person's own, and this agent could decide the person's approvals there"
        )
    if auth_policy == "shared-bearer":
        if exchange:
            errors.append(
                f"auth: exchange (apis: {', '.join(exchange)}) is not supported with the "
                "shared-bearer auth policy: shared-bearer has no user token to exchange; use "
                "auth: bearer with the peer's agent key"
            )
        if forward:
            errors.append(
                f"auth: forward (apis: {', '.join(forward)}) is not supported with the "
                "shared-bearer auth policy: there is no user credential to forward (every "
                "caller is the one principal `shared`); use auth: bearer or none"
            )
    elif auth_policy == "jwt":
        unaimed = [n for n in forward if "forward_audience" not in apis[n]]
        aimed = [n for n in forward if "forward_audience" in apis[n]]
        if unaimed:
            errors.append(
                f"auth: forward (apis: {', '.join(unaimed)}) needs forward_audience with the jwt "
                "auth policy: jwt sets no per-API credential, and forwards the caller's own "
                "token only to an audience the issuer minted it for; prefer auth: exchange"
            )
        if aimed:
            notes.append(
                f"auth: forward (apis: {', '.join(aimed)}): prefer auth: exchange; forward sends "
                "the caller's own token and needs one minted for both audiences"
            )
    return errors, notes


def legacy_findings(project_dir: str | Path) -> list[str]:
    """What still uses the retired product API policy in ``project_dir``."""
    root = Path(project_dir)
    findings: list[str] = []
    if (root / LEGACY_POLICY_FILENAME).is_file():
        findings.append(f"{LEGACY_POLICY_FILENAME} exists")
    manifest = root / "graph-agents-cli-manifest.yaml"
    if manifest.is_file():
        try:
            data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            data = None
        if isinstance(data, Mapping) and LEGACY_MANIFEST_KEY in data:
            findings.append(f"the manifest has a {LEGACY_MANIFEST_KEY}: block")
    return findings


def legacy_migration_message(findings: list[str]) -> str:
    """How to move a project from the retired product API policy to api-policy.yaml."""
    return (
        "This project uses the retired product API policy ("
        + "; ".join(findings)
        + "). Migrate it to api-policy.yaml, then re-run the command:\n"
        f"  1. Rename {LEGACY_POLICY_FILENAME} to {POLICY_FILENAME}.\n"
        f"  2. Replace its top-level `{LEGACY_MANIFEST_KEY}:` with `apis:` and move the fields\n"
        "     under an API name, adding the now required allowed_methods:\n"
        "       apis:\n"
        "         example:\n"
        "           base_url_env: EXAMPLE_API_BASE_URL\n"
        "           auth: bearer              # none | bearer | forward (was forwarded-session)\n"
        "           token_env: EXAMPLE_API_TOKEN\n"
        "           allowed_methods: [GET, POST]  # list every method it may use\n"
        f"  3. In graph-agents-cli-manifest.yaml replace `{LEGACY_MANIFEST_KEY}:` with\n"
        f"     `{MANIFEST_KEY}: {{policy_file: {POLICY_FILENAME}}}`.\n"
        f"  4. In every tool module rename {LEGACY_CALLS_NAME} to {CALLS_NAME}, add\n"
        '     "api": "<name>" to each entry, and call the API through\n'
        '     app_utils.api_client.get_client("<name>").'
    )


def ensure_no_legacy_api_policy(project_dir: str | Path) -> None:
    """Raise ``LegacyApiPolicyError`` (exit 3) when the project uses the retired format."""
    findings = legacy_findings(project_dir)
    if findings:
        raise LegacyApiPolicyError(legacy_migration_message(findings))
