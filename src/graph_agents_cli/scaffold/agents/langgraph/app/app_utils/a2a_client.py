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

"""Ask other agents over A2A 1.0 (JSON-RPC) for the user, and relay the user's approvals.

A peer is an API in `api-policy.yaml` with `protocol: a2a` (`graph-agents-cli
peer add` writes it, and generates `tools/a2a_peers.py`, which calls
`peer_tools`). Every request goes through the policy client (`get_client`), so
the policy governs every byte: the allowed JSON-RPC methods, the approve gate,
the credential (`auth: exchange` mints a token for the peer just before
sending), the limits and the response cap; the A2A SDK's own HTTP client is
never used.

`A2APeerClient(peer)` talks to one peer:

* Before the first call it reads the peer's agent card (through the policy,
  cached `A2A_CARD_TTL_S`, failures for 10 s) and refuses a peer whose card
  offers no A2A 1.x JSON-RPC interface at exactly the URL this agent calls
  (`<base URL env> + a2a.path`), or names another agent: a card's URL is never
  dialed.
* One conversation per thread, peer and user: the `contextId` is a UUID keyed
  with `PRINCIPAL_HASH_SALT` (`context_id_for`), so it is stable across turns
  and replicas, and nobody can guess it. Calls to one peer in one thread are
  serialized; different peers run in parallel.
* A peer's answer must be A2A 1.0 JSON-RPC; the reply is the last `response`
  artifact's text (at most `A2A_REPLY_MAX_CHARS`). A task that failed because
  the peer's thread was busy (`thread_busy` error part) is sent again, 3 times
  at most (0.5, 1 and 2 s apart).
* With `A2A_FORWARD_ORIGIN=auto` (the default), a message to a peer whose card
  declares the origin extension carries the user's own words: the user's
  latest message when the user asked this agent directly, else the words the
  agent calling this one forwarded; never text a model wrote. `off` never
  sends them.
* A call that would come back to this agent, or to an agent already in the
  request's delegation chain, is refused.

The approval relay (`A2APeerClient.relay`, the `approve_agent_action` tool):
the peer's pending approval is read from the peer itself (`GetTask`, its exact
`approval_json`; when the peer lost the task, its approvals ledger at
`GET /threads/{context_id}/approvals`), never from the model. A gate the peer
decides directly (`decide_with: direct`) is reported as `needs_direct_approval`.
Otherwise the decision is one context-addressed message naming the approval's
digest and, in its metadata, the approval it decides (`approving`); the
policy gates that message, so the person approves it here first, seeing what
will happen at the peer (`nested`, `effect`). The message is built the same on
every run, so the run resumed by the person's decision sends exactly what they
approved, once; a rejection (or an expiry) sends the same message rejecting,
ungated, so the peer's task ends at once.

Tool results reach the model fenced as untrusted text (`UntrustedToolResults`).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import uuid
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from {{cookiecutter.agent_directory}}.app_utils.api_client import (
    A2A_ERROR_PART_TYPE,
    A2A_ORIGIN_EXTENSION,
    APPROVING_KEY,
    EXCHANGE_KEY,
    NESTED_CALL_KEYS,
    ORIGIN_KEY,
    PROTOCOL_A2A,
    ApiCallError,
    ApiClient,
    ApiPolicyError,
    api_protocol,
    approval_effect,
    current_caller,
    current_context,
    get_client,
    latest_user_message,
    load_policy,
    origin_max_chars,
)
from {{cookiecutter.agent_directory}}.app_utils.limits import SettingsError

logger = logging.getLogger(__name__)

A2A_VERSION = "1.0"
CARD_PATH_SUFFIX = "/.well-known/agent-card.json"
# The operations a peer's policy entry names (`peer add` writes them).
CARD_OPERATION = "getAgentCard"
LEDGER_OPERATION = "listContextApprovals"
LEDGER_PATH = "/threads/{context_id}/approvals"
# JSON-RPC error codes of A2A.
TASK_NOT_FOUND = -32001
TASK_NOT_CANCELABLE = -32002
THREAD_BUSY = "thread_busy"
APPROVAL_REQUEST_TYPE = "approval_request"
# `A2A_FORWARD_ORIGIN`: `auto` sends the user's own words to a peer whose card declares the
# origin extension, `off` never does (the owner's decision of 2026-09-28: `auto`).
DEFAULT_A2A_FORWARD_ORIGIN = "auto"
FORWARD_ORIGIN_VALUES = ("auto", "off")
DEFAULT_CARD_TTL_S = 300
DEFAULT_REPLY_MAX_CHARS = 6000
CARD_FAILURE_TTL_S = 10.0
CARD_CACHE_MAX = 256
LOCKS_MAX = 4096
# What a peer says about itself, as the model reads it.
DESCRIPTION_MAX_CHARS = 300
STATUS_TEXT_MAX_CHARS = 1000
ERROR_MESSAGE_MAX_CHARS = 300
AGENT_SAID_MAX_CHARS = 1500
# A `thread_busy` answer is sent again after these pauses; a cancel the peer's other
# replica runs (-32002) once, after `CANCEL_RETRY_S`.
BUSY_RETRY_DELAYS_S = (0.5, 1.0, 2.0)
CANCEL_RETRY_S = 1.0
# The namespace of a decision's message id (uuid5: the same on every run).
_DECISION_NAMESPACE = uuid.UUID("7b3f6c1e-4a52-4d7c-9e0a-1f2d3c4b5a69")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def _int_setting(name: str, default: int, minimum: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(f"{name}={raw!r} is not a whole number.") from None
    if value < minimum:
        raise SettingsError(f"{name}={value} must be at least {minimum}.")
    return value


def card_ttl_s() -> int:
    """`A2A_CARD_TTL_S`: seconds a peer's checked agent card is reused (default 300; 0: never)."""
    return _int_setting("A2A_CARD_TTL_S", DEFAULT_CARD_TTL_S, 0)


