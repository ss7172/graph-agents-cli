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

"""`graph-agents-cli auth dev-token`: local JWTs for a jwt project, and nothing else.

Signing runs in the project's environment (`uv run`); here it is replaced by a
fake signer, so no uv, key or network is needed. The real signer is exercised
on an installed project by the slow template test.
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

import click
import pytest
from click.testing import CliRunner
from dotenv import dotenv_values

from graph_agents_cli.main import main
from graph_agents_cli.setup import cmd_dev_token
from graph_agents_cli.setup.cmd_dev_token import (
    DEV_ISSUER,
    auth_group,
    build_claims,
    env_line_value,
    parse_ttl,
)

from .conftest import write_manifest

PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAtestkeytestkeytestkey\n"
    "-----END PUBLIC KEY-----\n"
)
ENV_EXAMPLE_LIKE = (
    "APP_ENV=dev\n"
    "MODEL_PROVIDER=fake\n"
    "AUTH_POLICY=jwt\n"
    "AUTH_JWT_JWKS_URL=\n"
    "# AUTH_JWT_PUBLIC_KEY=\n"
    "AUTH_JWT_ISSUER=\n"
    "AUTH_JWT_AUDIENCE=\n"
)


@pytest.fixture
def signed(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Replace the project-environment signer: record the claims, return PEM and a token."""
    calls: list[dict] = []

    def fake_sign(root: Path, key_path: Path, claims: dict) -> dict[str, str]:
        calls.append({"root": root, "key_path": key_path, "claims": claims})
        key_path.write_text("private")
        key_path.chmod(0o600)
        return {"token": "header.payload.signature", "public_key": PEM}

    monkeypatch.setattr(cmd_dev_token, "_sign", fake_sign)
    return calls


@pytest.fixture
def jwt_project(project: Path) -> Path:
    write_manifest(project, auth_policy="jwt")
    env = project / ".env"
    env.write_text(ENV_EXAMPLE_LIKE)
    env.chmod(0o644)
    return project


def dev_token(*args: str):
    return CliRunner().invoke(auth_group, ["dev-token", *args])


def test_parse_ttl() -> None:
    assert parse_ttl("3600") == 3600
    assert parse_ttl("90s") == 90
    assert parse_ttl("30m") == 1800
    assert parse_ttl("12h") == 43200
    assert parse_ttl("7d") == 7 * 86400
    for bad in ("", "0", "8d", "1w", "-5m", "h"):
        with pytest.raises(click.BadParameter):
            parse_ttl(bad)


def test_claims_go_where_the_server_reads_them() -> None:
    flat = build_claims(sub="alice", roles=["user"], issuer="i", audience="a", ttl_s=60, now=1000)
    assert flat["sub"] == "alice" and flat["roles"] == ["user"]
    assert (flat["iss"], flat["aud"], flat["iat"], flat["nbf"], flat["exp"]) == (
        "i",
        "a",
        1000,
        1000,
        1060,
    )
    nested = build_claims(
        sub="alice",
        roles=["user", "support"],
        issuer="i",
        audience="a",
        ttl_s=60,
        principal_claim="preferred_username",
        roles_claim="realm_access.roles",
    )
    assert nested["preferred_username"] == "alice" and "sub" not in nested
    assert nested["realm_access"] == {"roles": ["user", "support"]}


def test_the_public_key_is_one_env_line_the_server_reads_back(tmp_path: Path) -> None:
    line = env_line_value(PEM)
    assert "\n" not in line
    env = tmp_path / ".env"
    env.write_text(f"AUTH_JWT_PUBLIC_KEY={line}\n")
    read = dotenv_values(env)["AUTH_JWT_PUBLIC_KEY"]
    # The template's JwtPolicy turns the escaped newlines back into a PEM.
    assert read.replace("\\n", "\n") == PEM.strip()


