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

"""graph-agents-cli auth: local credentials for a project's auth policy.

``auth dev-token`` lets a ``jwt`` project run locally without an identity
provider. It keeps an RSA key pair under ``.graph-agents-cli/dev-jwt/`` (git
ignored; the private key is mode 0600), puts the public key and a dev issuer
and audience in ``.env`` (only where ``.env`` leaves them blank), and prints a
token signed with the private key for the principal and roles asked for.
``run`` and ``eval`` send it as the bearer when it is in
``GRAPH_AGENTS_CLI_API_KEY``, which keeps it out of argv and shell history.

It refuses anything but a local dev setup: the project's policy must be
``jwt``, the local server's ``APP_ENV`` exactly ``dev``, and ``.env`` must not
point at a real issuer (``AUTH_JWT_JWKS_URL``) or hold another public key.
Deployed environments never see the dev key: ``deploy`` and ``secrets`` read
the chart values and ``.env.<env>``, and ``AUTH_JWT_PUBLIC_KEY`` is not a
secret key. Signing runs in the project's environment (``uv run``), whose
``pyjwt[crypto]`` is the library the server verifies with, so the CLI itself
needs no crypto dependency.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import click

from graph_agents_cli._click import LazyGroup
from graph_agents_cli._defaults import normalize_auth_policy
from graph_agents_cli._project import find_project_root, read_project_config
from graph_agents_cli._remote import API_KEY_ENV

EXIT_TOOL_FAILURE = 2
EXIT_CONFIG_ERROR = 3

DEV_KEY_DIR = Path(".graph-agents-cli") / "dev-jwt"
PRIVATE_KEY_FILE = "private-key.pem"
PUBLIC_KEY_FILE = "public-key.pem"
DEV_ISSUER = "graph-agents-cli-dev"
DEV_KEY_ID = "graph-agents-cli-dev"
ALGORITHM = "RS256"
DEFAULT_TTL = "12h"
MAX_TTL_S = 7 * 24 * 3600
SIGN_TIMEOUT_S = 300

_TTL = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$")
_TTL_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


class DevTokenError(click.ClickException):
    """A setup this command will not sign for (exit 3)."""

    exit_code = EXIT_CONFIG_ERROR


class SigningError(click.ClickException):
    """The project's environment could not create the key or sign (exit 2)."""

    exit_code = EXIT_TOOL_FAILURE


# The part that runs in the project's environment (`uv run python -c`): it
# reads {"key_path", "claims", "kid", "algorithm"} on stdin, creates the private
# key when absent (0600, never overwritten), and prints {"token", "public_key"}
# or {"error", "detail"}.
_SIGNER = r"""
import json, os, sys
payload = json.load(sys.stdin)
try:
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
except ImportError as exc:
    print(json.dumps({"error": "missing", "detail": str(exc)}))
    sys.exit(0)
try:
    path = payload["key_path"]
    if os.path.exists(path):
        with open(path, "rb") as handle:
            key = serialization.load_pem_private_key(handle.read(), password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise ValueError("not an RSA private key")
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        # Written whole under a temporary name, then linked into place: the key
        # appears complete or not at all, and never replaces one that exists.
        tmp = f"{path}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(pem)
            os.link(tmp, path)
        except FileExistsError:
            # Another run created the key first: sign with that one.
            with open(path, "rb") as handle:
                key = serialization.load_pem_private_key(handle.read(), password=None)
        finally:
            os.unlink(tmp)
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    token = jwt.encode(
        payload["claims"], key, algorithm=payload["algorithm"], headers={"kid": payload["kid"]}
    )
    print(json.dumps({"token": token, "public_key": public}))
except Exception as exc:
    print(json.dumps({"error": "failed", "detail": f"{type(exc).__name__}: {exc}"}))
"""