def reply_max_chars() -> int:
    """`A2A_REPLY_MAX_CHARS`: the most of a peer's reply the model reads (default 6000)."""
    return _int_setting("A2A_REPLY_MAX_CHARS", DEFAULT_REPLY_MAX_CHARS, 1)


def forward_origin() -> str:
    """`A2A_FORWARD_ORIGIN`: `auto` (default) or `off`; `SettingsError` otherwise."""
    value = (os.environ.get("A2A_FORWARD_ORIGIN") or DEFAULT_A2A_FORWARD_ORIGIN).strip().lower()
    if value not in FORWARD_ORIGIN_VALUES:
        raise SettingsError(
            f"A2A_FORWARD_ORIGIN={value!r} must be one of {', '.join(FORWARD_ORIGIN_VALUES)}."
        )
    return value


def client_settings() -> None:
    """Check every A2A client setting (the app's startup check calls this)."""
    card_ttl_s()
    reply_max_chars()
    origin_max_chars()
    forward_origin()


def own_name() -> str:
    """This agent's A2A name (`A2A_NAME`, else its agent directory), as its peers know it."""
    return os.environ.get("A2A_NAME") or "{{cookiecutter.agent_directory}}"


# ---------------------------------------------------------------------------
# Errors and replies
# ---------------------------------------------------------------------------


class PeerRpcError(ApiCallError):
    """A peer answered a JSON-RPC call with an error; `code` is its JSON-RPC code."""

    def __init__(self, message: str, code: Any) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PeerReply:
    """What a peer answered, read from its task (or its direct message).

    `text` is the last `response` artifact's text (`A2A_REPLY_MAX_CHARS` at
    most), `status_text` the task's status message. `approvals` are the pending
    approvals the peer reported (exact values, from `approval_json`); None when
    the task waits for no approval, or reported none this way (an older agent:
    read its approvals ledger instead). `error_code` names why a task failed or
    a decision was refused (the peer's error part).
    """

    state: str
    text: str
    task_id: str | None
    context_id: str
    approvals: list[dict[str, Any]] | None = None
    status_text: str = ""
    error_code: str | None = None


def _clean(text: Any, limit: int) -> str:
    """`text` without control characters (newlines and tabs as spaces), at most `limit` long."""
    if not isinstance(text, str):
        return ""
    cleaned = _CONTROL.sub(" ", text).strip()
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 3].rstrip() + "..."


def _texts(parts: Any) -> str:
    return "".join(
        str(part.get("text"))
        for part in parts or []
        if isinstance(part, Mapping) and "text" in part
    )


def _data_parts(message: Any, kind: str) -> list[dict[str, Any]]:
    parts = message.get("parts") if isinstance(message, Mapping) else None
    return [
        part["data"]
        for part in parts or []
        if isinstance(part, Mapping)
        and isinstance(part.get("data"), Mapping)
        and part["data"].get("type") == kind
    ]


def _exact_approvals(message: Any) -> list[dict[str, Any]] | None:
    """The approvals a status message's approval request reports, as exact JSON (or None).

    Read from `approval_json` only: the request's `Struct` holds every number as
    a double, so an approval read from it could differ from the call it binds.
    """
    for data in _data_parts(message, APPROVAL_REQUEST_TYPE):
        text = data.get("approval_json")
        if not isinstance(text, str):
            return None
        try:
            approvals = json.loads(text)
        except ValueError:
            return None
        if isinstance(approvals, list):
            return [a for a in approvals if isinstance(a, dict)]
    return None


def reply_of(peer: str, task: Mapping[str, Any], context_id: str) -> PeerReply:
    """A `PeerReply` from a task as the peer's JSON-RPC answer holds it."""
    status = task.get("status") if isinstance(task.get("status"), Mapping) else {}
    message = status.get("message") if isinstance(status.get("message"), Mapping) else {}
    artifacts = [
        a for a in task.get("artifacts") or [] if isinstance(a, Mapping) and a.get("parts")
    ]
    responses = [a for a in artifacts if a.get("name") == "response"] or artifacts
    text = _texts(responses[-1].get("parts")) if responses else ""
    limit = reply_max_chars()
    errors = _data_parts(message, A2A_ERROR_PART_TYPE)
    code = errors[0].get("code") if errors else None
    return PeerReply(
        state=str(status.get("state") or "TASK_STATE_UNSPECIFIED"),
        text=text if len(text) <= limit else text[:limit] + " [truncated]",
        task_id=str(task["id"]) if task.get("id") else None,
        context_id=str(task.get("contextId") or context_id),
        approvals=_exact_approvals(message),
        status_text=_texts(message.get("parts")),
        error_code=str(code) if isinstance(code, str) else None,
    )


# ---------------------------------------------------------------------------
# Peers, context ids, cards, locks
# ---------------------------------------------------------------------------

# The peers `peer_tools` registered: name -> {"api", "approvals", "description"}.
_PEERS: dict[str, dict[str, str]] = {}


