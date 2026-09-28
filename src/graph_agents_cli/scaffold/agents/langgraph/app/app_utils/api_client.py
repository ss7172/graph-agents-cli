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
(`auth: none | bearer | forward | exchange`) and which methods and operations
are allowed. `get_client(name)` returns a client for one declared API; every
request outside the policy raises `ApiPolicyError` before anything is sent.

Fail closed: without a policy file, with an invalid one, or for an API the
file does not declare, `get_client` raises `ApiPolicyError`; there is no
unrestricted fallback. `auth: bearer` sends `Authorization: Bearer
$<token_env>`. `auth: forward` sends the calling principal's own credential for
that API, `principal.attributes["credentials"][<name>]`, in `forward_header`
(default `Authorization`), or, with `forward_audience`, the caller's own
verified bearer token when it was minted for that audience too; the principal
comes from the run context the server sets for the graph run, and nothing is
sent when the caller has no credential. `auth: exchange` sends `Bearer
<token>` in `forward_header`, a token the issuer mints for the API's
`exchange.audience` in exchange for the caller's own (RFC 8693,
`token_exchange.py`): asked for just before the call is sent, after every
check, the approval and the limits, and never for a refused or paused call.
An exchanged token that names no actor (no `act` claim, or one that is not a
readable JWT) is refused and nothing is sent, unless the API sets
`exchange.allow_actorless: true`.
A call to such an API that would loop back to this agent, or to an agent
already in the request's delegation chain, is refused before anything else.

An API's `protocol` (`http` by default) says how its calls are judged. For
`protocol: jsonrpc` (a JSON-RPC 2.0 API) and `protocol: a2a` (another agent,
over A2A 1.0 JSON-RPC), every POST must send one JSON-RPC request object, and
the client reads what it is from the body sent, never from the tool
(`derive_rpc`): its method (`rpc_method`; under `a2a` an A2A 0.3 name is read
as its 1.0 name) and, for an A2A message whose parts name an approval, what it
decides (`a2a_operation`: `reject` only when every such part rejects, else
`approve`). Operation entries may pin `rpc_method` and `a2a_operation`: an
allow must match them, and a denial or approval gate naming them covers every
call they describe, whatever its path or label. A tool's `operation_id` that
names an entry pinning another method or decision is refused
(`label_problem`), as is a message that approves without an approval gate or
a denial holding it: an agent never decides, on its own, an approval the agent
it calls waits for.

Every method the policy allows can be sent (`request()`, or `get`, `head`,
`post`, `put`, `patch`, `delete`, `options`), with a JSON body, query
parameters and extra headers. The app adds its correlation headers to every
call of another agent (`protocol: a2a`) or of an `auth: forward` or `auth:
exchange` API, and of no other (`set_outbound_headers`: the request id and,
under OTLP tracing, the W3C trace context; `propagates`), unless the tool sets
the same header; they differ per request, so an approval does not bind them. An API's optional `limits` cap the calls before
they are sent: `max_calls_per_run` counts the calls to that API within one
agent run (the run id of the LangGraph run, else the request's; calls made
outside any run share one count), and `rate_per_minute` is a token bucket per
process, so each replica allows that rate. Counters are dropped when a `/chat`
or A2A run ends (`end_run`), and otherwise (LangGraph Server runs included)
after `RUN_COUNTER_TTL_S` without a call or beyond `MAX_TRACKED_RUNS` runs, so
memory does not grow across runs. `limits.max_response_bytes` caps each answer:
the body is read (decoded) up to that many bytes and discarded past it, and the
call fails; unset, answers are not capped.

An API's optional `approval` block names the calls a human must approve before
they are sent (`gated`, `ApiPolicy.gate`). It is one rule, or a list of rules
when different calls need different approvers: the first rule in file order
that covers a call gates it, and the approval is asked of that rule's approvers
(they are recorded with it and decide it). A call the first rule covers only
because it names no operation id, and that a later rule with other approvers
also covers, is refused: it could be either rule's call. Approval never widens
access: a gated call must pass the policy first, and denials still win. A gated
call pauses the agent run before anything is sent: the client describes the
exact request (`canonical_call`: API, method, URL with the rendered path,
query, JSON body, operation id and the tool's own headers), hashes it
(`call_hash`) and calls LangGraph's `interrupt()` with the approval payload
(the call, with the fields named in `redact=` masked, the tool and the model's
stated purpose, the approvers). The chat runtime records the approval and ends
the stream awaiting a decision (see `approvals.py`). When the run resumes, the
tool runs again from its start and this client rebuilds the request: it is sent
only when the decision approves exactly this request (the same hash) and the
approvals ledger marks that approval used (`set_approval_ledger`), so an
approval is sent once, never replayed. A rejected or expired approval, a
request that changed after it was approved, a used approval, or a gated call
made outside an agent run (nothing can pause it) raises `ApiPolicyError` and
sends nothing. After an approved call was sent, a second gated call in the same
tool call is refused (on its resume the tool would run again and meet the
first, already used, approval): make it in a new tool call. Code before a gated
call runs again on resume, so keep other side effects after it.

A decision is bound to the call it was taken for, not to the policy of the
moment: when the resumed tool rebuilds a request to the same API, method and
path as the paused call (and, on a JSON-RPC API, the same JSON-RPC method and
A2A decision, so a read sent first on resume does not take an approve
message's decision, and a message that rejects is not stopped by the
rejection it reports), the decision applies whatever the policy now says
about gating it (`_decision_waiting`, `call_identity`). A rejected or expired call is never
sent, even when the policy no longer gates it (a new image while the call
waited, or a typo that un-gates it); a call whose approval is still pending
pauses again for that same approval. An approved call is sent only when the
current policy still allows it (a later denial, or narrower
`allowed_methods`/`allowed_operations`, refuses it first), still gates it with
the same approvers (those of the rule that gates it now: a reordered or edited
list of rules that hands the call to other approvers does not keep the
approval), and the request is exactly the approved one; otherwise
nothing is sent and the model is told to ask again. A call a decision stopped
stays stopped for the rest of that tool call.

The ledger binds a decision to its call too, for a tool call that runs again
with no decision (LangGraph Server's own API can continue a paused run
without input, or replay it from a checkpoint, and a copied thread keeps its
tool calls): before a call is sent without a decision waiting for it, the
ledger is asked for the approvals recorded for this tool call (the model
message that made it and its call id, or the task's interrupt), and a call
one was asked for is refused, gated or not now: a rejected, expired or
pending one is not sent, and an approved one is sent only by the run its
decision resumed, once (`_bound_approvals`). Keep the agent's middleware
(`tool_call_scope`), which names the tool call.

Every tool module declares `API_CALLS`, a module-level list of
`{"api", "method", "operation_id", "path"}` dicts naming each call it makes
(plus `rpc_method`, and `a2a_operation` for a message that decides, on a
JSON-RPC API); `graph-agents-cli lint` checks those declarations against the
same rules.
"""

from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple, Protocol
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
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: str | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        # Why nothing was sent (a fixed phrase, never a value), for the log line.
        self.reason = reason


class ResponseTooLarge(Exception):
    """A response body over the API's `limits.max_response_bytes` (the client discards it)."""


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
        *,
        rpc: RpcCall | None = None,
    ) -> None:
        """Raise `ApiPolicyError` when the call is outside the API's policy.

        The message is what the model reads (a tool error): the API, the
        call and the rule that refused it, without file names. `rpc` is what
        a request to a JSON-RPC API is (`derive_rpc`: its method and decision).
        """
        rpc = rpc or RpcCall()
        reason = refusal_reason(
            self.api(api_name),
            method,
            operation_id,
            path,
            rpc_method=rpc.rpc_method,
            a2a_operation=rpc.a2a_operation,
        )
        if reason:
            what = (operation_id or path or "<unnamed operation>") + describe_rpc(rpc)
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
        *,
        template: str | None = None,
        rpc: RpcCall | None = None,
    ) -> ApprovalGate | None:
        """The human approval the API's policy requires before the call is sent, or None.

        See `gated`: with a list of approval rules, the first rule in file order
        that covers the call (its sent `path`, or the `template` it was rendered
        from) gates it, with that rule's approvers. Ask only after `check`
        passed: approval never widens access.

        Fails closed across rules: a call the first covering rule cannot rule
        out only because it names no operation id, and that a later rule with
        other approvers also covers, could be either rule's call. It raises
        `ApiPolicyError` (refused, nothing is sent) instead of asking either
        rule's approvers.
        """
        rpc = rpc or RpcCall()
        try:
            return gated(
                self.api(api_name),
                method,
                operation_id,
                path,
                template=template,
                rpc_method=rpc.rpc_method,
                a2a_operation=rpc.a2a_operation,
            )
        except ApprovalRuleConflict as exc:
            reason = str(exc)
            what = (operation_id or path or "<unnamed operation>") + describe_rpc(rpc)
            raise ApiPolicyError(
                f"{api_name}: {method.upper()} {what} refused by the API policy: {reason}.",
                reason=reason,
            ) from None


