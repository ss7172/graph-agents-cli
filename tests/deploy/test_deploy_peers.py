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
"""`deploy`'s peer pre-checks: the rules the runtime applies to the agents it calls.

Outside dev a deploy refuses (exit 3, before anything is built) what the agent
would refuse at run time or at startup: a credential sent to another agent over
plain http to a host that is not cluster-internal, an `auth: exchange` API with
no token URL or client id, and a plain-http token URL. Everything else warns.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from click.testing import CliRunner

from graph_agents_cli._api_policy import policy_errors
from graph_agents_cli.deploy import _preflight
from graph_agents_cli.deploy._config import DeploySettings
from graph_agents_cli.deploy.cmd_deploy import cmd_deploy

TEMPLATE_CLIENT = (
    Path(__file__).resolve().parents[2]
    / "src/graph_agents_cli/scaffold/agents/langgraph/app/app_utils/api_client.py"
)

POLICY = {
    "apis": {
        "orders_agent": {
            "protocol": "a2a",
            "a2a": {"path": "/a2a/orders"},
            "base_url_env": "ORDERS_AGENT_URL",
            "auth": "exchange",
            "exchange": {"audience": "orders"},
            "allowed_methods": ["GET", "POST"],
            "allowed_operations": [
                {
                    "operationId": "getAgentCard",
                    "methods": ["GET"],
                    "path": "/a2a/orders/.well-known/agent-card.json",
                },
                {"rpc_method": "SendMessage", "methods": ["POST"], "path": "/a2a/orders"},
            ],
            "denied_operations": [{"a2a_operation": "approve"}],
        },
        "weather": {
            "base_url_env": "WEATHER_URL",
            "auth": "bearer",
            "token_env": "WEATHER_TOKEN",
            "allowed_methods": ["GET"],
        },
    }
}
GOOD_ENV = {
    "ORDERS_AGENT_URL": "http://orders-agent.orders-agent-prod.svc.cluster.local",
    "TOKEN_EXCHANGE_URL": "https://issuer.example.com/token",
    "TOKEN_EXCHANGE_CLIENT_ID": "concierge",
    "WEATHER_URL": "http://weather.example.com",
}


def _settings(keys: list[str] | None = None) -> DeploySettings:
    return DeploySettings(
        project_name="concierge",
        secret_keys=keys
        if keys is not None
        else ["TOKEN_EXCHANGE_CLIENT_SECRET", "PRINCIPAL_HASH_SALT"],
    )


def _findings(env: dict[str, str], env_name: str = "prod", **kw) -> _preflight.PeerFindings:
    values = {"env": {"APP_ENV": "dev" if env_name == "dev" else "prod", **env}}
    return _preflight.peer_findings(_settings(**kw), env_name, values, POLICY)


def test_the_policy_used_here_is_valid() -> None:
    assert policy_errors(POLICY) == []


def test_a_well_configured_peer_passes() -> None:
    findings = _findings(GOOD_ENV)
    assert findings.error() is None and findings.notes() == []


@pytest.mark.parametrize(
    "url",
    [
        "http://orders-agent",  # a single-label Service name
        "http://orders-agent.orders.svc",
        "http://orders-agent.orders.svc.cluster.local:8080",
        "http://127.0.0.1:8001",
        "http://localhost:8001",
        "https://orders.example.com",
    ],
)
def test_https_or_an_internal_host_may_carry_credentials(url: str) -> None:
    assert _findings({**GOOD_ENV, "ORDERS_AGENT_URL": url}).error() is None


def test_plain_http_to_a_public_host_is_refused_outside_dev_and_warns_in_dev() -> None:
    env = {**GOOD_ENV, "ORDERS_AGENT_URL": "http://orders.example.com"}
    error = _findings(env).error()
    assert error is not None
    assert (
        "ORDERS_AGENT_URL (http://orders.example.com) must use https outside APP_ENV=dev" in error
    )
    dev = _findings(env, "dev")
    assert dev.error() is None and any("must use https" in n for n in dev.notes())
    # APP_ENV other than exactly dev is a deployed environment, whatever the name.
    values = {"env": {"APP_ENV": "staging", **env}}
    assert _preflight.peer_findings(_settings(), "dev", values, POLICY).error() is not None


def test_an_api_that_is_not_a_peer_is_not_checked_here() -> None:
    # weather is plain http with a bearer token: a later release may refuse it (held default).
    assert _findings(GOOD_ENV).error() is None


def test_exchange_needs_the_token_url_and_the_client_id() -> None:
    env = {k: v for k, v in GOOD_ENV.items() if not k.startswith("TOKEN_EXCHANGE")}
    error = _findings(env).error() or ""
    assert "TOKEN_EXCHANGE_URL is not set" in error
    assert "TOKEN_EXCHANGE_CLIENT_ID is not set" in error


def test_a_plain_http_token_url_is_refused_unless_allowed_or_loopback() -> None:
    internal = "http://issuer.shared.svc.cluster.local:8080/token"
    error = _findings({**GOOD_ENV, "TOKEN_EXCHANGE_URL": internal}).error() or ""
    assert f"TOKEN_EXCHANGE_URL ({internal}) must use https" in error
    allowed = {**GOOD_ENV, "TOKEN_EXCHANGE_URL": internal, "TOKEN_EXCHANGE_ALLOW_HTTP": "true"}
    assert _findings(allowed).error() is None
    loopback = {**GOOD_ENV, "TOKEN_EXCHANGE_URL": "http://127.0.0.1:9000/token"}
    assert _findings(loopback).error() is None


def test_placeholders_are_left_to_the_placeholder_check() -> None:
    env = {
        **GOOD_ENV,
        "ORDERS_AGENT_URL": "http://CHANGE-ME",
        "TOKEN_EXCHANGE_URL": "http://CHANGE-ME",
    }
    assert _findings(env).error() is None


def test_what_secrets_keys_lacks_is_a_warning() -> None:
    findings = _findings(GOOD_ENV, keys=[])
    assert findings.error() is None
    notes = " ".join(findings.notes())
    assert "TOKEN_EXCHANGE_CLIENT_SECRET is not in secrets.keys" in notes
    assert "PRINCIPAL_HASH_SALT is not in secrets.keys" in notes
    unset = _findings({k: v for k, v in GOOD_ENV.items() if k != "ORDERS_AGENT_URL"})
    assert unset.error() is None
    assert any(
        "ORDERS_AGENT_URL (the URL of the agent behind orders_agent)" in n for n in unset.notes()
    )


def test_no_policy_no_findings() -> None:
    findings = _preflight.peer_findings(_settings(keys=[]), "prod", {"env": {}}, None)
    assert findings.error() is None and findings.notes() == []


def test_internal_host_is_the_runtimes() -> None:
    """The CLI's copy of the runtime's transport rule (design 3.6) stays in step with it."""
    template = TEMPLATE_CLIENT.read_text()
    suffixes = re.search(r"^_INTERNAL_SUFFIXES = (\(.*\))$", template, re.M)
    assert suffixes is not None
    assert ast.literal_eval(suffixes.group(1)) == _preflight.INTERNAL_SUFFIXES
    match = re.search(r"\ndef internal_host\(host: str\) -> bool:\n(.*?)\n\n\n", template, re.S)
    assert match is not None
    ours = Path(_preflight.__file__).read_text()
    mine = re.search(r"\ndef internal_host\(host: str\) -> bool:\n(.*?)\n\n\n", ours, re.S)
    assert mine is not None
    assert match.group(1).replace("_INTERNAL_SUFFIXES", "INTERNAL_SUFFIXES") == mine.group(1)


# --------------------------------------------------------------------------- through deploy


def invoke(*args: str):
    return CliRunner().invoke(cmd_deploy, list(args), catch_exceptions=False)


def _peer_project(project: SimpleNamespace, env: str, peer_env: dict[str, str]) -> None:
    (project.root / "api-policy.yaml").write_text(yaml.safe_dump(POLICY, sort_keys=False))
    path = project.chart / f"values-{env}.yaml"
    values = yaml.safe_load(path.read_text()) or {}
    values.setdefault("env", {}).update(peer_env)
    path.write_text(yaml.safe_dump(values, sort_keys=False))


@pytest.mark.parametrize("cd", ["skip", "helm-push", "argocd"])
def test_deploy_refuses_a_peer_it_could_not_call_before_anything_runs(
    project: SimpleNamespace, fake, cd: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    project.cfg.create_params["cd"] = cd
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    _peer_project(project, "prod", {**GOOD_ENV, "ORDERS_AGENT_URL": "http://orders.example.com"})
    (project.root / ".env.prod").write_text("OPENAI_API_KEY=k\nPOSTGRES_DSN=d\nAPI_KEY=a\n")
    args = ["--env", "prod", "--yes", "--image", "ghcr.io/my-org/my-agent:abc1234"]
    result = invoke(*args)
    assert result.exit_code == 3, result.output
    assert "could not call the agents in api-policy.yaml in prod" in result.output
    assert "must use https outside APP_ENV=dev" in result.output
    assert not fake.find("helm") and not fake.any("docker") and not fake.any("git ")


def test_deploy_to_dev_warns_and_goes_on(project: SimpleNamespace, fake) -> None:
    _peer_project(project, "dev", {"ORDERS_AGENT_URL": "http://orders.example.com"})
    result = invoke("--env", "dev", "--image", "x/y:1")
    assert result.exit_code == 0, result.output
    assert "Warning: ORDERS_AGENT_URL (http://orders.example.com) must use https" in result.output
    assert "Warning: TOKEN_EXCHANGE_URL is not set" in result.output
    assert fake.find("helm upgrade")