def test_a_token_for_a_jwt_dev_project(jwt_project: Path, signed: list[dict]) -> None:
    result = dev_token("--sub", "alice", "--roles", "user, support", "--ttl", "2h")
    assert result.exit_code == 0, result.output
    # stdout is the token alone; everything else went to stderr.
    assert result.stdout == "header.payload.signature\n"
    assert 'export GRAPH_AGENTS_CLI_API_KEY="$(graph-agents-cli auth dev-token' in result.stderr
    assert "--sub alice --roles user,support" in result.stderr
    assert "Never deploy the dev key" in result.stderr
    claims = signed[0]["claims"]
    assert claims["sub"] == "alice" and claims["roles"] == ["user", "support"]
    assert claims["iss"] == DEV_ISSUER and claims["aud"] == jwt_project.name
    assert claims["exp"] - claims["iat"] == 7200
    key_dir = jwt_project / ".graph-agents-cli" / "dev-jwt"
    assert stat.S_IMODE(key_dir.stat().st_mode) == 0o700
    assert (key_dir / "public-key.pem").read_text() == PEM
    env = jwt_project / ".env"
    values = dotenv_values(env)
    assert values["AUTH_JWT_ISSUER"] == DEV_ISSUER
    assert values["AUTH_JWT_AUDIENCE"] == jwt_project.name
    assert values["AUTH_JWT_PUBLIC_KEY"].replace("\\n", "\n") == PEM.strip()
    # Blank lines were filled in place; the commented example stays a comment.
    text = env.read_text()
    assert text.count("AUTH_JWT_ISSUER=") == 1 and "# AUTH_JWT_PUBLIC_KEY=\n" in text
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert "Wrote AUTH_JWT_PUBLIC_KEY, AUTH_JWT_ISSUER, AUTH_JWT_AUDIENCE" in result.stderr

    # A second token reuses everything: .env is not written again.
    before = env.read_text()
    again = dev_token("--sub", "bob")
    assert again.exit_code == 0, again.output
    assert env.read_text() == before and "Wrote" not in again.stderr
    assert signed[1]["claims"]["roles"] == []


def test_values_env_already_sets_are_used_as_they_are(jwt_project: Path, signed) -> None:
    env = jwt_project / ".env"
    env.write_text(
        ENV_EXAMPLE_LIKE.replace(
            "AUTH_JWT_ISSUER=\n", "AUTH_JWT_ISSUER=https://idp.local/\n"
        ).replace("AUTH_JWT_AUDIENCE=\n", "AUTH_JWT_AUDIENCE=orders,orders-admin\n")
        + "AUTH_JWT_PRINCIPAL_CLAIM=email\nAUTH_JWT_ROLES_CLAIM=realm_access.roles\n"
    )
    result = dev_token("--sub", "alice@example.com", "--roles", "user")
    assert result.exit_code == 0, result.output
    claims = signed[0]["claims"]
    assert claims["iss"] == "https://idp.local/" and claims["aud"] == "orders"
    assert claims["email"] == "alice@example.com" and "sub" not in claims
    assert claims["realm_access"] == {"roles": ["user"]}
    assert "Wrote AUTH_JWT_PUBLIC_KEY to" in result.stderr


def test_it_says_to_restart_a_running_local_server(jwt_project: Path, signed, monkeypatch):
    from graph_agents_cli.run import _local_server

    monkeypatch.setattr(_local_server, "get_server_port", lambda root: 18080)
    result = dev_token("--sub", "alice")
    assert result.exit_code == 0, result.output
    assert "graph-agents-cli run --stop-server" in result.stderr


@pytest.mark.parametrize(
    ("env_text", "shell", "message"),
    [
        (ENV_EXAMPLE_LIKE.replace("AUTH_POLICY=jwt", "AUTH_POLICY=shared-bearer"), {}, "jwt"),
        (ENV_EXAMPLE_LIKE, {"AUTH_POLICY": "custom"}, "auth policy is custom"),
        (ENV_EXAMPLE_LIKE.replace("APP_ENV=dev", "APP_ENV=staging"), {}, "APP_ENV is 'staging'"),
        (ENV_EXAMPLE_LIKE.replace("APP_ENV=dev", "APP_ENV=DEV"), {}, "APP_ENV is 'DEV'"),
        (ENV_EXAMPLE_LIKE.replace("APP_ENV=dev\n", ""), {}, "APP_ENV is unset"),
        (ENV_EXAMPLE_LIKE, {"APP_ENV": "prod"}, "APP_ENV is 'prod'"),
        (
            ENV_EXAMPLE_LIKE.replace("AUTH_JWT_JWKS_URL=", "AUTH_JWT_JWKS_URL=https://idp/jwks"),
            {},
            "AUTH_JWT_JWKS_URL is set (.env)",
        ),
        (ENV_EXAMPLE_LIKE + "AUTH_JWT_ALGORITHMS=ES256\n", {}, "does not allow RS256"),
    ],
)
def test_refused_outside_a_local_jwt_dev_setup(
    jwt_project: Path, signed, monkeypatch, env_text, shell, message
) -> None:
    (jwt_project / ".env").write_text(env_text)
    for name, value in shell.items():
        monkeypatch.setenv(name, value)
    result = dev_token("--sub", "alice")
    assert result.exit_code == 3, result.output
    assert message in result.output
    assert not signed, "signed a token it had refused"
    assert "header.payload.signature" not in result.output