def context_id_for(thread_id: str, peer: str, subject: str) -> str:
    """The A2A `contextId` of this thread's conversation with `peer` (its API) for `subject`.

    A UUID made from an HMAC-SHA256 keyed with `PRINCIPAL_HASH_SALT` (plain
    SHA-256 without it) of this agent's A2A name, the thread, the peer's API and
    the user: the same on every turn and replica, not guessable with the salt,
    and not disclosing the thread id. Changing the salt starts new conversations.
    """
    data = "\x1f".join(("a2a-context", own_name(), thread_id, peer, subject)).encode("utf-8")
    salt = (os.environ.get("PRINCIPAL_HASH_SALT") or "").strip()
    if salt:
        digest = hmac.new(salt.encode("utf-8"), data, hashlib.sha256).digest()
    else:
        digest = hashlib.sha256(data).digest()
    return str(uuid.UUID(bytes=digest[:16], version=4))


@dataclass
class PeerCard:
    """What the checked agent card says: its description and whether it reads the origin."""

    name: str
    description: str
    origin: bool


@dataclass
class _Cards:
    """Checked cards by (API, URL): a bounded LRU, failures kept briefly, one fetch at a time."""

    entries: OrderedDict[tuple[str, str], tuple[float, PeerCard | Exception]] = field(
        default_factory=OrderedDict
    )
    fetching: dict[tuple[str, str], asyncio.Future[PeerCard]] = field(default_factory=dict)


_CARDS = _Cards()
# Per-context locks (one conversation runs one turn at a time), a bounded LRU of the
# unlocked ones.
_LOCKS: OrderedDict[str, asyncio.Lock] = OrderedDict()


def _lock_for(context_id: str) -> asyncio.Lock:
    lock = _LOCKS.get(context_id)
    if lock is None:
        lock = _LOCKS[context_id] = asyncio.Lock()
    _LOCKS.move_to_end(context_id)
    if len(_LOCKS) > LOCKS_MAX:
        for key in [k for k, v in _LOCKS.items() if not v.locked()][: len(_LOCKS) - LOCKS_MAX]:
            del _LOCKS[key]
    return lock


def reset_a2a_client() -> None:
    """Forget the cards and locks (tests)."""
    _CARDS.entries.clear()
    _CARDS.fetching.clear()
    _LOCKS.clear()


def _url_key(url: str) -> tuple[str, str, int | None, str]:
    """A URL as the card check compares it: scheme, host, port, path (one trailing slash off)."""
    parsed = httpx.URL(url)
    port = parsed.port or {"http": 80, "https": 443}.get(parsed.scheme)
    return parsed.scheme.lower(), (parsed.host or "").lower(), port, parsed.path.rstrip("/") or "/"


def _thread_id(runtime: Any) -> str | None:
    config = getattr(runtime, "config", None)
    if not isinstance(config, Mapping):
        try:
            from langgraph.config import get_config

            config = get_config()
        except Exception:  # outside a graph run
            return None
    thread_id = (config.get("configurable") or {}).get("thread_id")
    return str(thread_id) if thread_id else None


def _origin_of(context: Any) -> Mapping[str, Any] | None:
    attributes = getattr(context, "attributes", None)
    if attributes is None and isinstance(context, Mapping):
        attributes = context.get("attributes")
    credentials = attributes.get("credentials") if isinstance(attributes, Mapping) else None
    origin = credentials.get("@origin") if isinstance(credentials, Mapping) else None
    return origin if isinstance(origin, Mapping) else None


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


