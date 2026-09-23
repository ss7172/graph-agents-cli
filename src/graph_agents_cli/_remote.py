# Copyright 2026 Google LLC
# Modifications Copyright 2026 graph-agents-cli contributors
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

"""Request headers and URL classification for talking to a remote agent.

Transport-neutral so every caller (``run``, ``eval generate``) agrees on which
credentials a ``--url`` gets and where the chat or A2A surface lives.
Credentials follow the auth policy of CONTRACTS section 6:

* ``--header 'Authorization: Bearer ...'`` or ``GRAPH_AGENTS_CLI_API_KEY``
  for ``shared-bearer``;
* ``--cookie name=value`` or ``--session-token value`` (sent as
  ``X-Session-Token``) for ``product-session``.

No cloud SDK is consulted; the CLI stores no credentials (D19).
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import NamedTuple

import click
import httpx

API_KEY_ENV = "GRAPH_AGENTS_CLI_API_KEY"
SESSION_TOKEN_HEADER = "X-Session-Token"
AGENT_CARD_PATH = "/.well-known/agent-card.json"

RUN_MODES: tuple[str, ...] = ("chat", "a2a")
DEFAULT_RUN_MODE = "chat"


def parse_header(value: str) -> tuple[str, str]:
    """Parse one ``Key: Value`` header string."""
    if ":" not in value:
        raise click.BadParameter(f"Invalid header format (expected 'Key: Value'): {value}")
    key, _, val = value.partition(":")
    key = key.strip()
    if not key:
        raise click.BadParameter(f"Invalid header format (empty name): {value}")
    return key, val.strip()


def parse_cookie(value: str) -> tuple[str, str]:
    """Parse one ``name=value`` cookie string."""
    if "=" not in value:
        raise click.BadParameter(f"Invalid cookie format (expected 'name=value'): {value}")
    name, _, val = value.partition("=")
    name = name.strip()
    if not name:
        raise click.BadParameter(f"Invalid cookie format (empty name): {value}")
    return name, val.strip()


def _has_header(headers: Mapping[str, str], name: str) -> bool:
    lname = name.lower()
    return any(k.lower() == lname for k in headers)


def build_headers(
    header: Sequence[str] = (),
    cookie: Sequence[str] = (),
    session_token: str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the request headers for a chat or A2A call.

    Precedence: explicit ``--header`` values are applied verbatim; ``--cookie``
    pairs are folded into one ``Cookie`` header (appended to an explicit
    ``Cookie`` header if one was given); ``--session-token`` becomes
    ``X-Session-Token``; and when no ``Authorization`` header was supplied,
    ``GRAPH_AGENTS_CLI_API_KEY`` from ``env`` (default ``os.environ``) becomes
    ``Authorization: Bearer <key>``.
    """
    environ = os.environ if env is None else env
    headers: dict[str, str] = {}
    for raw in header:
        key, val = parse_header(raw)
        headers[key] = val

    pairs = [parse_cookie(c) for c in cookie]
    if pairs:
        rendered = "; ".join(f"{k}={v}" for k, v in pairs)
        existing_key = next((k for k in headers if k.lower() == "cookie"), None)
        if existing_key is not None and headers[existing_key]:
            headers[existing_key] = f"{headers[existing_key]}; {rendered}"
        else:
            headers[existing_key or "Cookie"] = rendered

    if session_token:
        headers[SESSION_TOKEN_HEADER] = session_token

    if not _has_header(headers, "Authorization"):
        api_key = environ.get(API_KEY_ENV, "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

    return headers


class RemoteTarget(NamedTuple):
    """Where a query goes: the chat base URL or the resolved A2A base URL."""

    mode: str
    base_url: str
    # For ``a2a``: the base under which the agent card was found. ``None``
    # for ``chat``.
    a2a_base: str | None = None
    card: dict | None = None


def a2a_candidates(base_url: str, agent_directory: str | None) -> list[str]:
    """A2A base URLs to probe for a card, in order.

    The scaffolded app serves the card at ``/a2a/<agent_directory>`` (CONTRACTS
    section 5); the bare URL is tried second so a ``--url`` that already points
    at the A2A base, or a spec-canonical root card, still works.
    """
    base = base_url.rstrip("/")
    candidates: list[str] = []
    if agent_directory:
        candidates.append(f"{base}/a2a/{agent_directory}")
    candidates.append(base)
    return candidates


def classify_url(
    url: str,
    *,
    mode: str = DEFAULT_RUN_MODE,
    agent_directory: str | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 30.0,
) -> RemoteTarget:
    """Resolve a ``--url`` into a :class:`RemoteTarget` for ``mode``.

    ``chat`` needs no probing: the chat API lives at ``<url>/chat``. ``a2a``
    probes ``<url>/a2a/<agent_directory>/.well-known/agent-card.json`` and then
    ``<url>/.well-known/agent-card.json``; the first 200 wins.

    Raises :class:`click.ClickException` when no card is found (listing each
    probe) or :class:`click.BadParameter` for an unknown mode.
    """
    base = url.rstrip("/")
    if mode == "chat":
        return RemoteTarget(mode="chat", base_url=base)
    if mode != "a2a":
        raise click.BadParameter(f"Unknown mode {mode!r}; choose from {', '.join(RUN_MODES)}")

    failures: list[tuple[str, str]] = []
    for candidate in a2a_candidates(base, agent_directory):
        card_url = f"{candidate}{AGENT_CARD_PATH}"
        try:
            resp = httpx.get(card_url, headers=dict(headers or {}), timeout=timeout)
        except httpx.TransportError as exc:
            failures.append((card_url, f"unreachable: {exc}"))
            continue
        if resp.status_code == 200:
            try:
                card = resp.json()
            except ValueError:
                card = None
            return RemoteTarget(
                mode="a2a",
                base_url=base,
                a2a_base=candidate,
                card=card if isinstance(card, dict) else None,
            )
        failures.append((card_url, f"HTTP {resp.status_code}: {resp.text[:200]}"))

    detail = "\n".join(f"  {card_url}\n    {why}" for card_url, why in failures)
    hint = ""
    if base.endswith("/a2a"):
        hint += "\n  Pass the base service URL without the '/a2a' suffix."
    if any("HTTP 404" in why or "HTTP 405" in why for _, why in failures):
        hint += "\n  If this agent only exposes the chat API, try --mode chat instead."
    raise click.ClickException(f"Failed to fetch an A2A agent card:\n{detail}{hint}")
