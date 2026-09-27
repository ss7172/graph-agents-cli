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

"""HTTP clients for the CLI's own requests, and how they treat proxies.

httpx takes ``HTTP_PROXY``, ``HTTPS_PROXY``, ``ALL_PROXY`` and ``NO_PROXY`` from
the environment (and, on macOS, the system proxy settings), and it builds a
transport for every proxy it finds when a client is created, whatever host the
request is for. So a proxy it cannot use (an unknown scheme, or a SOCKS proxy
without the ``socksio`` package) breaks every request, and ``NO_PROXY`` does
not help. Coding-agent sandboxes set exactly such variables (Codex's network
proxy: an HTTP proxy in ``HTTP(S)_PROXY`` and a ``socks5h://`` one in
``ALL_PROXY``).

The rules here:

* A request to this machine (``localhost``, ``*.localhost``, a loopback or
  unspecified address) never goes through a proxy: its client ignores the
  environment (``trust_env=False``). That is the local server of ``run``,
  ``eval`` and ``approvals``, and the playground.
* Any other request honours the environment's proxies, SOCKS included
  (graph-agents-cli depends on ``httpx[socks]``). A proxy setting httpx cannot
  use is a :class:`ProxyConfigError`: one line that names the variable, exit 3.
"""

from __future__ import annotations

import importlib.util
import ipaddress
import os
import urllib.request
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import click
import httpx

EXIT_CONFIG_ERROR = 3
# The proxy schemes httpx can use; socks5 and socks5h need the socksio package.
SUPPORTED_PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")
SOCKS_SCHEMES = ("socks5", "socks5h")


class ProxyConfigError(click.ClickException):
    """A proxy setting in the environment that the CLI's HTTP client cannot use (exit 3)."""

    exit_code = EXIT_CONFIG_ERROR


def is_local(url: str | httpx.URL) -> bool:
    """Whether ``url`` names this machine: localhost, a loopback or an unspecified address."""
    try:
        host = httpx.URL(url).host
    except (httpx.InvalidURL, TypeError, ValueError):
        return False
    host = host.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def _redacted(value: str) -> str:
    """``value`` without the ``user:password@`` of a proxy URL."""
    try:
        parts = urlsplit(value)
    except ValueError:
        return "<unparseable>"
    if "@" not in parts.netloc:
        return value
    return urlunsplit(parts._replace(netloc="***@" + parts.netloc.rsplit("@", 1)[1]))


def _variable(kind: str) -> str:
    """The environment variable httpx read the ``kind`` (http, https, all) proxy from."""
    for name in (f"{kind}_proxy", f"{kind.upper()}_PROXY"):
        if os.environ.get(name):
            return name
    return f"the system {kind.upper()} proxy setting"


def env_proxy_problem() -> str | None:
    """Why the environment's proxy settings would break an httpx client, or None when usable.

    It checks what httpx itself reads (``urllib.request.getproxies``): every proxy
    it would build a transport for, not only the one a request would use.
    """
    try:
        proxies = urllib.request.getproxies()
    except Exception:  # an unreadable system setting: httpx fails on it too, but say nothing
        return None
    for kind in ("all", "http", "https"):
        value = (proxies.get(kind) or "").strip()
        if not value:
            continue
        where = f"{_variable(kind)}={_redacted(value)}"
        scheme = value.split("://", 1)[0].lower() if "://" in value else "http"
        if scheme not in SUPPORTED_PROXY_SCHEMES:
            return (
                f"the proxy in {where} cannot be used: its scheme {scheme!r} is not one of "
                f"{', '.join(SUPPORTED_PROXY_SCHEMES)}. Fix or unset {_variable(kind)} "
                "(requests to this machine never use a proxy)."
            )
        if scheme in SOCKS_SCHEMES and importlib.util.find_spec("socksio") is None:
            return (
                f"the SOCKS proxy in {where} needs the socksio package, which this "
                "installation lacks: reinstall graph-agents-cli (it depends on httpx[socks]) "
                f"or unset {_variable(kind)} (requests to this machine never use a proxy)."
            )
    return None


def check_env_proxies(url: str | httpx.URL) -> None:
    """Raise :class:`ProxyConfigError` when a request to ``url`` would fail on a proxy setting.

    Nothing to check for a local ``url``: its requests ignore the environment.
    """
    if is_local(url):
        return
    problem = env_proxy_problem()
    if problem:
        raise ProxyConfigError(f"Cannot send requests to {_origin(url)}: {problem}")


def _origin(url: str | httpx.URL) -> str:
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError, ValueError):
        return "the agent"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.host}{port}"


def _options(url: str | httpx.URL, kwargs: dict[str, Any]) -> dict[str, Any]:
    if is_local(url):
        return {**kwargs, "trust_env": False}
    check_env_proxies(url)
    return kwargs


def _built(factory: Any, url: str | httpx.URL, kwargs: dict[str, Any]) -> Any:
    options = _options(url, kwargs)
    try:
        return factory(**options)
    except ImportError as exc:  # a proxy transport whose package is missing
        raise ProxyConfigError(f"Cannot send requests to {_origin(url)}: {exc}") from exc


def client(url: str | httpx.URL, **kwargs: Any) -> httpx.Client:
    """An ``httpx.Client`` for requests to ``url``'s origin (see the module's proxy rules)."""
    return _built(httpx.Client, url, kwargs)


def async_client(url: str | httpx.URL, **kwargs: Any) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` for requests to ``url``'s origin (see the module's proxy rules)."""
    return _built(httpx.AsyncClient, url, kwargs)


def request(
    method: str,
    url: str | httpx.URL,
    *,
    timeout: float | httpx.Timeout | None = 5.0,
    **kwargs: Any,
) -> httpx.Response:
    """One request, like ``httpx.request``, with the module's proxy rules."""
    with client(url, timeout=timeout) as http:
        return http.request(method, url, **kwargs)


def get(url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
    """``GET url``, like ``httpx.get``, with the module's proxy rules."""
    return request("GET", url, **kwargs)


def delete(url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
    """``DELETE url``, like ``httpx.delete``, with the module's proxy rules."""
    return request("DELETE", url, **kwargs)