def parse_ttl(raw: str) -> int:
    """``3600``, ``90s``, ``30m``, ``12h`` or ``2d`` -> seconds (1 s to 7 days)."""
    match = _TTL.match(raw or "")
    if not match:
        raise click.BadParameter(f"{raw!r}: use seconds or a number with s, m, h or d (e.g. 12h)")
    seconds = int(match.group(1)) * _TTL_UNITS[match.group(2)]
    if not 1 <= seconds <= MAX_TTL_S:
        raise click.BadParameter(f"{raw!r}: between 1 second and 7 days")
    return seconds


def _csv(raw: str | None) -> list[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def _set_claim(claims: dict[str, Any], path: str, value: Any) -> None:
    """Put ``value`` at a claim name, or a dotted path (``realm_access.roles``) as the server reads it."""
    if "." not in path:
        claims[path] = value
        return
    *parents, leaf = path.split(".")
    node = claims
    for part in parents:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[leaf] = value


def build_claims(
    *,
    sub: str,
    roles: list[str],
    issuer: str,
    audience: str,
    ttl_s: int,
    principal_claim: str = "sub",
    roles_claim: str = "roles",
    now: int | None = None,
) -> dict[str, Any]:
    """The token's claims: principal and roles where the server reads them, plus iss/aud/exp."""
    issued = int(time.time()) if now is None else now
    claims: dict[str, Any] = {
        "iss": issuer,
        "aud": audience,
        "iat": issued,
        "nbf": issued,
        "exp": issued + ttl_s,
        "jti": uuid.uuid4().hex,
    }
    _set_claim(claims, principal_claim or "sub", sub)
    _set_claim(claims, roles_claim or "roles", roles)
    return claims


def _normalised_pem(value: str) -> str:
    """A PEM as the server reads it (``\\n`` escapes accepted), without whitespace."""
    return "".join(value.replace("\\n", "\n").split())


def env_line_value(pem: str) -> str:
    """``AUTH_JWT_PUBLIC_KEY`` as one ``.env`` line: single-quoted, newlines as ``\\n``."""
    return "'" + pem.strip().replace("\n", "\\n") + "'"


def _refuse(message: str) -> None:
    raise DevTokenError(message)


def _check_setup(env: Any, *, manifest_policy: str) -> None:
    """Refuse anything but a local jwt dev setup (``env`` is the login EnvView)."""
    policy = normalize_auth_policy(env.get("AUTH_POLICY").strip() or manifest_policy, warn=False)
    if policy != "jwt":
        local = {
            "shared-bearer": "Local runs of a shared-bearer project send the API_KEY in .env.",
            "custom": (
                "A custom policy reads what its CustomPolicy checks: pass it with "
                "--header 'Name: value' or --cookie name=value."
            ),
        }.get(policy, "")
        _refuse(
            f"This project's auth policy is {policy} (AUTH_POLICY): dev tokens are for jwt "
            f"projects. {local}".rstrip()
        )
    app_env = env.get("APP_ENV")
    if app_env != "dev":
        shown = repr(app_env) if env.has("APP_ENV") else "unset"
        _refuse(
            f"APP_ENV is {shown}; dev tokens are only for a local server under APP_ENV=dev "
            "(set it in .env, as .env.example does). Deployed environments take tokens from "
            "your identity provider."
        )
    if env.has("AUTH_JWT_JWKS_URL"):
        _refuse(
            f"AUTH_JWT_JWKS_URL is set ({env.where('AUTH_JWT_JWKS_URL')}): the local server "
            "verifies tokens from that issuer, not from a dev key. Get a token from the issuer, "
            "or clear AUTH_JWT_JWKS_URL to use a dev key."
        )
    algorithms = [a.upper() for a in _csv(env.get("AUTH_JWT_ALGORITHMS"))]
    if algorithms and ALGORITHM not in algorithms:
        _refuse(
            f"AUTH_JWT_ALGORITHMS ({', '.join(algorithms)}) does not allow {ALGORITHM}, "
            "which the dev key signs with."
        )


def _sign(root: Path, key_path: Path, claims: dict[str, Any]) -> dict[str, str]:
    """Create the key when absent and sign ``claims`` in the project's environment."""
    from graph_agents_cli._runner import run_resolved
    from graph_agents_cli._tools import ToolNotFoundError

    payload = {"key_path": str(key_path), "claims": claims, "kid": DEV_KEY_ID}
    payload["algorithm"] = ALGORITHM
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)  # the project's own .venv, not the CLI's
    try:
        result = run_resolved(
            ["uv", "run", "--quiet", "python", "-c", _SIGNER],
            cwd=str(root),
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SIGN_TIMEOUT_S,
            env=env,
        )
    except ToolNotFoundError as exc:
        raise SigningError(f"{exc}") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise SigningError(f"Could not run the project's Python (uv run): {exc}") from exc
    lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
    try:
        answer = json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError:
        answer = {}
    if result.returncode != 0 or not isinstance(answer, dict) or not answer:
        detail = (result.stderr or result.stdout or "").strip()[-800:]
        raise SigningError(
            "The project's environment could not sign the token (uv run exited "
            f"{result.returncode}).\n  {detail}\n  Run `graph-agents-cli install` first."
        )
    if answer.get("error") == "missing":
        raise SigningError(
            f"The project's environment has no PyJWT with crypto support ({answer.get('detail')}). "
            "Run `graph-agents-cli install` (the template depends on pyjwt[crypto])."
        )
    if answer.get("error"):
        raise SigningError(
            f"Could not sign with {key_path}: {answer.get('detail')}. If that file is damaged "
            "(not an RSA private key), delete it to create a new dev key, then clear "
            "AUTH_JWT_PUBLIC_KEY in .env and rerun; tokens signed with the old key stop working."
        )
    return {"token": str(answer["token"]), "public_key": str(answer["public_key"])}


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)


