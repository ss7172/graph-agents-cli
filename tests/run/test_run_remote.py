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

"""Header building and URL classification (no Google token code)."""

from __future__ import annotations

import click
import httpx
import pytest
import respx

from graph_agents_cli import _remote
from graph_agents_cli._remote import build_headers, classify_url

# ---------------------------------------------------------------------------
# build_headers
# ---------------------------------------------------------------------------


def test_build_headers_parses_header_pairs_and_api_key_env():
    headers = build_headers(
        header=["X-Trace: abc", "Accept-Language:en"],
        env={"GRAPH_AGENTS_CLI_API_KEY": "secret"},
    )
    assert headers == {
        "X-Trace": "abc",
        "Accept-Language": "en",
        "Authorization": "Bearer secret",
    }


def test_build_headers_explicit_authorization_wins_over_env():
    headers = build_headers(
        header=["authorization: Bearer mine"], env={"GRAPH_AGENTS_CLI_API_KEY": "env"}
    )
    assert headers == {"authorization": "Bearer mine"}


def test_build_headers_without_api_key_sets_no_authorization():
    assert build_headers(env={}) == {}


def test_build_headers_folds_cookies_and_session_token():
    headers = build_headers(
        cookie=["session=abc", "tenant=acme"],
        session_token="tok-1",
        env={},
    )
    assert headers == {"Cookie": "session=abc; tenant=acme", "X-Session-Token": "tok-1"}


def test_build_headers_appends_cookies_to_explicit_cookie_header():
    headers = build_headers(header=["Cookie: a=1"], cookie=["b=2"], env={})
    assert headers == {"Cookie": "a=1; b=2"}


@pytest.mark.parametrize("bad", ["no-colon", ": empty-name"])
def test_build_headers_rejects_bad_header(bad):
    with pytest.raises(click.BadParameter):
        build_headers(header=[bad], env={})


@pytest.mark.parametrize("bad", ["noequals", "=value"])
def test_build_headers_rejects_bad_cookie(bad):
    with pytest.raises(click.BadParameter):
        build_headers(cookie=[bad], env={})


def test_build_headers_reads_os_environ_by_default(monkeypatch):
    monkeypatch.setenv("GRAPH_AGENTS_CLI_API_KEY", "from-env")
    assert build_headers()["Authorization"] == "Bearer from-env"


def test_no_google_token_code_remains():
    source = open(_remote.__file__, encoding="utf-8").read()
    for token in ("get_id_token", "get_access_token", "aiplatform", "reasoningEngines", "gcloud"):
        assert token not in source


# ---------------------------------------------------------------------------
# classify_url
# ---------------------------------------------------------------------------


@respx.mock(assert_all_called=False)
def test_classify_url_chat_does_not_probe(respx_mock):
    route = respx_mock.get(url__regex=r".*").mock(return_value=httpx.Response(200))
    target = classify_url("https://agent.example.com/", mode="chat", agent_directory="app")
    assert target.mode == "chat"
    assert target.base_url == "https://agent.example.com"
    assert target.a2a_base is None
    assert not route.called


@respx.mock
def test_classify_url_a2a_probes_agent_directory_first(respx_mock):
    card = {"name": "my-agent", "url": "https://agent.example.com/a2a/app"}
    route = respx_mock.get("https://agent.example.com/a2a/app/.well-known/agent-card.json").mock(
        return_value=httpx.Response(200, json=card)
    )
    target = classify_url(
        "https://agent.example.com",
        mode="a2a",
        agent_directory="app",
        headers={"Authorization": "Bearer k"},
    )
    assert target.mode == "a2a"
    assert target.a2a_base == "https://agent.example.com/a2a/app"
    assert target.card == card
    assert route.calls[0].request.headers["Authorization"] == "Bearer k"


@respx.mock
def test_classify_url_a2a_falls_back_to_root_card(respx_mock):
    respx_mock.get("https://agent.example.com/a2a/app/.well-known/agent-card.json").mock(
        return_value=httpx.Response(404)
    )
    respx_mock.get("https://agent.example.com/.well-known/agent-card.json").mock(
        return_value=httpx.Response(200, json={"name": "root"})
    )
    target = classify_url("https://agent.example.com", mode="a2a", agent_directory="app")
    assert target.a2a_base == "https://agent.example.com"


@respx.mock
def test_classify_url_a2a_without_card_lists_probes_and_hints(respx_mock):
    respx_mock.get(url__regex=r".*agent-card\.json").mock(
        return_value=httpx.Response(404, text="nope")
    )
    with pytest.raises(click.ClickException) as excinfo:
        classify_url("https://agent.example.com", mode="a2a", agent_directory="app")
    msg = str(excinfo.value)
    assert "/a2a/app/.well-known/agent-card.json" in msg
    assert "https://agent.example.com/.well-known/agent-card.json" in msg
    assert "--mode chat" in msg


@respx.mock
def test_classify_url_a2a_reports_unreachable(respx_mock):
    respx_mock.get(url__regex=r".*").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(click.ClickException) as excinfo:
        classify_url("https://agent.example.com", mode="a2a", agent_directory=None)
    assert "unreachable" in str(excinfo.value)


def test_classify_url_rejects_unknown_mode():
    with pytest.raises(click.BadParameter):
        classify_url("https://x", mode="adk")