def describe_rpc(rpc: RpcCall) -> str:
    """` (rpc_method GetTask)`, ` (rpc_method SendMessage, a2a_operation approve)`, or ""."""
    parts = [
        f"{key} {value}"
        for key, value in ((RPC_METHOD_KEY, rpc.rpc_method), (A2A_OPERATION_KEY, rpc.a2a_operation))
        if value
    ]
    return f" ({', '.join(parts)})" if parts else ""


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
    backslash inside a segment, empty segments (`//`), a `;` (also
    percent-encoded), a control character anywhere, or whitespace at either
    end of a segment or next to a dot (also percent-encoded:
    `segment_text_problem`, which lint applies to declared paths too), and
    a query or fragment in the path (send them through `params=`) are
    refused: the policy check would otherwise pass a template while the wire
    path lands on another endpoint (for example `/items/1/../../admin` ->
    `/admin`, `/orders/7/cancel;x=1`, which servers that strip path
    parameters route to `/orders/7/cancel`, `/orders/7/cancel%20` and
    `/orders/7/cancel%20.json`, which servers that trim a segment, or the
    name before its suffix, route there too, or `/orders/7%00/cancel`,
    which servers that end a path at a NUL route to `/orders/7`).
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
        if ";" in decoded:
            raise ApiPolicyError(
                f"path {path!r} contains ';' (a path parameter, which some servers strip "
                "before routing): refused."
            )
        problem = segment_text_problem(decoded)
        if problem:
            raise ApiPolicyError(
                f"path {path!r} has a segment that {problem}, which some servers route to "
                "another endpoint: refused."
            )


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
# Human approval (`approval` in api-policy.yaml)
# ---------------------------------------------------------------------------

# The `type` of the interrupt value a gated call raises, and of the resume value
# the chat runtime answers it with.
APPROVAL_INTERRUPT = "api_approval"
APPROVAL_DECISION = "api_approval_decision"
DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"
DECISION_EXPIRED = "expired"
# Not a decision: the answer a resume gives a paused call whose approval is still
# pending (another call's decision resumed the run), so that call pauses again
# for its own approval instead of being rebuilt under a policy that changed.
DECISION_PENDING = "pending"
# What a field named in `redact=` shows the approver instead of its value.
REDACTED = "<redacted>"
# How much of the model's text before a tool call the approval shows as its purpose.
PURPOSE_MAX_CHARS = 500
COMMENT_MAX_CHARS = 300
_UNPRINTABLE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class BoundApproval(NamedTuple):
    """An approval the ledger recorded for a tool call: the call it was asked for and its state."""

    api: str
    method: str
    path: str
    # "pending", "approved", "rejected" or "expired" (a pending one past its expiry).
    status: str
    # Whether its approved call was sent (an approval is used once).
    used: bool
    # A call to a JSON-RPC API: the request its body was (`derive_rpc`), part of which
    # call it is (`call_identity`). None for other calls.
    rpc_method: str | None = None
    a2a_operation: str | None = None


class ApprovalLedger(Protocol):
    """Where approvals are recorded (the chat runtime's approvals table)."""

    async def consume(
        self, approval_id: str, call_hash: str, thread_id: str | None = None
    ) -> str | None:
        """Mark the approval used for the request `call_hash` (of thread `thread_id`):
        None when it may be sent now (approved, for exactly this request on this
        thread, never used before), else why not."""
        ...

    async def bound_approvals(
        self, *, tool_call: tuple[str, str] | None = None, interrupt_id: str | None = None
    ) -> list[BoundApproval]:
        """The approvals recorded for a tool call, newest first: the ones asked by the
        tool call `(message id, tool call id)` or by the interrupt `interrupt_id`,
        on whichever thread (a copied thread keeps its tool calls)."""
        ...


_ledger: ApprovalLedger | None = None


def set_approval_ledger(ledger: ApprovalLedger | None) -> None:
    """Install the process's approvals ledger (the chat runtime does it when it starts).

    Without one, an approved call is refused (nothing sent): no approval can be
    checked for single use.
    """
    global _ledger
    _ledger = ledger


def approval_ledger() -> ApprovalLedger | None:
    return _ledger


# Headers that follow a request to the services it calls (see set_outbound_headers).
_outbound_headers: Callable[[], Mapping[str, str]] | None = None

# The `auth` modes whose APIs receive the correlation headers (see `propagates`): the
# modes that act for the calling user, `forward` and `exchange` (a token exchanged for the
# user's, RFC 8693: the owner's decision of 2026-09-28).
PROPAGATING_AUTH_MODES = frozenset({"forward", "exchange"})
# The protocols whose APIs receive them whatever their `auth`: `a2a`, another agent (an A2A
# peer, the owner's decision of 2026-09-27: "only to A2A peers and auth: forward APIs").
PROPAGATING_PROTOCOLS = frozenset({PROTOCOL_A2A})


def set_outbound_headers(provider: Callable[[], Mapping[str, str]] | None) -> None:
    """Install what adds the correlation headers to the calls that carry them.

    The app installs `telemetry.outbound_trace_headers` (this request's
    `X-Request-ID` and, under OTLP tracing, its W3C trace context), so an agent
    this call reaches logs the same request id and continues the same trace.
    Only the APIs `propagates` names receive them; any other API receives
    neither. A header the tool sets itself wins, and a header a tool may not set
    (`forbidden_header`) is never added. The headers differ for every request,
    so they are not part of the request an approval binds (`canonical_call`).
    """
    global _outbound_headers
    _outbound_headers = provider


def propagates(api_settings: Mapping[str, Any]) -> bool:
    """Whether calls to this API carry the request's correlation headers.

    Only the APIs that are part of the same request do: another agent
    (`protocol: a2a`, `PROPAGATING_PROTOCOLS`), whatever its `auth`, and the
    APIs that act for the calling user (`PROPAGATING_AUTH_MODES`), `auth:
    forward` and `auth: exchange` ones, as another agent does when it is
    reached with the caller's own credential or a token exchanged for it. Any
    other API (`auth: bearer` or `none` over `http` or `jsonrpc`) is a third
    party that never learns this request's id or trace. This is the one place
    that decides; `PROPAGATE_TRACE_HEADERS=false` turns the headers off for
    every API.
    """
    return (
        api_settings.get("auth") in PROPAGATING_AUTH_MODES
        or api_protocol(api_settings) in PROPAGATING_PROTOCOLS
    )


def outbound_headers(api_settings: Mapping[str, Any]) -> dict[str, str]:
    """The correlation headers for a call to this API made now: empty for an API that
    does not receive them (`propagates`) or without a provider."""
    provider = _outbound_headers
    if provider is None or not propagates(api_settings):
        return {}
    try:
        headers = {str(name): str(value) for name, value in dict(provider()).items()}
    except Exception as exc:  # correlation never stops a call
        logger.warning("api call: no correlation headers (%s)", type(exc).__name__)
        return {}
    return {name: value for name, value in headers.items() if not forbidden_header(name)}


def _run_thread_id() -> str | None:
    """The thread of the current graph run (its `configurable.thread_id`), if any."""
    try:
        from langgraph.config import get_config

        thread_id = (get_config().get("configurable") or {}).get("thread_id")
    except Exception:  # outside a graph run
        return None
    return str(thread_id) if thread_id else None