@click.group("auth", cls=LazyGroup)
def auth_group() -> None:
    """Local credentials for the project's auth policy (dev-only JWTs).

    Nothing here touches a cluster or an identity provider.
    """


@auth_group.command("dev-token")
@click.option(
    "--sub",
    required=True,
    help="The principal id the token carries (in AUTH_JWT_PRINCIPAL_CLAIM, default sub).",
)
@click.option(
    "--roles",
    default="",
    help="Comma list of roles (in AUTH_JWT_ROLES_CLAIM, default roles), e.g. user,support.",
)
@click.option(
    "--ttl",
    default=DEFAULT_TTL,
    show_default=True,
    help="Lifetime: seconds, or a number with s, m, h or d (at most 7d).",
)
def cmd_dev_token(sub: str, roles: str, ttl: str) -> None:
    """Mint a JWT for local runs of a jwt project (APP_ENV=dev only).

    Prints the token alone on stdout, so it can go straight into the variable
    `run` and `eval` send as the bearer, without appearing in argv or your
    shell history:

    \b
      export GRAPH_AGENTS_CLI_API_KEY="$(graph-agents-cli auth dev-token --sub alice --roles user)"
      graph-agents-cli run "hello"
      graph-agents-cli eval run

    \b
    The first call creates a dev RSA key pair in .graph-agents-cli/dev-jwt/
    (git ignored) and fills blank AUTH_JWT_PUBLIC_KEY, AUTH_JWT_ISSUER and
    AUTH_JWT_AUDIENCE in .env (kept 0600); values .env already sets are used
    as they are. Restart a running local server afterwards
    (`graph-agents-cli run --stop-server`). Refused unless the project's
    policy is jwt, APP_ENV is exactly dev, and .env names no JWKS URL and no
    other public key. Never deploy the dev key.

    \b
    Exit codes:
      0  token printed
      2  the project's environment could not sign (run `graph-agents-cli install`)
      3  not a jwt project under APP_ENV=dev, or another key or issuer is configured
    """
    from graph_agents_cli.setup.cmd_auth import load_env, write_env

    ttl_s = parse_ttl(ttl)
    sub = sub.strip()
    if not sub:
        raise click.BadParameter("--sub must not be empty")
    role_list = _csv(roles)

    root = find_project_root(Path.cwd())
    if root is None:
        raise DevTokenError(
            "Not inside a graph-agents-cli project (no graph-agents-cli-manifest.yaml here or "
            "above)."
        )
    config = read_project_config(str(root))
    env_file = root / ".env"
    if not env_file.is_file():
        raise DevTokenError(
            f"No .env in {root}: the local server reads its settings from it. Create it with "
            "`cp .env.example .env`, then rerun."
        )
    env = load_env(env_file)
    _check_setup(env, manifest_policy=config.auth_policy)

    key_dir = root / DEV_KEY_DIR
    _private_dir(key_dir)
    key_path = key_dir / PRIVATE_KEY_FILE
    issuer = env.get("AUTH_JWT_ISSUER").strip() or DEV_ISSUER
    audiences = _csv(env.get("AUTH_JWT_AUDIENCE"))
    audience = audiences[0] if audiences else (config.project_name or root.name)
    claims = build_claims(
        sub=sub,
        roles=role_list,
        issuer=issuer,
        audience=audience,
        ttl_s=ttl_s,
        principal_claim=env.get("AUTH_JWT_PRINCIPAL_CLAIM").strip() or "sub",
        roles_claim=env.get("AUTH_JWT_ROLES_CLAIM").strip() or "roles",
    )
    signed = _sign(root, key_path, claims)
    public_pem = signed["public_key"]
    (key_dir / PUBLIC_KEY_FILE).write_text(public_pem, encoding="utf-8")

    configured = env.get("AUTH_JWT_PUBLIC_KEY").strip()
    if configured and _normalised_pem(configured) != _normalised_pem(public_pem):
        where = env.where("AUTH_JWT_PUBLIC_KEY")
        how = "unset it in your shell" if where == "environment" else f"clear it in {where}"
        raise DevTokenError(
            f"AUTH_JWT_PUBLIC_KEY ({where}) is not the dev key in {DEV_KEY_DIR}/, so the local "
            f"server would refuse a dev token. To use the dev key, {how} and rerun."
        )

    wanted = {
        "AUTH_JWT_PUBLIC_KEY": env_line_value(public_pem),
        "AUTH_JWT_ISSUER": issuer,
        "AUTH_JWT_AUDIENCE": audience,
    }
    written = [name for name in wanted if not env.has(name)]
    write_env(env_file, {name: wanted[name] for name in written})
    if not written and os.name != "nt" and env_file.exists():
        env_file.chmod(0o600)  # write_env keeps it 0600 when it writes

    err = click.get_text_stream("stderr")
    roles_text = ", ".join(role_list) if role_list else "none"
    click.echo(
        f"Dev token for {sub} (roles: {roles_text}), valid {ttl.strip()}, accepted only by a "
        "server with this project's dev key.",
        file=err,
    )
    if written:
        click.echo(
            f"Wrote {', '.join(written)} to {env_file} (the private key stays in "
            f"{DEV_KEY_DIR / PRIVATE_KEY_FILE}).",
            file=err,
        )
        from graph_agents_cli.run._local_server import get_server_port

        if get_server_port(root) is not None:
            click.echo(
                "A local server is running with the old settings: restart it with "
                "`graph-agents-cli run --stop-server`.",
                file=err,
            )
    role_flag = f" --roles {shlex.quote(','.join(role_list))}" if role_list else ""
    command = f"graph-agents-cli auth dev-token --sub {shlex.quote(sub)}{role_flag}"
    click.echo(
        "Keep the token out of argv and shell history: put it in the variable run and eval send,\n"
        f'  export {API_KEY_ENV}="$({command})"\n'
        "Never deploy the dev key: deployed environments verify tokens from your identity "
        "provider (AUTH_JWT_JWKS_URL in the chart values).",
        file=err,
    )
    click.echo(signed["token"])
