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
they are sent (`gated`, `ApiPolicy.gate`). It is one rule, or a list of rules
when different calls need different approvers: the first rule in file order
that covers a call gates it, and the approval is asked of that rule's
approvers (they are recorded with it and decide it). Approval never widens
access: a gated call must pass the policy first, and denials still win. A gated call
pauses the agent run before anything is sent: the client describes the exact
request (`canonical_call`: API, method, URL with the rendered path, query,
JSON body, operation id and the tool's own headers), hashes it (`call_hash`)
and calls LangGraph's `interrupt()` with the approval payload (the call, with
the fields named in `redact=` masked, the tool and the model's stated purpose,
the approvers). The chat runtime records the approval and ends the stream
awaiting a decision (see `approvals.py`). When the run resumes, the tool runs
again from its start and this client rebuilds the request: it is sent only
when the decision approves exactly this request (the same hash) and the
approvals ledger marks that approval used (`set_approval_ledger`), so an
approval is sent once, never replayed. A rejected or expired approval, a
request that changed after it was approved, a used approval, or a gated call
made outside an agent run (nothing can pause it) raises `ApiPolicyError` and
sends nothing. After an approved call was sent, a second gated call in the
same tool call is refused (on its resume the tool would run again and meet the
first, already used, approval): make it in a new tool call. Code before a
gated call runs again on resume, so keep other side effects after it.

A decision is bound to the call it was taken for, not to the policy of the
moment: when the resumed tool rebuilds a request to the same API, method and
path as the paused call, the decision applies whatever the policy now says
about gating it (`_decision_waiting`). A rejected or expired call is never
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
`{"api", "method", "operation_id", "path"}` dicts naming each call it makes;
`graph-agents-cli lint` checks those declarations against the same rules.
"""

from __future__ import annotations

import contextlib
import hashlib
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
_SPACE_BY_DOT_RE = re.compile(r"\s\.|\.\s")

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
# (`timeout_s`). It is one such rule (a mapping), or a non-empty list of rules
# of that same shape when different calls need different approvers: a call is
# gated by the FIRST rule, in file order, whose `required_for` covers it, and a
# later rule that also covers it does not apply to it. It never widens access:
# a gated call must still be allowed, and denials still win. It belongs to the
# API only: on an operation entry the key is refused, with a pointer to
# `approval.required_for.operations`.
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
    """Errors of an API's `approval`: one rule (a mapping), or a non-empty list of rules."""
    where = f"{api_where}.{APPROVAL_KEY}"
    if isinstance(value, list):
        if not value:
            return [f"{where}: must not be empty; omit the key when no call needs approval"]
        errors: list[str] = []
        for index, rule in enumerate(value):
            errors.extend(_approval_rule_errors(f"{where}[{index}]", rule, api_where))
        return errors
    if not isinstance(value, Mapping):
        return [
            f"{where}: must be a mapping with required_for and approvers, or a non-empty "
            "list of such mappings (rules; the first that covers a call gates it)"
        ]
    return _approval_rule_errors(where, value, api_where)


def _approval_rule_errors(where: str, value: Any, api_where: str) -> list[str]:
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
    on the wire. Operation ids and paths are compared ignoring letter case,
    and a path's literal segments also cover their dot-suffixed spellings
    (``cancel.json``, ``cancel.``: ``path_matches`` with ``suffixes``).
    """
    if not _methods_match(entry, method):
        return None
    pinned_id = entry.get("operationId")
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


def approval_rules(api: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """An API's approval rules in file order: none, its one ``approval`` mapping, or its list."""
    approval = api.get(APPROVAL_KEY)
    if approval is None:
        return []
    return list(approval) if isinstance(approval, list) else [approval]


def approval_rule_label(api: Mapping[str, Any], index: int) -> str:
    """``approval`` for an API's one approval mapping, ``approval[<index>]`` in a list of rules."""
    if isinstance(api.get(APPROVAL_KEY), list):
        return f"{APPROVAL_KEY}[{index}]"
    return APPROVAL_KEY


def rule_covers(
    rule: Mapping[str, Any], method: str, operation_id: str | None, path: str | None
) -> str | None:
    """Which part of one approval rule's ``required_for`` covers the call, or None.

    ``required_for.methods`` covers a call with one of its methods (``["*"]``:
    every method). An entry of ``required_for.operations`` covers a call as a
    denial does (``denial_match``: fail closed), not as an allow.
    """
    required_for = rule.get("required_for") or {}
    method = method.upper()
    methods = [str(m).upper() for m in required_for.get("methods") or []]
    if ANY_METHOD in methods or method in methods:
        return f"required_for.methods {methods}"
    for entry in required_for.get("operations") or []:
        unnamed = denial_match(entry, method, operation_id or None, path or None)
        if unnamed is None:
            continue
        part = f"required_for.operations ({describe_operation(entry)})"
        if unnamed:
            part += f": the call names no {unnamed}, so it cannot be ruled out"
        return part
    return None