@dataclass
class ToolCallScope:
    """The tool call a gated request is made for (set by the agent's middleware)."""

    name: str
    call_id: str | None = None
    # The id of the model message that made the tool call: with `call_id`, it names
    # this tool call in the approvals ledger (a model may reuse call ids across turns).
    message_id: str | None = None
    # The text the model wrote with the tool call: its stated purpose, when any.
    purpose: str | None = None
    # An approved call was sent in this tool call (a second gated call is refused).
    gated_sent: bool = False
    # How many of the task's resume values this tool call took (`interrupt()` calls).
    resumes_taken: int = 0
    # The calls (`call_identity`) a decision stopped in this tool call.
    stopped: set[tuple[str, ...]] = field(default_factory=set)


_TOOL_CALL: ContextVar[ToolCallScope | None] = ContextVar("api_tool_call", default=None)
# The same, for a tool run without the middleware's scope (in its own context).
_GATED_SENT: ContextVar[bool] = ContextVar("api_gated_sent", default=False)
_RESUMES_TAKEN: ContextVar[int] = ContextVar("api_resumes_taken", default=0)
_STOPPED: ContextVar[frozenset[tuple[str, ...]]] = ContextVar(
    "api_stopped_calls", default=frozenset()
)


def call_identity(
    api: str,
    method: str,
    path: str,
    rpc_method: str | None = None,
    a2a_operation: str | None = None,
) -> tuple[str, ...]:
    """Which call a decision is bound to: the API, the method and the path sent.

    The path is compared normalised and ignoring letter case, as a gate
    compares it, so a rebuilt request that differs only in spelling is the
    same call (a body or query that changed is caught by the call hash).

    A call to a JSON-RPC API (`protocol: jsonrpc|a2a`) is also known by the
    request its body is (`derive_rpc`): its JSON-RPC method and A2A decision.
    Every call to such an API shares one method and path, so without them a
    `GetTask` the resumed tool sends first would take the decision an approve
    message waits for, and a call a rejection stopped would stop the `reject`
    message that tells the other agent. A call to an `http` API keeps the
    three fields: its label is the tool's, not the request's.
    """
    identity: tuple[str, ...] = (
        str(api),
        str(method).upper(),
        normalize_path(str(path)).casefold(),
    )
    if rpc_method is None and a2a_operation is None:
        return identity
    return (*identity, str(rpc_method or ""), str(a2a_operation or ""))


def _is_decision(value: Any) -> bool:
    return isinstance(value, Mapping) and value.get("type") == APPROVAL_DECISION


def _decision_identity(decision: Mapping[str, Any]) -> tuple[str, ...] | None:
    api, method, path = decision.get("api"), decision.get("method"), decision.get("path")
    if not (isinstance(api, str) and isinstance(method, str) and isinstance(path, str)):
        return None
    rpc_method, a2a_operation = decision.get(RPC_METHOD_KEY), decision.get(A2A_OPERATION_KEY)
    if not all(value is None or isinstance(value, str) for value in (rpc_method, a2a_operation)):
        return None
    return call_identity(api, method, path, rpc_method, a2a_operation)


# Where LangGraph keeps a task's resume values in the run config (its interrupt()
# reads them there); the constant's module is internal, so it is looked up lazily.
_SCRATCHPAD_KEY_FALLBACK = "__pregel_scratchpad"


def _scratchpad_key() -> str:
    try:
        from langgraph._internal._constants import CONFIG_KEY_SCRATCHPAD
    except ImportError:  # moved in another LangGraph version: the name it has always had
        return _SCRATCHPAD_KEY_FALLBACK
    return str(CONFIG_KEY_SCRATCHPAD)


def _next_resume_value(taken: int) -> Any:
    """What the task's next `interrupt()` returns, without taking it; None when nothing.

    The resume values of the current LangGraph task (the decisions a resumed
    run brought for its paused calls) in the order `interrupt()` hands them
    out; `taken` is how many this tool call took already. Outside a LangGraph
    task (no run, or a tool invoked directly) there are none. A task whose
    resume values cannot be read (another LangGraph version) is refused: a
    decision waiting there could not be honoured.
    """
    try:
        from langgraph.config import get_config

        configurable = get_config().get("configurable") or {}
    except Exception:  # outside a graph run
        return None
    scratchpad = configurable.get(_scratchpad_key()) if isinstance(configurable, Mapping) else None
    if scratchpad is None:
        return None
    try:
        resumed = list(scratchpad.resume)
        if taken < len(resumed):
            return resumed[taken]
        return scratchpad.get_null_resume(False) if taken == len(resumed) else None
    except (AttributeError, TypeError) as exc:
        raise ApiPolicyError(
            "cannot read the run's approval decisions (an unsupported LangGraph version): "
            "refused, nothing was sent.",
            reason="approval decisions unreadable",
        ) from exc


def _task_interrupt_id() -> str | None:
    """The id an `interrupt()` of the current LangGraph task gets (None outside a task).

    LangGraph derives it from the task's checkpoint namespace, so the task a
    run continues without input interrupts with the same id as when it paused.
    """
    try:
        from langgraph.config import get_config
        from langgraph.types import Interrupt

        namespace = (get_config().get("configurable") or {}).get("checkpoint_ns")
        if not isinstance(namespace, str) or not namespace:
            return None
        return str(Interrupt.from_ns(None, namespace).id)
    except Exception:  # outside a graph run, or another LangGraph version
        return None


def _resumes_taken() -> int:
    scope = _TOOL_CALL.get()
    return scope.resumes_taken if scope is not None else _RESUMES_TAKEN.get()


def _took_resume() -> None:
    scope = _TOOL_CALL.get()
    if scope is not None:
        scope.resumes_taken += 1
    else:
        _RESUMES_TAKEN.set(_RESUMES_TAKEN.get() + 1)


def _stopped_calls() -> frozenset[tuple[str, ...]] | set[tuple[str, ...]]:
    scope = _TOOL_CALL.get()
    return scope.stopped if scope is not None else _STOPPED.get()


def _stop_call(identity: tuple[str, ...]) -> None:
    scope = _TOOL_CALL.get()
    if scope is not None:
        scope.stopped.add(identity)
    else:
        _STOPPED.set(_STOPPED.get() | {identity})


def _decision_waiting(identity: tuple[str, ...]) -> Mapping[str, Any] | None:
    """The decision the resumed run brought for this call, when the next resume value is one.

    A request the tool rebuilds on resume is the paused call when it has the
    paused call's API, method and path, and on a JSON-RPC API its JSON-RPC
    method and A2A decision (`call_identity`); the decision then applies to
    it whatever the policy now says about gating it.
    """
    waiting = _next_resume_value(_resumes_taken())
    if _is_decision(waiting) and _decision_identity(waiting) == identity:
        return waiting
    return None


def _decided_with(value: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    """`(decide_with, relayers)` an interrupt or a decision carries (`direct` when absent: a
    call paused before 0.3)."""
    decide_with = value.get("decide_with")
    relayers = value.get("relayers")
    return (
        decide_with if isinstance(decide_with, str) else DEFAULT_DECIDE_WITH,
        tuple(str(r) for r in relayers) if isinstance(relayers, list | tuple) else (),
    )


def _context_actor() -> str | None:
    """The agent the current run acts through (its principal's `@actor`), or None."""
    context = current_context()
    attributes = getattr(context, "attributes", None)
    if attributes is None and isinstance(context, Mapping):
        attributes = context.get("attributes")
    actor = attributes.get("@actor") if isinstance(attributes, Mapping) else None
    actor_id = actor.get("id") if isinstance(actor, Mapping) else None
    return actor_id if isinstance(actor_id, str) and actor_id else None


def _bound_refusal(bound: list[BoundApproval]) -> tuple[str, str]:
    """What the model reads, and the log reason, for a call refused by its recorded approval.

    `bound` is newest first; a used approval wins (the call was sent once).
    """
    record = next((b for b in bound if b.used), bound[0])
    if record.used:
        return (
            "was sent already with its approval, which is used once (the tool call ran "
            "again without a new decision)",
            "approval already used",
        )
    if record.status == "rejected":
        return "was not approved: an approver rejected it", "approval rejected"
    if record.status == "expired":
        return (
            "was not approved: the approval request expired before anyone decided",
            "approval expired",
        )
    if record.status == "approved":
        return (
            "was approved, but only the run its decision resumed may send it (the tool "
            "call ran again without that decision)",
            "approval not resumed",
        )
    return (
        "needs human approval, and the run was resumed without an approval decision",
        "resumed without a decision",
    )


def _plain(text: Any, limit: int) -> str | None:
    """`text` as one printable line of at most `limit` characters, or None when empty."""
    if isinstance(text, list):
        text = "".join(
            str(block.get("text", "")) if isinstance(block, Mapping) else str(block)
            for block in text
        )
    if not isinstance(text, str):
        return None
    cleaned = " ".join(_UNPRINTABLE.sub("", text).split())
    if not cleaned:
        return None
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 3].rstrip() + "..."