def test_another_public_key_is_never_replaced(jwt_project: Path, signed) -> None:
    other = env_line_value(PEM.replace("testkey", "otherkey"))
    env = jwt_project / ".env"
    env.write_text(ENV_EXAMPLE_LIKE + f"AUTH_JWT_PUBLIC_KEY={other}\n")
    result = dev_token("--sub", "alice")
    assert result.exit_code == 3, result.output
    assert "is not the dev key" in result.output and "clear it in .env" in result.output
    assert "header.payload.signature" not in result.output
    assert f"AUTH_JWT_PUBLIC_KEY={other}" in env.read_text()


def test_needs_a_project_and_an_env_file(project: Path, signed) -> None:
    result = dev_token("--sub", "alice")
    assert result.exit_code == 3 and "Not inside a graph-agents-cli project" in result.output
    write_manifest(project, auth_policy="jwt")
    result = dev_token("--sub", "alice")
    assert result.exit_code == 3 and "cp .env.example .env" in result.output
    assert not signed


def test_an_empty_principal_is_a_usage_error(jwt_project: Path, signed) -> None:
    assert dev_token("--sub", "  ").exit_code == 2
    assert dev_token("--sub", "a", "--ttl", "30d").exit_code == 2
    assert not signed


@pytest.mark.parametrize(
    ("stdout", "returncode", "message", "code"),
    [
        (json.dumps({"error": "missing", "detail": "No module named 'jwt'"}), 0, "install", 2),
        (json.dumps({"error": "failed", "detail": "ValueError: bad key"}), 0, "bad key", 2),
        ("", 1, "could not sign the token", 2),
        ("not json", 0, "could not sign the token", 2),
    ],
)
def test_signer_failures_are_tool_failures(
    jwt_project: Path, monkeypatch, stdout, returncode, message, code
) -> None:
    from graph_agents_cli import _runner

    def fake_run(args, **kwargs):
        assert args[:4] == ["uv", "run", "--quiet", "python"]
        assert kwargs["cwd"] == str(jwt_project)
        assert "VIRTUAL_ENV" not in kwargs["env"]
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="boom")

    monkeypatch.setattr(_runner, "run_resolved", fake_run)
    result = dev_token("--sub", "alice")
    assert result.exit_code == code, result.output
    assert message in result.output


def test_the_signer_payload_carries_the_claims_and_the_key_path(jwt_project: Path, monkeypatch):
    from graph_agents_cli import _runner

    seen: dict = {}

    def fake_run(args, **kwargs):
        seen.update(json.loads(kwargs["input"]))
        answer = {"token": "t.o.k", "public_key": PEM}
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(answer) + "\n", stderr="")

    monkeypatch.setattr(_runner, "run_resolved", fake_run)
    result = dev_token("--sub", "alice")
    assert result.exit_code == 0, result.output
    assert result.stdout == "t.o.k\n"
    assert seen["algorithm"] == "RS256" and seen["kid"] == "graph-agents-cli-dev"
    assert seen["key_path"].endswith(".graph-agents-cli/dev-jwt/private-key.pem")
    assert seen["claims"]["sub"] == "alice"


def test_the_auth_group_is_registered_and_documented() -> None:
    result = CliRunner().invoke(
        main, ["auth", "--help"], env={"GRAPH_AGENTS_CLI_NO_UPDATE_CHECK": "1"}
    )
    assert result.exit_code == 0, result.output
    assert "dev-token" in result.output and "Source:" in result.output
    root = CliRunner().invoke(
        main, ["--help"], env={"GRAPH_AGENTS_CLI_NO_UPDATE_CHECK": "1"}, terminal_width=200
    )
    assert "Local credentials for the project's auth policy (dev-only JWTs)." in root.output