class A2APeerClient:
    """One peer (a `protocol: a2a` API), called for the current run's user. See the module doc.

    `peer` is a name `peer_tools` registered (`orders`), or the API's own name.
    `runtime` is the tool's `ToolRuntime` (its thread, its user's words, its
    context); `context` overrides the run context. `transport` is for tests.
    """

    def __init__(
        self,
        peer: str,
        *,
        runtime: Any = None,
        context: Any = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.runtime = runtime
        self.context = (
            context
            if context is not None
            else getattr(runtime, "context", None) or current_context()
        )
        self.transport = transport
        self.peer, self.api = self._resolve(peer)
        settings = load_policy().api(self.api)
        if api_protocol(settings) != PROTOCOL_A2A:
            raise ApiPolicyError(f"API {self.api!r} is not an A2A peer (protocol: a2a).")
        self.settings = settings
        self.path = str((settings.get("a2a") or {}).get("path") or f"/a2a/{self.peer}")

    @staticmethod
    def _resolve(peer: str) -> tuple[str, str]:
        name = str(peer or "").strip()
        registered = _PEERS.get(name)
        if registered is not None:
            return name, registered["api"]
        apis = load_policy().apis
        for api in (name, f"{name}_agent"):
            if api in apis and api_protocol(apis[api]) == PROTOCOL_A2A:
                return name, api
        if name in apis:
            raise ApiPolicyError(f"API {name!r} is not an A2A peer (protocol: a2a).")
        known = ", ".join(_PEERS) or "(none)"
        raise ApiPolicyError(f"unknown agent {name!r}; ask one of: {known}")

    # -- plumbing -----------------------------------------------------------------

    def _client(self) -> ApiClient:
        return get_client(self.api, context=self.context, transport=self.transport)

    def _base_url(self) -> str:
        return str(self._client().base_url()).rstrip("/")

    def _audience(self) -> str | None:
        exchange = self.settings.get(EXCHANGE_KEY)
        if self.settings.get("auth") == "exchange" and isinstance(exchange, Mapping):
            return str(exchange.get("audience") or "") or None
        return str(self.settings.get("forward_audience") or "") or None

    def check_loop(self) -> None:
        """Refuse a call that would come back to this agent or to one before it in the chain.

        The peer is known by its name, the last segment of its `a2a.path` (its
        A2A name) and its audience; each is checked as `token_exchange.loop_problem`
        checks a delegation target: this agent's own names (its A2A name, its
        audiences) and the agents the request came through (T9).
        """
        from {{cookiecutter.agent_directory}}.app_utils.token_exchange import loop_problem

        chain = current_caller(self.context).actor_chain
        for name in (self.peer, self.path.rstrip("/").rsplit("/", 1)[-1], self._audience()):
            problem = loop_problem(name, chain) if name else None
            if problem:
                raise ApiPolicyError(f"{problem}; refused, nothing was sent.")

    def context_id(self) -> str:
        """This thread's conversation with the peer, for this user (`context_id_for`)."""
        thread_id = _thread_id(self.runtime)
        if not thread_id:
            raise ApiPolicyError(
                f"calling {self.peer} needs an agent run (a thread): refused, nothing was sent."
            )
        return context_id_for(thread_id, self.api, current_caller(self.context).principal_id)

    async def _rpc(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        rpc_id: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """One JSON-RPC 2.0 request to the peer; its `result`. Errors as the model reads them."""
        request_id = rpc_id or uuid.uuid4().hex
        body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)}
        try:
            answer = await self._client().post(
                self.path, json_body=body, headers={"A2A-Version": A2A_VERSION, **(headers or {})}
            )
        except ApiCallError as exc:
            if exc.status_code == 401:
                raise ApiCallError(
                    f"{self.peer} refused the credential (401): check exchange.audience and "
                    f"{self.peer}'s AUTH_JWT_AUDIENCE",
                    status_code=401,
                    body=exc.body,
                ) from None
            raise
        if (
            not isinstance(answer, Mapping)
            or answer.get("jsonrpc") != "2.0"
            or answer.get("id") != request_id
            or ("result" not in answer and "error" not in answer)
        ):
            raise ApiCallError(f"{self.peer} answered with something that is not A2A 1.0 JSON-RPC")
        if "error" in answer:
            error = answer["error"] if isinstance(answer["error"], Mapping) else {}
            code = error.get("code")
            text = _clean(error.get("message"), ERROR_MESSAGE_MAX_CHARS)
            raise PeerRpcError(f"{self.peer} refused {method}: {code} {text}".rstrip(), code)
        return answer["result"]

    def _task_reply(self, result: Any, context_id: str) -> PeerReply:
        """A `PeerReply` from a `SendMessage` result (a task or a message) or a task."""
        from a2a.types.a2a_pb2 import SendMessageResponse, Task
        from google.protobuf import json_format

        if not isinstance(result, Mapping):
            raise ApiCallError(f"{self.peer} answered with something that is not A2A 1.0 JSON-RPC")
        try:
            if "task" in result or "message" in result:
                json_format.ParseDict(result, SendMessageResponse(), ignore_unknown_fields=True)
            else:
                json_format.ParseDict(result, Task(), ignore_unknown_fields=True)
        except json_format.ParseError:
            raise ApiCallError(
                f"{self.peer} answered with something that is not A2A 1.0 JSON-RPC"
            ) from None
        if isinstance(result.get("message"), Mapping):
            message = result["message"]
            text = _texts(message.get("parts"))
            return PeerReply(
                state="TASK_STATE_COMPLETED",
                text=text[: reply_max_chars()],
                task_id=None,
                context_id=str(message.get("contextId") or context_id),
            )
        task = result["task"] if isinstance(result.get("task"), Mapping) else result
        return reply_of(self.peer, task, context_id)

    # -- the card -----------------------------------------------------------------

    async def card(self) -> PeerCard:
        """The peer's checked agent card (cached; one fetch at a time). See the module doc."""
        url = self._base_url()
        key = (self.api, url)
        loop = asyncio.get_running_loop()
        cached = _CARDS.entries.get(key)
        if cached is not None and cached[0] > loop.time():
            _CARDS.entries.move_to_end(key)
            if isinstance(cached[1], Exception):
                raise cached[1]
            return cached[1]
        pending = _CARDS.fetching.get(key)
        if pending is not None:
            return await asyncio.shield(pending)
        future: asyncio.Future[PeerCard] = loop.create_future()
        _CARDS.fetching[key] = future
        try:
            card = await self._fetch_card(url)
        except (ApiCallError, ApiPolicyError) as exc:
            if not isinstance(exc, ApiPolicyError):
                self._keep_card(key, exc, CARD_FAILURE_TTL_S)
            future.set_exception(exc)
            future.exception()  # retrieved: a waiter may never come
            raise
        finally:
            _CARDS.fetching.pop(key, None)
        self._keep_card(key, card, float(card_ttl_s()))
        future.set_result(card)
        return card

    @staticmethod
    def _keep_card(key: tuple[str, str], value: PeerCard | Exception, ttl: float) -> None:
        if ttl <= 0:
            return
        _CARDS.entries[key] = (asyncio.get_running_loop().time() + ttl, value)
        _CARDS.entries.move_to_end(key)
        while len(_CARDS.entries) > CARD_CACHE_MAX:
            _CARDS.entries.popitem(last=False)

    async def _fetch_card(self, url: str) -> PeerCard:
        from a2a.types import AgentCard
        from google.protobuf import json_format

        try:
            data = await self._client().get(
                f"{self.path}{CARD_PATH_SUFFIX}",
                operation_id=CARD_OPERATION,
                headers={"A2A-Version": A2A_VERSION},
            )
        except ApiCallError as exc:
            if exc.status_code == 401:
                raise ApiCallError(
                    f"{self.peer} refused the credential (401): check exchange.audience and "
                    f"{self.peer}'s AUTH_JWT_AUDIENCE",
                    status_code=401,
                ) from None
            raise
        card = AgentCard()
        try:
            json_format.ParseDict(data, card, ignore_unknown_fields=True)
        except (json_format.ParseError, TypeError, AttributeError):
            raise ApiCallError(
                f"{self.peer}'s agent card is not an A2A agent card; nothing was sent."
            ) from None
        expected = f"{url}{self.path}"
        versions = [
            i
            for i in card.supported_interfaces
            if i.protocol_binding.upper() == "JSONRPC" and i.protocol_version.startswith("1.")
        ]
        if not versions:
            raise ApiCallError(
                f"{self.peer}'s agent card offers no A2A 1.x JSON-RPC interface; nothing was sent."
            )
        if not any(_url_key(i.url) == _url_key(expected) for i in versions):
            env = self.settings.get("base_url_env")
            raise ApiCallError(
                f"{self.peer}'s agent card names {versions[0].url} as its A2A endpoint, not the "
                f"URL this agent calls ({env} + {self.path}): set the peer's APP_URL (appUrl in "
                "its chart values); nothing was sent."
            )
        segment = self.path.rstrip("/").rsplit("/", 1)[-1]
        if card.name != segment:
            raise ApiCallError(
                f"{self.peer}'s agent card is agent {card.name!r}, not {segment!r} (the last "
                f"segment of its a2a.path, {self.path}); nothing was sent."
            )
        return PeerCard(
            name=card.name,
            description=_clean(card.description, DESCRIPTION_MAX_CHARS),
            origin=any(e.uri == A2A_ORIGIN_EXTENSION for e in card.capabilities.extensions),
        )

    # -- the user's words -----------------------------------------------------------

    def _origin(self, card: PeerCard) -> dict[str, Any] | None:
        """The user's own words this call forwards (the origin extension), or None.

        Sent only under `A2A_FORWARD_ORIGIN=auto` to a peer whose card declares the
        extension. The user's latest message when the user asked this agent
        directly; the words the calling agent forwarded when an agent asked
        (none: nothing is sent, never a model's text). `hops` counts the agents.
        """
        if not card.origin or forward_origin() != "auto":
            return None
        if current_caller(self.context).delegated:
            forwarded = _origin_of(self.context)
            if forwarded is None or not isinstance(forwarded.get("text"), str):
                return None
            text, truncated = forwarded["text"], forwarded.get("truncated") is True
            hops = forwarded.get("hops")
            hops = int(hops) + 1 if isinstance(hops, int) and not isinstance(hops, bool) else 2
        else:
            text = latest_user_message(self.runtime) if self.runtime is not None else ""
            truncated, hops = False, 1
        if not text:
            return None
        cap = origin_max_chars()
        return {"text": text[:cap], "truncated": truncated or len(text) > cap, "hops": hops}

    @staticmethod
    def _extension(
        origin: Mapping[str, Any] | None, approving: Mapping[str, Any] | None = None
    ) -> tuple[dict[str, Any] | None, dict[str, str]]:
        """A message's metadata and headers for the origin extension (both empty: none)."""
        data: dict[str, Any] = {}
        if origin is not None:
            data[ORIGIN_KEY] = dict(origin)
        if approving is not None:
            data[APPROVING_KEY] = dict(approving)
        headers = {"A2A-Extensions": A2A_ORIGIN_EXTENSION} if origin is not None else {}
        return ({A2A_ORIGIN_EXTENSION: data} if data else None), headers

    # -- calls ------------------------------------------------------------------------

    async def _prepare(self) -> tuple[PeerCard, str]:
        self.check_loop()
        context_id = self.context_id()
        return await self.card(), context_id

    async def send(self, text: str) -> PeerReply:
        """Send `text` to the peer (a blocking `SendMessage`) and return its reply.

        A task the peer failed because its thread was busy is sent again, 3 times
        at most. Calls to one peer in one thread are serialized.
        """
        from a2a.types.a2a_pb2 import (
            Message,
            Part,
            Role,
            SendMessageConfiguration,
            SendMessageRequest,
        )
        from google.protobuf import json_format

        card, context_id = await self._prepare()
        async with _lock_for(context_id):
            for delay in (*BUSY_RETRY_DELAYS_S, None):
                request = SendMessageRequest(
                    message=Message(
                        message_id=uuid.uuid4().hex,
                        context_id=context_id,
                        role=Role.ROLE_USER,
                        parts=[Part(text=text)],
                    ),
                    configuration=SendMessageConfiguration(history_length=0),
                )
                params = json_format.MessageToDict(request)
                metadata, headers = self._extension(self._origin(card))
                if metadata is not None:
                    params["message"]["metadata"] = metadata  # plain JSON: exact values
                result = await self._rpc("SendMessage", params, headers=headers)
                reply = self._task_reply(result, context_id)
                if reply.error_code != THREAD_BUSY or delay is None:
                    return reply
                logger.info("A2A peer %s was busy; sending again in %g s", self.peer, delay)
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    async def get_task(self, task_id: str) -> PeerReply:
        """The peer's task `task_id` (`GetTask`, owner-scoped at the peer)."""
        from a2a.types.a2a_pb2 import GetTaskRequest
        from google.protobuf import json_format

        _card, context_id = await self._prepare()
        params = json_format.MessageToDict(GetTaskRequest(id=str(task_id), history_length=0))
        return self._task_reply(await self._rpc("GetTask", params), context_id)

    async def cancel(self, task_id: str) -> PeerReply:
        """Cancel the peer's task (`CancelTask`; the policy must allow it: `peer add --calls
        ask,status,cancel`). A task still running on another replica of the peer (-32002) is
        asked again once, after 1 s."""
        from a2a.types.a2a_pb2 import CancelTaskRequest
        from google.protobuf import json_format

        _card, context_id = await self._prepare()
        params = json_format.MessageToDict(CancelTaskRequest(id=str(task_id)))
        for retry in (True, False):
            try:
                return self._task_reply(await self._rpc("CancelTask", params), context_id)
            except PeerRpcError as exc:
                if exc.code != TASK_NOT_CANCELABLE:
                    raise
                if not retry:
                    raise ApiCallError(
                        f"the task is still running on another replica of {self.peer}; "
                        "try again later"
                    ) from None
            await asyncio.sleep(CANCEL_RETRY_S)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _ledger(self, context_id: str) -> list[dict[str, Any]]:
        """The peer's approvals on this conversation, from its approvals ledger (exact JSON)."""
        rows = await self._client().get(
            LEDGER_PATH, operation_id=LEDGER_OPERATION, path_params={"context_id": context_id}
        )
        if not isinstance(rows, list):
            raise ApiCallError(
                f"{self.peer}'s approvals answered with something that is not a list"
            )
        return [row for row in rows if isinstance(row, dict)]

    async def _pending(self, task_id: str | None) -> tuple[list[dict[str, Any]], PeerReply | None]:
        """What the peer waits on for this conversation: its pending approvals and the task.

        `GetTask` first (the task must be this conversation's: T6); when the peer
        lost the task (-32001), or reported its approvals only as a `Struct`, its
        approvals ledger, which is authoritative.
        """
        _card, context_id = await self._prepare()
        reply: PeerReply | None = None
        if task_id:
            try:
                reply = await self.get_task(task_id)
            except PeerRpcError as exc:
                if exc.code != TASK_NOT_FOUND:
                    raise
            if reply is not None and reply.context_id != context_id:
                raise ApiPolicyError(
                    f"task {task_id} of {self.peer} belongs to another conversation; refused."
                )
            if reply is not None and reply.state != "TASK_STATE_INPUT_REQUIRED":
                return [], reply
            if reply is not None and reply.approvals is not None:
                return [
                    a for a in reply.approvals if a.get("status", "pending") == "pending"
                ], reply
        rows = await self._ledger(context_id)
        pending = [a for a in rows if a.get("status") == "pending"]
        return sorted(pending, key=lambda a: str(a.get("created_at") or "")), reply

    async def pending_approvals(self, task_id: str | None) -> list[dict[str, Any]]:
        """The approvals the peer waits on in this conversation (exact values), oldest first."""
        pending, _reply = await self._pending(task_id)
        return pending

    def approving(self, approval: Mapping[str, Any]) -> dict[str, Any]:
        """What the person is asked to approve here: the peer's approval as the peer reported it."""
        call = {key: approval[key] for key in NESTED_CALL_KEYS if key in approval}
        return {
            "agent": self.peer,
            "approval_id": approval.get("approval_id"),
            "call": call,
            "reason": approval.get("reason"),
            "expires_at": approval.get("expires_at"),
            "digest": approval.get("digest"),
            "reported_by": self.peer,
            "decide_with": approval.get("decide_with"),
            "nested": approval.get("nested") if isinstance(approval.get("nested"), dict) else None,
        }

    def _decision_params(
        self,
        approval: Mapping[str, Any],
        decision: str,
        context_id: str,
        task_id: str | None,
        origin: Mapping[str, Any] | None,
        comment: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """The decision message, built the same on every run: the approval binds its body."""
        approval_id = str(approval.get("approval_id"))
        data: dict[str, Any] = {"approval_id": approval_id, "decision": decision}
        if comment:
            data["comment"] = comment
        if approval.get("digest"):
            data["digest"] = approval["digest"]
        message: dict[str, Any] = {
            "messageId": str(
                uuid.uuid5(_DECISION_NAMESPACE, f"{context_id}:{approval_id}:{decision}")
            ),
            "contextId": context_id,
            "role": "ROLE_USER",
            "parts": [{"data": data}],
        }
        if task_id:
            message["referenceTaskIds"] = [str(task_id)]
        metadata, headers = self._extension(origin, self.approving(approval))
        message["metadata"] = metadata
        return {"message": message, "configuration": {"historyLength": 0}}, headers

    async def decide(
        self,
        approval: Mapping[str, Any],
        decision: str,
        comment: str | None = None,
        *,
        task_id: str | None = None,
    ) -> PeerReply:
        """Send the person's decision on the peer's `approval`, on this conversation.

        `approve` is gated by this agent's policy (the person approves it here
        first); `reject` is sent at once.
        """
        card, context_id = await self._prepare()
        params, headers = self._decision_params(
            approval, decision, context_id, task_id, self._origin(card), comment
        )
        async with _lock_for(context_id):
            result = await self._rpc(
                "SendMessage",
                params,
                rpc_id=f"{decision}-{approval.get('approval_id')}",
                headers=headers,
            )
        return self._task_reply(result, context_id)

    async def relay(self, task_id: str) -> str:
        """The `approve_agent_action` tool: relay the person's decision on what the peer waits on.

        See the module doc. Returns what the model reads.
        """
        pending, reply = await self._pending(task_id)
        if not pending:
            state = f" (its task is {_state_name(reply.state)})" if reply is not None else ""
            return f"{self.peer} is not waiting for an approval{state}."
        approval, others = pending[0], pending[1:]
        also = (
            f" {self.peer} also waits on {len(others)} more approval(s) "
            f"({', '.join(str(o.get('approval_id')) for o in others)}): relay them one at a time."
            if others
            else ""
        )
        if approval.get("decide_with", "direct") != "relayed":
            return json.dumps(self.needs_direct(approval))
        try:
            answered = await self.decide(approval, "approve", task_id=task_id)
        except ApiPolicyError as exc:
            if exc.reason not in ("approval rejected", "approval expired"):
                raise
            # The person said no (or nobody answered): tell the peer, so its task ends now
            # instead of waiting for its own approval to expire. Rejecting is always safe.
            await self.decide(
                approval, "reject", "The user did not approve this action.", task_id=task_id
            )
            why = "rejected it" if exc.reason == "approval rejected" else "did not answer in time"
            return (
                f"The user {why}; {self.peer} was told and did nothing. Tell the user it was not "
                "done."
            )
        return describe(self.peer, answered, relays=True) + also

    def needs_direct(self, approval: Mapping[str, Any]) -> dict[str, Any]:
        """What the model reads for a gate the peer lets the person decide only directly."""
        effect = approval.get("effect") if isinstance(approval.get("effect"), dict) else None
        if effect is None:
            effect = approval_effect(self.approving(approval))
        url = f"{self._base_url()}"
        return {
            "status": "needs_direct_approval",
            "agent": self.peer,
            "approval_id": approval.get("approval_id"),
            "effect": effect,
            "how": (
                f"graph-agents-cli approvals approve {approval.get('approval_id')} --url {url}, "
                f"or POST /threads/<conversation>/approvals/{approval.get('approval_id')} at "
                f"{self.peer} with the person's own token"
            ),
            "next_step": (
                f"{self.peer} lets only the person decide this at {self.peer} itself "
                "(decide_with: direct): tell the user exactly what it wants to do and how to "
                "approve it there. Never say it was done."
            ),
        }


def _state_name(state: str) -> str:
    return state.removeprefix("TASK_STATE_").lower().replace("_", "-")


def _what(approval: Mapping[str, Any]) -> str:
    what = f"{approval.get('method')} {approval.get('path')}"
    if approval.get("operation_id"):
        what += f" ({approval['operation_id']})"
    return what


def describe(peer: str, reply: PeerReply, *, relays: bool) -> str:
    """What the model reads for a peer's reply (`relays`: this agent may relay approvals)."""
    if reply.state == "TASK_STATE_COMPLETED":
        return reply.text or f"({peer} completed the task with an empty reply)"
    if reply.state == "TASK_STATE_INPUT_REQUIRED" and reply.approvals:
        refused = (
            f"{peer} refused the decision ({reply.error_code}): "
            + _clean(reply.status_text, STATUS_TEXT_MAX_CHARS).split("Waiting for approval")[0]
            if reply.error_code
            else None
        )
        direct = [a for a in reply.approvals if a.get("decide_with", "direct") != "relayed"]
        waiting = [
            {
                "approval_id": a.get("approval_id"),
                "call": _what(a),
                "effect": _effect_line(a.get("effect")),
                "decided_by": "the person at " + peer
                if a in direct
                else "the person here (relayed)",
                "expires_at": a.get("expires_at"),
            }
            for a in reply.approvals
        ]
        if relays and not direct:
            status, step = (
                "needs_user_approval",
                "Tell the user exactly what the agent wants to do, then call approve_agent_action "
                "with this agent and task_id: the user is asked to approve or reject it there. "
                "Never assume approval.",
            )
        else:
            status, step = (
                "needs_direct_approval",
                f"Only the person can approve this, at {peer} itself: tell the user exactly what "
                f"{peer} wants to do and that they must approve it there. Never say it was done.",
            )
        result: dict[str, Any] = {
            "status": status,
            "agent": peer,
            "task_id": reply.task_id,
            "waiting": waiting,
            "agent_said": _clean(reply.text or reply.status_text, AGENT_SAID_MAX_CHARS),
            "next_step": step,
        }
        if refused:
            result["refused"] = refused.strip()
        return json.dumps(result)
    status_text = _clean(reply.status_text, STATUS_TEXT_MAX_CHARS)
    if reply.state == "TASK_STATE_INPUT_REQUIRED":
        return (
            f"{peer} needs more input (task {reply.task_id}): "
            f"{status_text or reply.text or 'no detail'}. Ask it again with the answer."
        )
    return f"{peer} ended the task as {_state_name(reply.state)}: {status_text or 'no detail'}"


def _effect_line(effect: Any) -> str | None:
    if not isinstance(effect, Mapping):
        return None
    via = [str(v) for v in effect.get("via") or [] if v]
    hops = via[:-1] if via and via[-1] == effect.get("agent") else via
    who = str(effect.get("agent")) + (f" (via {', '.join(hops)})" if hops else "")
    return f"{who} will {_what(effect)}"


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------

ASK_DOC = """Ask another agent to do something for the user, over A2A, and return its reply.

The agents you can ask:
{roster}

Args:
    agent: which agent to ask.
    request: a complete, self-contained instruction for that agent. Say who it is for only
        as "the user", and name every record id the user gave (order, invoice, SKU).

Several ask_agent calls in one turn run in parallel. If the reply says needs_user_approval,
call approve_agent_action with that agent and task_id; if it says needs_direct_approval,
tell the user what that agent wants to do and that they must approve it there.
"""

RELAY_DOC = """Ask the user to approve an action another agent is waiting on, then pass the user's
decision to that agent and return what it did.

Args:
    agent: the agent whose task reported needs_user_approval.
    task_id: that task's id, exactly as ask_agent returned it.
"""


def _check_peers(peers: Mapping[str, Mapping[str, str]]) -> dict[str, dict[str, str]]:
    """Every peer is a `protocol: a2a` API of the loaded policy; raise (fail closed) otherwise."""
    apis = load_policy().apis
    checked: dict[str, dict[str, str]] = {}
    for name, peer in peers.items():
        api = str(peer.get("api") or "")
        if api not in apis or api_protocol(apis[api]) != PROTOCOL_A2A:
            raise ApiPolicyError(
                f"peer {name!r}: API {api!r} is not a protocol: a2a API of api-policy.yaml; "
                "run `graph-agents-cli peer sync` (tools/a2a_peers.py is out of date)."
            )
        checked[str(name)] = {
            "api": api,
            "approvals": str(peer.get("approvals") or "deny"),
            "description": _clean(peer.get("description"), DESCRIPTION_MAX_CHARS),
        }
    return checked


def _warn_without_salt() -> None:
    dev = os.environ.get("APP_ENV") == "dev"
    if not dev and not (os.environ.get("PRINCIPAL_HASH_SALT") or "").strip():
        logger.warning(
            "A2A peers: PRINCIPAL_HASH_SALT is not set, so the contextIds sent to other agents "
            "are plain hashes of the thread and user ids; set it (a secret) so they cannot be "
            "guessed."
        )


def peer_tools(
    peers: Mapping[str, Mapping[str, str]],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[Any]:
    """The tools that ask `peers` (`{name: {"api", "approvals", "description"}}`).

    `ask_agent(agent, request)`, whose description lists the peers and what each
    does; and, when a peer has `approvals: relay`, `approve_agent_action(agent,
    task_id)` for those peers. Every peer must be a `protocol: a2a` API of the
    loaded policy, or this raises (the app then fails to start: fail closed).
    """
    from langchain.tools import ToolRuntime
    from langchain_core.tools import tool

    checked = _check_peers(peers)
    if not checked:
        return []
    _PEERS.update(checked)
    _warn_without_salt()
    names = tuple(checked)
    relay_names = tuple(n for n, p in checked.items() if p["approvals"] == "relay")
    roster = "\n".join(
        f"- {name}: {peer['description'] or '(no description: set it with `peer add --description`)'}"
        for name, peer in checked.items()
    )

    async def ask_agent(agent: str, request: str, runtime: Any) -> str:
        client = A2APeerClient(agent, runtime=runtime, transport=transport)
        reply = await client.send(request)
        return describe(client.peer, reply, relays=checked[client.peer]["approvals"] == "relay")

    ask_agent.__annotations__ = {
        "agent": Literal[names],  # type: ignore[valid-type]
        "request": str,
        "runtime": ToolRuntime[Any],
        "return": str,
    }
    ask_agent.__doc__ = ASK_DOC.format(roster=roster)
    tools: list[Any] = [tool(ask_agent)]
    if relay_names:

        async def approve_agent_action(agent: str, task_id: str, runtime: Any) -> str:
            if agent not in relay_names:
                raise ApiPolicyError(
                    f"this agent does not relay approvals to {agent!r}; relays go to: "
                    f"{', '.join(relay_names)}"
                )
            client = A2APeerClient(agent, runtime=runtime, transport=transport)
            return await client.relay(task_id)

        approve_agent_action.__annotations__ = {
            "agent": Literal[relay_names],  # type: ignore[valid-type]
            "task_id": str,
            "runtime": ToolRuntime[Any],
            "return": str,
        }
        approve_agent_action.__doc__ = RELAY_DOC
        tools.append(tool(approve_agent_action))
    return tools


def list_agents_tool(
    peers: Mapping[str, Mapping[str, str]],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Any:
    """A `list_agents` tool that reads each peer's agent card (for discovery at run time)."""
    from langchain.tools import ToolRuntime
    from langchain_core.tools import tool

    checked = _check_peers(peers)
    _PEERS.update(checked)

    async def list_agents(runtime: Any) -> str:
        async def one(name: str) -> str:
            try:
                card = await A2APeerClient(name, runtime=runtime, transport=transport).card()
            except (ApiCallError, ApiPolicyError) as exc:
                return f"- {name}: unavailable ({type(exc).__name__})"
            return f"- {name}: {card.description or checked[name]['description']}"

        return "\n".join(await asyncio.gather(*(one(name) for name in checked)))

    list_agents.__annotations__ = {"runtime": ToolRuntime[Any], "return": str}
    list_agents.__doc__ = "List the agents you can ask, with what each says it does (its card)."
    return tool(list_agents)


__all__ = [
    "A2APeerClient",
    "PeerCard",
    "PeerReply",
    "PeerRpcError",
    "client_settings",
    "context_id_for",
    "describe",
    "list_agents_tool",
    "peer_tools",
    "reset_a2a_client",
]