def _field(message: Any, name: str) -> Any:
    return message.get(name) if isinstance(message, Mapping) else getattr(message, name, None)


def calling_message(messages: Iterable[Any], call_id: str | None) -> Any:
    """The assistant message that made tool call `call_id` (the latest one), or None."""
    if not call_id:
        return None
    for message in reversed(list(messages or [])):
        if any(
            (c.get("id") if isinstance(c, Mapping) else getattr(c, "id", None)) == call_id
            for c in _field(message, "tool_calls") or []
        ):
            return message
    return None


def stated_purpose(messages: Iterable[Any], call_id: str | None) -> str | None:
    """The text of the assistant message that made tool call `call_id`, when it wrote any.

    This is the model's own account of why it makes the call (the default
    system prompt asks for one before an action that needs approval). It is
    shown to the approver as the model's statement, beside the exact request.
    """
    message = calling_message(messages, call_id)
    return None if message is None else _plain(_field(message, "content"), PURPOSE_MAX_CHARS)


@contextlib.contextmanager
def tool_call_scope(request: Any) -> Iterator[ToolCallScope]:
    """Name the tool call a middleware is about to run (`request` is its ToolCallRequest).

    A gated request made inside names that tool, and the model's stated
    purpose, in its approval.
    """
    call = getattr(request, "tool_call", None) or {}
    state = getattr(request, "state", None)
    messages = state.get("messages") if isinstance(state, Mapping) else None
    call_id = call.get("id") if isinstance(call, Mapping) else None
    message = calling_message(messages or [], call_id)
    message_id = _field(message, "id") if message is not None else None
    scope = ToolCallScope(
        name=str((call.get("name") if isinstance(call, Mapping) else "") or ""),
        call_id=str(call_id) if call_id else None,
        message_id=str(message_id) if message_id else None,
        purpose=None if message is None else _plain(_field(message, "content"), PURPOSE_MAX_CHARS),
    )
    token = _TOOL_CALL.set(scope)
    try:
        yield scope
    finally:
        _TOOL_CALL.reset(token)


def redact_fields(value: Any, names: frozenset[str]) -> Any:
    """`value` with the value of every key in `names` (any depth, any letter case) masked."""
    if not names:
        return value
    if isinstance(value, Mapping):
        return {
            key: REDACTED if str(key).casefold() in names else redact_fields(item, names)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact_fields(item, names) for item in value]
    return value


def _query_view(query: httpx.QueryParams | None, names: frozenset[str]) -> dict[str, Any]:
    """The query as the approver sees it: a repeated key lists its values."""
    view: dict[str, Any] = {}
    for key, value in query.multi_items() if query is not None else ():
        shown = REDACTED if key.casefold() in names else value
        if key not in view:
            view[key] = shown
        elif isinstance(view[key], list):
            view[key].append(shown)
        else:
            view[key] = [view[key], shown]
    return view


def canonical_call(
    api: str,
    method: str,
    url: httpx.URL | str,
    query: httpx.QueryParams | None,
    json_body: Any,
    operation_id: str | None,
    headers: Iterable[tuple[str, str]] = (),
) -> dict[str, Any]:
    """Everything that decides what a request does, in one comparable form.

    The URL carries the rendered path under the API's base URL; the query keeps
    the order it is sent in; `headers` are the tool's own (the policy's
    credential is not part of the call: it may be renewed between the approval
    and the send).
    """
    return {
        "api": api,
        "method": method.upper(),
        "url": str(url),
        "query": [[key, value] for key, value in (query.multi_items() if query else ())],
        "body": json_body,
        "operation_id": operation_id or None,
        "headers": sorted([name.lower(), value] for name, value in headers),
    }


