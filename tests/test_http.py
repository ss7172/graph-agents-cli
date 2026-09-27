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

"""Proxies from the environment: never for this machine, usable or one clear error otherwise.

Coding-agent sandboxes (Codex's network proxy) set ``HTTP(S)_PROXY`` to an HTTP
proxy and ``ALL_PROXY`` to a ``socks5h://`` one. httpx builds a transport for
every proxy variable when a client is created, so before the fix ``run``,
``eval run`` and ``eval generate`` failed on their own loopback server with
``ImportError: ... socksio`` (or, with socksio, sent loopback requests to the
proxy).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from graph_agents_cli import _chat_client
from graph_agents_cli.run import _local_server

PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
# Port 9 (discard) is closed on every developer machine: a request sent there fails.
DEAD_HTTP_PROXY = "http://127.0.0.1:9"
DEAD_SOCKS_PROXY = "socks5h://127.0.0.1:9"


@pytest.fixture
def proxy_env(monkeypatch: pytest.MonkeyPatch):
    """No proxy variable from the developer's shell; the test sets its own."""
    for name in PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)

    def set_proxies(**values: str) -> None:
        for name, value in values.items():
            monkeypatch.setenv(name, value)

    return set_proxies


class _Server:
    """A loopback HTTP server that records the request targets it received."""

    def __init__(self, handler: type[BaseHTTPRequestHandler]) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.targets = []  # type: ignore[attr-defined]
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def targets(self) -> list[str]:
        return self.httpd.targets  # type: ignore[attr-defined]


class _HealthHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        pass

    def do_GET(self) -> None:
        self.server.targets.append(self.path)  # type: ignore[attr-defined]
        body = json.dumps({"status": "ok", "checkpointer": "memory"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def health_server() -> Iterator[_Server]:
    """A local server answering ``GET /health``; an HTTP proxy stub too (it records the
    absolute-form target a client sends to a proxy)."""
    server = _Server(_HealthHandler)
    server.thread.start()
    try:
        yield server
    finally:
        server.httpd.shutdown()
        server.httpd.server_close()


def test_local_server_health_never_goes_through_the_environments_proxies(proxy_env, health_server):
    """The readiness probe of `run` / `eval run`: failed with ImportError (socksio) before."""
    proxy_env(HTTP_PROXY=DEAD_HTTP_PROXY, HTTPS_PROXY=DEAD_HTTP_PROXY, ALL_PROXY=DEAD_SOCKS_PROXY)
    health = _local_server._fetch_health(health_server.port, timeout=5)
    assert health == {"status": "ok", "checkpointer": "memory"}
    assert health_server.targets == ["/health"]


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_chat_client_to_this_machine_ignores_the_proxies(proxy_env, health_server, host):
    proxy_env(HTTP_PROXY=DEAD_HTTP_PROXY, ALL_PROXY=DEAD_SOCKS_PROXY)
    base = f"http://{host}:{health_server.port}"
    assert _chat_client.get_health(base)["status"] == "ok"


def test_a_remote_request_still_goes_through_an_http_proxy(proxy_env, health_server):
    """HTTP(S)_PROXY keeps working for a remote agent, with a SOCKS ALL_PROXY beside it."""
    proxy = f"http://127.0.0.1:{health_server.port}"
    proxy_env(HTTP_PROXY=proxy, HTTPS_PROXY=proxy, ALL_PROXY=DEAD_SOCKS_PROXY)
    # The host does not resolve: only the proxy can answer for it.
    assert _chat_client.get_health("http://agent.example.invalid")["status"] == "ok"
    assert health_server.targets == ["http://agent.example.invalid/health"]


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        ("socks4://127.0.0.1:1080", "ALL_PROXY=socks4://127.0.0.1:1080"),
        ("ftp://user:s3cret@proxy.example:21", "ALL_PROXY=ftp://***@proxy.example:21"),
    ],
)
def test_an_unusable_proxy_is_a_one_line_config_error_for_a_remote_url(proxy_env, value, shown):
    from graph_agents_cli import _http

    proxy_env(ALL_PROXY=value)
    with pytest.raises(_http.ProxyConfigError) as excinfo:
        _chat_client.get_health("https://agent.example.invalid")
    message = excinfo.value.format_message()
    assert excinfo.value.exit_code == 3
    assert "\n" not in message
    assert shown in message
    assert "s3cret" not in message
    assert "Fix or unset ALL_PROXY" in message
    # A request to this machine is unaffected by the same setting.
    _http.check_env_proxies("http://127.0.0.1:8000")


def test_a_socks_proxy_without_socksio_names_the_fix(proxy_env, monkeypatch):
    import importlib.util

    from graph_agents_cli import _http

    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a: None if name == "socksio" else real_find_spec(name, *a),
    )
    proxy_env(ALL_PROXY=DEAD_SOCKS_PROXY)
    with pytest.raises(_http.ProxyConfigError, match="needs the socksio package") as excinfo:
        _http.client("https://agent.example.invalid")
    assert "unset ALL_PROXY" in excinfo.value.format_message()


def test_socks_proxies_are_supported():
    """graph-agents-cli depends on httpx[socks]: a SOCKS ALL_PROXY is usable remotely."""
    import importlib.util

    assert importlib.util.find_spec("socksio") is not None


@pytest.mark.parametrize(
    ("url", "local"),
    [
        ("http://127.0.0.1:18080", True),
        ("http://127.0.0.2/x", True),
        ("http://localhost:8000", True),
        ("http://LOCALHOST.:8000", True),
        ("http://agent.localhost", True),
        ("http://[::1]:8000", True),
        ("http://0.0.0.0:8000", True),
        ("https://agent.example.com", False),
        ("http://10.0.0.5:8000", False),
        ("http://localhost.example.com", False),
        ("not a url", False),
    ],
)
def test_is_local(url, local):
    from graph_agents_cli import _http

    assert _http.is_local(url) is local