def gated(
    api: Mapping[str, Any],
    method: str,
    operation_id: str | None = None,
    path: str | None = None,
    *,
    template: str | None = None,
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
    apply to it). At runtime, ask with the path that is sent and, when there
    is one, the ``template`` it was rendered from: a rule covers the call when
    it covers either.
    """
    rules = approval_rules(api)
    if not rules:
        return None
    method = method.upper()
    operation_id = operation_id or None
    path = path or None
    template = template or None
    covering: list[tuple[int, str]] = []
    for index, rule in enumerate(rules):
        part = rule_covers(rule, method, operation_id, path)
        if part is None and template is not None:
            part = rule_covers(rule, method, operation_id, template)
        if part is not None:
            covering.append((index, part))
    if not covering:
        return None
    index, part = covering[0]
    rule = rules[index]
    listed = isinstance(api.get(APPROVAL_KEY), list)
    return ApprovalGate(
        approvers=tuple(str(a) for a in rule.get("approvers") or ()),
        timeout_s=int(rule.get("timeout_s", DEFAULT_APPROVAL_TIMEOUT_S)),
        rule=f"{approval_rule_label(api, index)}.{part}",
        index=index if listed else None,
        also=tuple(i for i, _ in covering[1:]),
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
        *,
        template: str | None = None,
    ) -> ApprovalGate | None:
        """The human approval the API's policy requires before the call is sent, or None.

        See `gated`: with a list of approval rules, the first rule in file order
        that covers the call (its sent `path`, or the `template` it was rendered
        from) gates it, with that rule's approvers. Ask only after `check`
        passed: approval never widens access.
        """
        return gated(self.api(api_name), method, operation_id, path, template=template)


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
    stopped: set[tuple[str, str, str]] = field(default_factory=set)


_TOOL_CALL: ContextVar[ToolCallScope | None] = ContextVar("api_tool_call", default=None)
# The same, for a tool run without the middleware's scope (in its own context).
_GATED_SENT: ContextVar[bool] = ContextVar("api_gated_sent", default=False)
_RESUMES_TAKEN: ContextVar[int] = ContextVar("api_resumes_taken", default=0)
_STOPPED: ContextVar[frozenset[tuple[str, str, str]]] = ContextVar(
    "api_stopped_calls", default=frozenset()
)


def call_identity(api: str, method: str, path: str) -> tuple[str, str, str]:
    """Which call a decision is bound to: the API, the method and the path sent.

    The path is compared normalised and ignoring letter case, as a gate
    compares it, so a rebuilt request that differs only in spelling is the
    same call (a body or query that changed is caught by the call hash).
    """
    return (str(api), str(method).upper(), normalize_path(str(path)).casefold())


def _is_decision(value: Any) -> bool:
    return isinstance(value, Mapping) and value.get("type") == APPROVAL_DECISION


def _decision_identity(decision: Mapping[str, Any]) -> tuple[str, str, str] | None:
    api, method, path = decision.get("api"), decision.get("method"), decision.get("path")
    if not (isinstance(api, str) and isinstance(method, str) and isinstance(path, str)):
        return None
    return call_identity(api, method, path)


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


def _stopped_calls() -> frozenset[tuple[str, str, str]] | set[tuple[str, str, str]]:
    scope = _TOOL_CALL.get()
    return scope.stopped if scope is not None else _STOPPED.get()


def _stop_call(identity: tuple[str, str, str]) -> None:
    scope = _TOOL_CALL.get()
    if scope is not None:
        scope.stopped.add(identity)
    else:
        _STOPPED.set(_STOPPED.get() | {identity})


def _decision_waiting(identity: tuple[str, str, str]) -> Mapping[str, Any] | None:
    """The decision the resumed run brought for this call, when the next resume value is one.

    A request the tool rebuilds on resume is the paused call when it has the
    paused call's API, method and path (`call_identity`); the decision then
    applies to it whatever the policy now says about gating it.
    """
    waiting = _next_resume_value(_resumes_taken())
    if _is_decision(waiting) and _decision_identity(waiting) == identity:
        return waiting
    return None


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
    ) -> PreparedRequest:
        """Every policy check before sending: the path, URL, query, headers, the credential
        sent, and the approval the call needs (`gate`, None when none)."""
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
        tool_headers = [
            (name, value)
            for name, value in request_headers.multi_items()
            if name.lower() not in {c.lower() for c in credentials}
        ]
        for name, value in credentials.items():
            request_headers[name] = value  # the policy's credential always wins
        secrets = tuple(credentials.values()) + tuple(
            value.split(" ", 1)[1] for value in credentials.values() if " " in value
        )
        return PreparedRequest(
            wire_path=wire_path,
            url=url,
            query=query,
            headers=request_headers,
            tool_headers=tool_headers,
            secrets=secrets,
            gate=self.gate_for(method, operation_id, path, wire_path, path_params is not None),
        )

    def gate_for(
        self,
        method: str,
        operation_id: str | None,
        path: str,
        wire_path: str,
        templated: bool,
    ) -> ApprovalGate | None:
        """The approval the API's `approval` requires before this call, or None.

        A rule covers the call when it covers the sent path or the template it
        was rendered from; the first such rule in file order gates it, and its
        approvers are the ones the approval is asked of (and bound to).
        Asked only once the policy allowed the call: approval never widens access.
        """
        return self.policy.gate(
            self.name, method, operation_id, wire_path, template=path if templated else None
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
        identity = call_identity(self.name, method, prepared.wire_path)
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
        return [b for b in found if call_identity(b.api, b.method, b.path) == identity]

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
        identity = call_identity(self.name, method, prepared.wire_path)
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
        else:
            # The paused call's gate is gone from the policy: its decision still binds it.
            assert waiting is not None
            asked = waiting.get("approvers")
            approvers = tuple(str(a) for a in asked) if isinstance(asked, list | tuple) else ()
            timeout_s = DEFAULT_APPROVAL_TIMEOUT_S
            rule = "the approval this call was paused for (the policy no longer gates it)"
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
            "timeout_s": timeout_s,
            "rule": rule,
            "call_hash": digest,
        }
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
        gate names now: a policy that changed while the call waited (a new
        image with other approvers, or one that no longer gates the call) is
        not satisfied by a decision taken under the old one.
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