def call_hash(call: Mapping[str, Any]) -> str:
    """SHA-256 of `canonical_call`: equal exactly when the requests are the same.

    Raises `TypeError` or `ValueError` for a body that is not plain JSON (a NaN,
    an object JSON has no form for).
    """
    text = json.dumps(
        call, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class PreparedRequest:
    """A request that passed every policy check (`ApiClient._prepare`)."""

    wire_path: str
    url: httpx.URL
    query: httpx.QueryParams | None
    headers: httpx.Headers
    # The tool's own headers (without the policy's credential), bound by an approval.
    tool_headers: list[tuple[str, str]]
    secrets: tuple[str, ...]
    gate: ApprovalGate | None
    # What a request to a JSON-RPC API is, read from its body (`derive_rpc`).
    rpc: RpcCall = field(default_factory=RpcCall)


@dataclass(frozen=True)
class SubjectToken:
    """The caller's own verified bearer token, as the auth policy kept it.

    `credentials["@subject_token"]`, with its audiences (`@subject_aud`) and
    expiry (`@subject_exp`, epoch seconds; None when unknown). What `auth:
    exchange` exchanges and `auth: forward` with `forward_audience` forwards.
    """

    # Out of repr: a repr ends up in logs and tracebacks.
    token: str = field(repr=False)
    audience: tuple[str, ...] = ()
    expires_at: float | None = None


# Where the delegation-carrying APIs send the caller's identity: `forward` and
# `exchange` in `forward_header`, `bearer` in Authorization.
_CREDENTIAL_HEADER_MODES = ("bearer", *HEADER_AUTH_MODES)
# Hosts a credential-carrying call to another agent may reach over plain http outside
# APP_ENV=dev: loopback, a single-label service name, a cluster-internal name.
_INTERNAL_SUFFIXES = (".svc", ".svc.cluster.local")


def internal_host(host: str) -> bool:
    """Whether `host` is loopback, a single-label name or a cluster-internal (`.svc`) name."""
    host = host.strip("[]").lower().rstrip(".")
    if host == "localhost" or ("." not in host and ":" not in host):
        return True
    if host.endswith(_INTERNAL_SUFFIXES):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def peer_transport_problem(settings: Mapping[str, Any], url: httpx.URL, env: str) -> str | None:
    """Why a call to another agent may not use this base URL, or None.

    Outside `APP_ENV=dev`, an A2A peer (`protocol: a2a`) reached with a
    credential (`bearer`, `forward` or `exchange`) refuses a plain http base
    URL unless the host is internal (`internal_host`). Other APIs are not
    checked (a later release may extend it).
    """
    if settings.get("protocol") != "a2a" or settings.get("auth") not in _CREDENTIAL_HEADER_MODES:
        return None
    if url.scheme != "http" or os.environ.get("APP_ENV") == "dev" or internal_host(url.host):
        return None
    return f"{env} must use https outside APP_ENV=dev to carry credentials"


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
        subject: SubjectToken | None = None,
        actor_chain: tuple[str, ...] = (),
    ) -> None:
        self.policy = policy
        self.name = name
        self.settings = policy.api(name)
        self._credential = credential
        self._transport = transport
        self._run_id = run_id
        # `auth: exchange`: the caller's own token, exchanged just before sending.
        self._subject = subject
        # The agents the request came through, current first (loop refusal).
        self._actor_chain = tuple(actor_chain)

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
        problem = peer_transport_problem(self.settings, url, env)
        if problem:
            raise ApiCallError(f"{problem}; nothing was sent to API {self.name!r}.")
        return url

    def credential_header(self) -> str | None:
        """The header the policy's credential goes in (None: `auth: none`).

        A tool header of that name is never sent nor bound by an approval: the
        policy's credential replaces it.
        """
        mode = self.settings["auth"]
        if mode in HEADER_AUTH_MODES:
            return str(self.settings.get("forward_header") or DEFAULT_FORWARD_HEADER)
        return "Authorization" if mode == "bearer" else None

    def delegation_target(self) -> str | None:
        """The agent (audience) a call to this API acts for the caller at, or None.

        `exchange.audience` for `auth: exchange`, `forward_audience` for `auth:
        forward`: the calls that carry the caller's identity onward.
        """
        exchange = self.settings.get(EXCHANGE_KEY)
        if self.settings["auth"] == "exchange" and isinstance(exchange, Mapping):
            return str(exchange.get("audience") or "") or None
        if self.settings["auth"] == "forward" and self.settings.get("forward_audience"):
            return str(self.settings["forward_audience"])
        return None

    def check_loop(self) -> None:
        """Refuse (`ApiPolicyError`) a call that would come back to an agent of this request.

        That is this agent itself, or an agent already in the request's
        delegation chain (A -> B -> A), named by the API's delegation target.
        """
        target = self.delegation_target()
        if target is None:
            return
        try:
            from .token_exchange import loop_problem
        except ImportError:  # loaded outside its package: fail closed
            raise ApiPolicyError(
                f"{self.name}: the delegation loop check is unavailable; nothing was sent.",
                reason="delegation loop check unavailable",
            ) from None
        problem = loop_problem(target, self._actor_chain)
        if problem:
            raise ApiPolicyError(
                f"{self.name}: refused: {problem}; nothing was sent.", reason="delegation loop"
            )

    async def exchanged_credential(self) -> tuple[str, str] | None:
        """`(header, "Bearer <token>")` for an `auth: exchange` API; None for any other.

        The caller's own token exchanged for one minted for the API's
        `exchange.audience` (`token_exchange.py`: cached, single flight, failures
        remembered briefly). Raises `ApiCallError` (nothing sent) when the run
        has no user token, it has expired, the issuer refuses or fails, or the
        token names no actor and the API does not set `exchange.allow_actorless`.
        """
        if self.settings["auth"] != "exchange":
            return None
        if self._subject is None:
            raise ApiCallError(
                f"API {self.name!r} uses auth: exchange, but this run has no user token to "
                "exchange (shared-bearer, or a run resumed by another principal); nothing was "
                "sent.",
                reason="no user token to exchange",
            )
        try:
            from .token_exchange import TokenExchangeError, exchanger
        except ImportError:  # loaded outside its package: fail closed
            raise ApiCallError(
                f"API {self.name!r} uses auth: exchange, but token exchange is unavailable; "
                "nothing was sent.",
                reason="token exchange unavailable",
            ) from None
        exchange = self.settings[EXCHANGE_KEY]
        try:
            token = await exchanger().token(
                self.name,
                self._subject.token,
                audience=str(exchange["audience"]),
                scope=exchange.get("scope"),
                resource=exchange.get("resource"),
                subject_expires_at=self._subject.expires_at,
                allow_actorless=exchange.get(ALLOW_ACTORLESS_KEY) is True,
            )
        except TokenExchangeError as exc:
            raise ApiCallError(str(exc), reason=exc.reason) from None
        return self.credential_header() or DEFAULT_FORWARD_HEADER, f"Bearer {token}"

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
        redact: Iterable[str] = (),
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
        never followed. With `limits.max_response_bytes`, the body is read
        only up to that many bytes (decoded): past it the response is
        discarded and `ApiCallError` raised. An empty response body returns "". Raises
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

        A call the API's `approval` gates pauses the run for a human decision
        before it is sent (see the module docstring); `redact` names body and
        query fields (any depth, any letter case) the approver sees masked,
        for values they need not read (a card number, say). The approval is
        bound to the request as sent, masked fields included, and a decision
        to the call it was taken for: on resume it applies to that call even
        when the policy no longer gates it, and a tool call that runs again
        without a decision does not send a call an approval was asked for (a
        rejected call is never sent, an approved one once).
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
            prepared = self._prepare(
                method, path, operation_id, path_params, params, json_body, headers
            )
            bound = await self._bound_approvals(prepared, method, operation_id or label)
            approved = self._await_approval(
                prepared, method, operation_id, label, json_body, redact, log_fields, bound
            )
            # Counted just before sending: a call held for approval counts once, when sent.
            self.take_limits(method, operation_id or path)
            if approved is not None:
                await self._use_approval(approved, method, operation_id or label, log_fields)
            wire_path, url, query = prepared.wire_path, prepared.url, prepared.query
            request_headers, secrets = prepared.headers, prepared.secrets
            # Last, just before sending: a refused or paused call never exchanges a token.
            exchanged = await self.exchanged_credential()
            if exchanged is not None:
                header, value = exchanged
                request_headers[header] = value
                secrets = (*secrets, value, value.split(" ", 1)[1])
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
        except ApiCallError as exc:
            logger.warning(
                "api call not sent: %s %s %s: %s",
                self.name,
                method,
                label,
                exc.reason or "not configured",
                extra=log_fields,
            )
            raise
        started = time.perf_counter()
        cap = self.response_cap()
        async with httpx.AsyncClient(
            transport=self._transport, timeout=self.timeout(), follow_redirects=False
        ) as client:
            try:
                if cap is None:
                    response = await client.request(
                        method, url, params=query, json=json_body, headers=request_headers
                    )
                else:
                    request = client.build_request(
                        method, url, params=query, json=json_body, headers=request_headers
                    )
                    response = await self._send_capped(client, request, cap)
                response.raise_for_status()
            except ResponseTooLarge:
                self._log_call(label, "too large", started, log_fields, failed=True)
                raise ApiCallError(
                    f"{self.name} answered with more than {cap} bytes; discarded",
                    reason="response over limits.max_response_bytes",
                ) from None
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

    def response_cap(self) -> int | None:
        """`limits.max_response_bytes`: the most a response body may hold, or None (no cap)."""
        cap = (self.settings.get("limits") or {}).get("max_response_bytes")
        return cap if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0 else None

    @staticmethod
    async def _send_capped(
        client: httpx.AsyncClient, request: httpx.Request, cap: int
    ) -> httpx.Response:
        """Send `request` and read at most `cap` bytes of its body (decoded, as the tool gets
        it); `ResponseTooLarge` past that, with the rest never read. A declared
        `Content-Length` over the cap (an uncompressed body) is refused before reading."""
        response = await client.send(request, stream=True)
        try:
            encoded = response.headers.get("content-encoding", "identity").strip().lower()
            declared = response.headers.get("content-length", "")
            if encoded in ("", "identity") and declared.isdigit() and int(declared) > cap:
                raise ResponseTooLarge
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > cap:
                    raise ResponseTooLarge
                chunks.append(chunk)
        finally:
            await response.aclose()
        # The body as read (decoded): a response that holds it, for the usual handling.
        headers = [
            (name, value)
            for name, value in response.headers.multi_items()
            if name.lower() not in ("content-encoding", "content-length", "transfer-encoding")
        ]
        return httpx.Response(
            response.status_code,
            headers=headers,
            content=b"".join(chunks),
            request=request,
        )

    def _prepare(
        self,
        method: str,
        path: str,
        operation_id: str | None,
        path_params: Mapping[str, Any] | None,
        params: Any,
        json_body: Any,
        headers: Mapping[str, str] | None,
    ) -> PreparedRequest:
        """Every policy check before sending: the path, URL, query, headers, the credential
        sent, and the approval the call needs (`gate`, None when none)."""
        if method not in HTTP_METHODS:
            raise ApiPolicyError(f"{self.name}: unknown HTTP method {method!r}.")
        self.check_loop()
        # What the request is, read from the body sent (a JSON-RPC API): a malformed one is
        # refused before any rule is asked, and no rule trusts the tool's label for it.
        rpc = self.rpc_call(method, json_body)
        if path_params is not None:
            self.policy.check(self.name, method, operation_id, path, rpc=rpc)
            wire_path = render_path(path, path_params)
        elif _PLACEHOLDER.search(path):
            raise ApiPolicyError(f"path {path!r} has unfilled parameters; pass path_params=.")
        else:
            wire_path = path
        validate_concrete_path(wire_path)
        self.policy.check(self.name, method, operation_id, wire_path, rpc=rpc)
        mislabelled = label_problem(self.settings, operation_id, rpc.rpc_method, rpc.a2a_operation)
        if mislabelled:
            raise ApiPolicyError(
                f"{self.name}: {mislabelled}.", reason="operation_id does not match the request"
            )
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
        credential_header = self.credential_header()
        excluded = {c.lower() for c in credentials}
        if credential_header is not None:
            excluded.add(credential_header.lower())
            if credential_header not in credentials and credential_header in request_headers:
                # `exchange`: the token is added just before sending; never the tool's own.
                del request_headers[credential_header]
        tool_headers = [
            (name, value)
            for name, value in request_headers.multi_items()
            if name.lower() not in excluded
        ]
        for name, value in outbound_headers(self.settings).items():
            if name not in request_headers:  # the tool's own header wins
                request_headers[name] = value  # not in tool_headers: no approval binds it
        for name, value in credentials.items():
            request_headers[name] = value  # the policy's credential always wins
        secrets = tuple(credentials.values()) + tuple(
            value.split(" ", 1)[1] for value in credentials.values() if " " in value
        )
        gate = self.gate_for(method, operation_id, path, wire_path, path_params is not None, rpc)
        if rpc.a2a_operation == A2A_APPROVE and gate is None:
            # The policy's validator already refuses an A2A API that could send one: this
            # holds even for a policy that was never validated.
            reason = "a message that approves must wait for an approval or be denied"
            raise ApiPolicyError(
                f"{self.name}: {method} {(operation_id or wire_path) + describe_rpc(rpc)} refused "
                f"by the API policy: {reason} (gate or deny a2a_operation: approve).",
                reason=reason,
            )
        return PreparedRequest(
            wire_path=wire_path,
            url=url,
            query=query,
            headers=request_headers,
            tool_headers=tool_headers,
            secrets=secrets,
            gate=gate,
            rpc=rpc,
        )

    def rpc_call(self, method: str, json_body: Any) -> RpcCall:
        """What a request to this API is, read from the JSON body sent (`derive_rpc`).

        Empty for an `http` API. A body a JSON-RPC API cannot send as one
        JSON-RPC request raises `ApiPolicyError`, before anything is sent.
        """
        try:
            return derive_rpc(self.settings, method, json_body)
        except RpcRequestError as exc:
            raise ApiPolicyError(
                f"{self.name}: {exc}.", reason="not one JSON-RPC request"
            ) from None

    def gate_for(
        self,
        method: str,
        operation_id: str | None,
        path: str,
        wire_path: str,
        templated: bool,
        rpc: RpcCall | None = None,
    ) -> ApprovalGate | None:
        """The approval the API's `approval` requires before this call, or None.

        A rule covers the call when it covers the sent path or the template it
        was rendered from; the first such rule in file order gates it, and its
        approvers are the ones the approval is asked of (and bound to). On a
        JSON-RPC API an entry naming `rpc_method` or `a2a_operation` covers
        the call by what its body is (`rpc`).
        Asked only once the policy allowed the call: approval never widens access.
        """
        return self.policy.gate(
            self.name,
            method,
            operation_id,
            wire_path,
            template=path if templated else None,
            rpc=rpc,
        )

    async def _bound_approvals(
        self, prepared: PreparedRequest, method: str, what: str
    ) -> list[BoundApproval]:
        """The approvals the ledger holds for this call in this tool call, when none is resumed now.

        Asked when no decision waits for the call in this run: the tool call
        runs again without one (a run continued without input, or replayed
        from a checkpoint, through LangGraph Server's own API; a copy of the
        thread). The ledger then answers, by the tool call (the model message
        and the call id) or by the task's interrupt, with the approvals it
        recorded, and `_await_approval` refuses the call when one was asked
        for it, whatever the policy now says about gating it. Nothing to ask
        (no ledger, outside a tool call and a task): empty. A ledger that
        cannot answer refuses the call.
        """
        identity = self._identity(method, prepared)
        if identity in _stopped_calls() or _decision_waiting(identity) is not None:
            return []  # `_await_approval` refuses it, or applies its decision
        ledger = _ledger
        scope = _TOOL_CALL.get()
        tool_call = (
            (scope.message_id, scope.call_id)
            if scope is not None and scope.message_id and scope.call_id
            else None
        )
        interrupt_id = _task_interrupt_id()
        if ledger is None or (tool_call is None and interrupt_id is None):
            return []
        try:
            found = await ledger.bound_approvals(tool_call=tool_call, interrupt_id=interrupt_id)
        except Exception as exc:
            raise ApiPolicyError(
                f"{self.name}: {method} {what}: the approvals of this tool call could not be "
                f"read ({type(exc).__name__}), so it may have been decided already; nothing "
                "was sent.",
                reason="approvals unreadable",
            ) from exc
        return [
            b
            for b in found
            if call_identity(b.api, b.method, b.path, b.rpc_method, b.a2a_operation) == identity
        ]

    def _identity(self, method: str, prepared: PreparedRequest) -> tuple[str, ...]:
        """Which call this is, for the decisions bound to calls (`call_identity`)."""
        return call_identity(
            self.name,
            method,
            prepared.wire_path,
            prepared.rpc.rpc_method,
            prepared.rpc.a2a_operation,
        )

    def _await_approval(
        self,
        prepared: PreparedRequest,
        method: str,
        operation_id: str | None,
        label: str,
        json_body: Any,
        redact: Iterable[str],
        log_fields: Mapping[str, Any],
        bound: list[BoundApproval] | None = None,
    ) -> tuple[str, str] | None:
        """Hold a gated call for a human decision (see the module doc); None when not gated.

        The first time, `interrupt()` pauses the run with the approval payload
        (raising LangGraph's `GraphInterrupt`, which ends the tool call). On
        resume it returns the decision: this returns `(approval_id, call_hash)`
        only for an approval of exactly this request (`_use_approval` then
        marks it used); anything else raises `ApiPolicyError`, nothing sent.
        The decision is taken for the call it was made for even when the
        policy no longer gates it (`_decision_waiting`), and a call a decision
        stopped is refused again for the rest of the tool call. A call the
        ledger holds an approval for (`bound`, from `_bound_approvals`) while
        no decision waits for it is refused, gated or not: it is sent only
        through its own decision, once.
        """
        identity = self._identity(method, prepared)
        what = operation_id or label
        gate = prepared.gate
        if identity in _stopped_calls():
            raise ApiPolicyError(
                f"{self.name}: {method} {what} was stopped by its approval decision earlier "
                "in this tool call; nothing was sent.",
                reason="stopped by an approval decision",
            )
        waiting = _decision_waiting(identity)
        if waiting is None and bound:
            why, reason = _bound_refusal(bound)
            _stop_call(identity)
            raise ApiPolicyError(
                f"{self.name}: {method} {what} {why}; nothing was sent.", reason=reason
            )
        if gate is None and waiting is None:
            return None
        if gate is not None:
            approvers: tuple[str, ...] = gate.approvers
            timeout_s, rule = gate.timeout_s, gate.rule
            decide_with, relayers = gate.decide_with, gate.relayers
        else:
            # The paused call's gate is gone from the policy: its decision still binds it.
            assert waiting is not None
            asked = waiting.get("approvers")
            approvers = tuple(str(a) for a in asked) if isinstance(asked, list | tuple) else ()
            timeout_s = DEFAULT_APPROVAL_TIMEOUT_S
            rule = "the approval this call was paused for (the policy no longer gates it)"
            decide_with, relayers = _decided_with(waiting)
        rule_reason = f"approval required by {rule}"

        def refuse(why: str, reason: str = rule_reason) -> ApiPolicyError:
            return ApiPolicyError(
                f"{self.name}: {method} {what} {why}; nothing was sent.", reason=reason
            )

        call = canonical_call(
            self.name,
            method,
            prepared.url,
            prepared.query,
            json_body,
            operation_id,
            prepared.tool_headers,
        )
        try:
            digest = call_hash(call)
        except (TypeError, ValueError):
            raise refuse(
                "needs human approval, but its JSON body is not plain JSON, so it cannot be "
                "shown for approval (refused)"
            ) from None
        scope = _TOOL_CALL.get()
        if scope.gated_sent if scope is not None else _GATED_SENT.get():
            raise refuse(
                "needs human approval, and this tool call already sent an approved call: a tool "
                "call sends at most one (refused). Make this call in a new tool call"
            )
        names = frozenset(str(n).casefold() for n in redact or ())
        tool = scope.name if scope is not None and scope.name else None
        purpose = scope.purpose if scope is not None else None
        payload = {
            "type": APPROVAL_INTERRUPT,
            "api": self.name,
            "method": method,
            "path": prepared.wire_path,
            "query": _query_view(prepared.query, names),
            "body": redact_fields(json_body, names),
            "operation_id": operation_id or None,
            "tool": tool,
            "tool_call_id": scope.call_id if scope is not None else None,
            "message_id": scope.message_id if scope is not None else None,
            "reason": f"{tool}: {purpose}" if tool and purpose else (tool or purpose or None),
            "approvers": list(approvers),
            # How the requester decides (bound, as the approvers are: see `_check_decision`),
            # and the agent this run acts through (None: the user directly).
            "decide_with": decide_with,
            "relayers": list(relayers),
            "requester_actor": _context_actor(),
            "timeout_s": timeout_s,
            "rule": rule,
            "call_hash": digest,
        }
        # A JSON-RPC call: the request its body is, which is part of which call it is.
        for key, value in (
            (RPC_METHOD_KEY, prepared.rpc.rpc_method),
            (A2A_OPERATION_KEY, prepared.rpc.a2a_operation),
        ):
            if value is not None:
                payload[key] = value
        outside_run = refuse(
            f"needs human approval ({', '.join(approvers)}) before it is sent, which "
            "is possible only inside an agent run: refused"
        )
        try:
            from langgraph.errors import GraphBubbleUp
            from langgraph.types import interrupt
        except ImportError:  # loaded where LangGraph is not installed: nothing can pause
            raise outside_run from None
        try:
            decision = interrupt(payload)
            _took_resume()
            # Another call's decision resumed the run while this one still waits:
            # pause again, for the same approval.
            while _is_decision(decision) and decision.get("decision") == DECISION_PENDING:
                decision = interrupt(payload)
                _took_resume()
        except GraphBubbleUp:
            logger.info(
                "api call held for approval: %s %s %s (%s)",
                self.name,
                method,
                label,
                ", ".join(approvers),
                extra=dict(log_fields),
            )
            raise
        except (RuntimeError, KeyError):
            # Outside an agent run (`get_config` fails): nothing can pause and ask.
            raise outside_run from None
        try:
            approval_id = self._check_decision(decision, digest, gate, refuse)
        except ApiPolicyError:
            _stop_call(identity)
            raise
        return approval_id, digest

    async def _use_approval(
        self, approved: tuple[str, str], method: str, what: str, log_fields: Mapping[str, Any]
    ) -> None:
        """Mark the approval used in the ledger, once, just before the call is sent."""
        approval_id, digest = approved
        ledger = _ledger
        problem = (
            "this process has no approvals ledger to mark the approval used"
            if ledger is None
            else await ledger.consume(approval_id, digest, _run_thread_id())
        )
        if problem:
            raise ApiPolicyError(
                f"{self.name}: {method} {what} was approved, but the approval cannot be used: "
                f"{problem}; nothing was sent.",
                reason=f"approval not usable: {problem}",
            )
        scope = _TOOL_CALL.get()
        if scope is not None:
            scope.gated_sent = True
        else:
            _GATED_SENT.set(True)
        logger.info(
            "api call approved: %s %s %s (approval %s)",
            self.name,
            method,
            what,
            approval_id,
            extra=dict(log_fields),
        )

    @staticmethod
    def _check_decision(
        decision: Any,
        digest: str,
        gate: ApprovalGate | None,
        refuse: Callable[..., ApiPolicyError],
    ) -> str:
        """The approval id of a decision that approves the request `digest`; else raise.

        `gate` is what the policy requires of the call now (None: it no longer
        gates it). A rejection or an expiry refuses whatever the policy says.
        An approval must also have been asked of the approvers the policy's
        gate names now, to be decided the same way (`decide_with`, `relayers`):
        a policy that changed while the call waited (a new image with other
        approvers, one that lets other agents relay the decision, or one that
        no longer gates the call) is not satisfied by a decision taken under
        the old one.
        """
        if not isinstance(decision, Mapping) or decision.get("type") != APPROVAL_DECISION:
            raise refuse(
                "needs human approval, and the run was resumed without an approval decision",
                reason="resumed without a decision",
            )
        verdict = decision.get("decision")
        if verdict == DECISION_REJECT:
            comment = _plain(decision.get("comment"), COMMENT_MAX_CHARS)
            raise refuse(
                "was not approved: an approver rejected it"
                + (f" (their comment: {comment})" if comment else ""),
                reason="approval rejected",
            )
        if verdict == DECISION_EXPIRED:
            raise refuse(
                "was not approved: the approval request expired before anyone decided",
                reason="approval expired",
            )
        approval_id = decision.get("approval_id")
        if verdict != DECISION_APPROVE or not isinstance(approval_id, str) or not approval_id:
            raise refuse("was not approved (an unknown decision)", reason="unknown decision")
        if decision.get("call_hash") != digest:
            raise refuse(
                "differs from the request that was approved (it changed after the approval), "
                "so the approval does not cover it",
                reason="request differs from the approved one",
            )
        asked = decision.get("approvers")
        if gate is None:
            raise refuse(
                "was approved under an approval gate the policy no longer has (it changed "
                "while the call waited), so the approval does not cover it; ask again",
                reason="approval gate changed",
            )
        if not isinstance(asked, list | tuple) or {str(a) for a in asked} != set(gate.approvers):
            raise refuse(
                "was approved under an approval gate that has changed since (its approvers "
                "differ from the policy's now), so the approval does not cover it; ask again",
                reason="approval gate changed",
            )
        decide_with, relayers = _decided_with(decision)
        if (decide_with, frozenset(relayers)) != (gate.decide_with, frozenset(gate.relayers)):
            raise refuse(
                "was approved under an approval gate that has changed since (how its approvers "
                "decide, decide_with or relayers, differs from the policy's now), so the "
                "approval does not cover it; ask again",
                reason="approval gate changed",
            )
        return approval_id

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


def _context_credentials(context: Any) -> Mapping[str, Any]:
    attributes = getattr(context, "attributes", None)
    if attributes is None and isinstance(context, Mapping):
        attributes = context.get("attributes")
    credentials = attributes.get("credentials") if isinstance(attributes, Mapping) else None
    return credentials if isinstance(credentials, Mapping) else {}


def subject_token_of(context: Any) -> SubjectToken | None:
    """The calling principal's own verified bearer token (`credentials["@subject_token"]`).

    Kept by the `jwt` policy only when an API acts with it (`auth: exchange`,
    or `auth: forward` with `forward_audience`), or by a custom policy's
    `keep_subject_token`; None otherwise (and never under langgraph-server,
    whose run context carries no credentials).
    """
    credentials = _context_credentials(context)
    token = credentials.get("@subject_token")
    if not isinstance(token, str) or not token:
        return None
    raw_aud = credentials.get("@subject_aud")
    audience = (
        tuple(str(a) for a in raw_aud if isinstance(a, str))
        if isinstance(raw_aud, list | tuple)
        else (raw_aud,)
        if isinstance(raw_aud, str)
        else ()
    )
    raw_exp = credentials.get("@subject_exp")
    expires_at = (
        float(raw_exp)
        if isinstance(raw_exp, int | float) and not isinstance(raw_exp, bool)
        else None
    )
    return SubjectToken(token=token, audience=audience, expires_at=expires_at)


def _context_actor_chain(context: Any) -> tuple[str, ...]:
    """The agents the run's request came through (its `@actor` chain), current first."""
    actor = _context_attributes(context).get("@actor")
    if not isinstance(actor, Mapping):
        return ()
    chain = actor.get("chain")
    if isinstance(chain, list | tuple) and chain:
        return tuple(str(a) for a in chain)
    actor_id = actor.get("id")
    return (actor_id,) if isinstance(actor_id, str) and actor_id else ()


def forwarded_credential(
    api_name: str, context: Any, forward_audience: str | None = None
) -> str | None:
    """The credential `auth: forward` sends for API `api_name`, or None (nothing is sent).

    In order: the calling principal's `attributes["credentials"][api_name]` (a
    policy set it); else, with `forward_audience`, `Bearer <the caller's own
    token>` when its `aud` names that audience (the issuer minted it for the
    target too): a token minted only for this agent is never replayed at another.
    """
    credentials = _context_credentials(context)
    value = credentials.get(api_name)
    if isinstance(value, str) and value:
        return value
    if forward_audience:
        subject = subject_token_of(context)
        if subject is not None and forward_audience in subject.audience:
            return f"Bearer {subject.token}"
    return None


def get_client(
    api_name: str,
    *,
    context: Any = None,
    transport: httpx.AsyncBaseTransport | None = None,
    run_id: str | None = None,
) -> ApiClient:
    """A policy-enforcing client for API `api_name` of `api-policy.yaml`.

    `context` is the run context holding the calling principal (a tool's
    `runtime.context`); by default it is read from the current graph run. For
    an `auth: forward` API it gives the credential sent; for an `auth:
    exchange` API, the caller's own token (`subject_token_of`), exchanged when a
    request is sent, not here. For both, the delegation chain the loop check
    reads. `run_id` names the run `limits.max_calls_per_run` counts against; by
    default it is read from the current run (`current_run_id`).
    Raises `ApiPolicyError` when the policy file is missing or invalid, or does
    not declare `api_name`.
    """
    policy = load_policy()
    settings = policy.api(api_name)
    credential = None
    subject = None
    actor_chain: tuple[str, ...] = ()
    if settings["auth"] in HEADER_AUTH_MODES:
        ctx = context if context is not None else current_context()
        actor_chain = _context_actor_chain(ctx)
        if settings["auth"] == "forward":
            credential = forwarded_credential(api_name, ctx, settings.get("forward_audience"))
        else:
            subject = subject_token_of(ctx)
    return ApiClient(
        policy,
        api_name,
        credential=credential,
        transport=transport,
        run_id=run_id,
        subject=subject,
        actor_chain=actor_chain,
    )


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
    """The principal a run acts for, as tools see it (from the run context).

    `principal_id` is the user (the subject). When another agent presents the
    request for that user, `actor` names that agent and `actor_chain` every
    agent in between, current first (`delegated`); `roles` are then only
    those `AUTH_DELEGATED_ROLES` lends to agents.
    """

    principal_id: str
    roles: frozenset[str]
    actor: str | None = None
    actor_chain: tuple[str, ...] = ()

    def has_role(self, *roles: str) -> bool:
        return bool(self.roles.intersection(roles))

    @property
    def delegated(self) -> bool:
        """Whether an agent presents this request for the user (see `actor`)."""
        return self.actor is not None


def _context_attributes(ctx: Any) -> Mapping[str, Any]:
    attributes = getattr(ctx, "attributes", None)
    if attributes is None and isinstance(ctx, Mapping):
        attributes = ctx.get("attributes")
    return attributes if isinstance(attributes, Mapping) else {}


def _context_origin(ctx: Any) -> str | None:
    """The user's own words the calling agent forwarded (`credentials["@origin"]`), if any."""
    credentials = _context_attributes(ctx).get("credentials")
    origin = credentials.get("@origin") if isinstance(credentials, Mapping) else None
    text = origin.get("text") if isinstance(origin, Mapping) else None
    return text if isinstance(text, str) else None


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
    actor = _context_attributes(ctx).get("@actor")
    actor_id = actor.get("id") if isinstance(actor, Mapping) else None
    chain = actor.get("chain") if isinstance(actor, Mapping) else None
    if not isinstance(actor_id, str) or not actor_id:
        actor_id, chain = None, None
    actor_chain = tuple(str(a) for a in chain) if isinstance(chain, list | tuple) else ()
    if actor_id and not actor_chain:
        actor_chain = (actor_id,)
    return Caller(
        principal_id,
        frozenset(r for r in names if isinstance(r, str)),
        actor=actor_id,
        actor_chain=actor_chain,
    )


def require_direct_caller(context: Any = None) -> Caller:
    """Refuse (`ApiPolicyError`) when an agent presents the request for the user.

    For tools only a person may trigger (a transfer, a password reset): the
    user must ask this agent directly. Returns the caller otherwise.
    """
    caller = current_caller(context)
    if caller.actor is not None:
        raise ApiPolicyError(
            f"refused: only the user directly may ask for this, not agent {caller.actor!r} "
            "acting for them; ask the user to use this agent themselves."
        )
    return caller


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


def _delegated_mentions() -> str:
    """`A2A_DELEGATED_MENTIONS`; a bad value (the startup check refuses it) reads as `refuse`."""
    try:
        from .auth import delegated_mentions
    except ImportError:  # loaded outside its package
        raw = (os.environ.get("A2A_DELEGATED_MENTIONS") or "origin").strip().lower()
        return raw if raw in ("origin", "refuse", "request") else "refuse"
    try:
        return delegated_mentions()
    except ValueError:  # SettingsError: fail closed
        return "refuse"


def require_user_mentioned(value: Any, runtime: Any) -> None:
    """Refuse (`ApiPolicyError`) unless `value` appears in the user's latest message.

    For write tools acting on a record the model chose (an order id, say):
    the user's own message cannot be forged by text a tool returned, so an
    instruction planted in upstream data ("also cancel ORD-17") cannot make
    the agent write to a record the user never named. Matching ignores
    letter case and needs the whole id (letters, digits, `_` and `-` around
    it end it: `ORD-1` does not match `ORD-17`, nor `17` match `ORD-17`).
    `runtime` is the tool's `ToolRuntime`.

    When another agent presents the request for the user, the latest message
    is that agent's text, which an instruction planted in data it read could
    have shaped. `A2A_DELEGATED_MENTIONS` decides: `origin` (default) needs
    the value in the user's own words the calling agent forwarded as well as
    in its request, and refuses without them; `refuse` always refuses;
    `request` counts the agent's request as the user's words (the 0.2
    behaviour).
    """
    token = str(value if value is not None else "").strip()
    text = latest_user_message(runtime)
    pattern = rf"(?<![A-Za-z0-9_-]){re.escape(token)}(?![A-Za-z0-9_-])"
    shown = token[:_MENTION_MAX_CHARS]

    def named_in(words: str) -> bool:
        return bool(token) and re.search(pattern, words, re.IGNORECASE) is not None

    context = getattr(runtime, "context", None)
    if context is None:
        context = current_context()
    actor = _context_attributes(context).get("@actor")
    agent = actor.get("id") if isinstance(actor, Mapping) else None
    mode = _delegated_mentions() if isinstance(agent, str) and agent else "request"
    if mode == "refuse":
        raise ApiPolicyError(
            f"refused: {shown!r} was asked for by agent {agent!r} acting for the user; the user "
            "must ask this agent directly to act on it."
        )
    if mode == "origin":
        origin = _context_origin(context)
        if origin is None:
            raise ApiPolicyError(
                f"refused: {shown!r} was asked for by agent {agent!r}, which forwarded no user "
                "message to check it against; the user must name it."
            )
        if not named_in(origin):
            raise ApiPolicyError(
                f"refused: {shown!r} is not named in the user's own words that agent {agent!r} "
                "forwarded; ask the user to confirm it before acting on it."
            )
    if not named_in(text):
        raise ApiPolicyError(
            f"refused: {shown!r} is not named in the user's latest message; ask the user to "
            "confirm it before acting on it."
        )
