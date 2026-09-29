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
"""Settings checks shared by ``deploy`` and ``infra check``: what the pods would see.

A pod's environment is the app Secret (``envFrom``), then the chart's ConfigMap
(``env`` in the merged values, also ``envFrom`` and listed after the Secret, so
it wins, even with an empty value), then the variables the chart sets itself.
These checks read that environment the same way, from the merged chart values
and the names of the keys the Secret holds (values are never needed):

- ``env_placeholders``: a scaffold placeholder (``CHANGE-ME``) still in the
  chart env, for example an API base URL: the pods would call it.
- ``jwt_findings``: the ``jwt`` policy without a verification key, issuer or
  audience. Outside ``APP_ENV=dev`` the pod refuses to start without them (after
  the image was built and the rollout waited); in dev it starts, reports Ready
  and answers every request with 503.
- ``dsn_without_tls``: a database connection string for an external database
  that does not require TLS.
- ``peer_findings``: the other agents the project calls (``protocol: a2a`` APIs):
  a peer reached with a credential over plain http to a host that is not
  cluster-internal, an ``auth: exchange`` API without the issuer's token URL
  or this agent's client id (or a plain-http token URL), and what is missing
  from ``secrets.keys``.
"""

from __future__ import annotations

import ipaddress
import re
import urllib.parse
from collections.abc import Mapping, Set
from dataclasses import dataclass, field
from typing import Any

from graph_agents_cli._defaults import normalize_auth_policy
from graph_agents_cli.deploy import _image, _modes
from graph_agents_cli.deploy._config import DeploySettings

JWT_KEY_SOURCES = ("AUTH_JWT_JWKS_URL", "AUTH_JWT_PUBLIC_KEY")
JWT_REQUIRED_OUTSIDE_DEV = ("AUTH_JWT_ISSUER", "AUTH_JWT_AUDIENCE")
_DEFAULT_JWT_ALGORITHMS = "RS256,ES256"
_HMAC = frozenset({"HS256", "HS384", "HS512"})
# libpq modes that refuse a connection without TLS.
TLS_SSLMODES = frozenset({"require", "verify-ca", "verify-full"})
# URL (`?sslmode=x`) or keyword/value form, where libpq allows spaces around `=`
# and a quoted value (`sslmode = 'verify-full'`).
_SSLMODE = re.compile(r"(?:^|[?&\s])sslmode\s*=\s*'?([A-Za-z-]+)")


def chart_env(values: Mapping[str, Any]) -> dict[str, Any]:
    env = values.get("env")
    return dict(env) if isinstance(env, Mapping) else {}


def env_placeholders(values: Mapping[str, Any]) -> list[str]:
    """Chart ``env`` keys whose value still holds the scaffold placeholder (``CHANGE-ME``)."""
    return sorted(
        k for k, v in chart_env(values).items() if isinstance(v, str) and _image.has_placeholder(v)
    )


def effective_auth_policy(settings: DeploySettings, values: Mapping[str, Any]) -> str:
    """``env.AUTH_POLICY`` from the chart values, else the manifest's policy."""
    raw = str(chart_env(values).get("AUTH_POLICY") or "").strip()
    return normalize_auth_policy(raw or settings.auth_policy, warn=False)


def app_env(values: Mapping[str, Any]) -> str:
    """``APP_ENV`` as the pod reads it (the app's dev test is exactly ``dev``)."""
    value = chart_env(values).get("APP_ENV")
    return "" if value is None else str(value)


@dataclass
class JwtFindings:
    """What keeps the ``jwt`` policy from verifying tokens; ``strict`` outside dev."""

    env_name: str = ""
    strict: bool = False
    problems: list[str] = field(default_factory=list)
    # What cannot be told before the Secret is read; mentioned only next to problems.
    unverified: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.problems)

    @property
    def where(self) -> str:
        return f"values-{self.env_name}.yaml (or values.yaml) under env:"

    def error(self) -> str | None:
        """The refusal (exit 3) outside dev: the pods would refuse to start."""
        if not (self.problems and self.strict):
            return None
        also = f"\n  Also check: {'; '.join(self.unverified)}." if self.unverified else ""
        return (
            f"The jwt auth policy cannot verify tokens in {self.env_name}: "
            f"{'; '.join(self.problems)}.\n  Set them in {self.where} (a setting the chart env "
            "lists overrides the Secret, even when empty). Outside APP_ENV=dev the pods refuse "
            f"to start without them.{also}"
        )

    def warning(self) -> str | None:
        """The dev warning: the pods start, report Ready and answer every request with 503."""
        if not self.problems or self.strict:
            return None
        return (
            f"jwt: {'; '.join(self.problems)}. The {self.env_name} pods start and report Ready "
            f"but answer every request with 503 until it is set in {self.where}"
        )


def _setting(env: Mapping[str, Any], key: str, secret_keys: Set[str] | None) -> str | None:
    """What the pod sees for ``key``: the chart env's value when it lists the key (the
    ConfigMap wins, even when empty), else ``"<secret>"`` when the Secret holds it, else
    ``""``. ``None`` when that cannot be known (the Secret was not read)."""
    if key in env:
        value = env.get(key)
        return "" if value is None else str(value).strip()
    if secret_keys is None:
        return None
    return "<secret>" if key in secret_keys else ""


def jwt_findings(
    settings: DeploySettings,
    env_name: str,
    values: Mapping[str, Any],
    secret_keys: Set[str] | None,
) -> JwtFindings:
    """What keeps the ``jwt`` policy from verifying tokens in ``env_name`` (empty otherwise).

    ``secret_keys`` are the keys the app Secret holds (or will hold after this
    deploy); ``None`` when unknown (argocd mode never reads the cluster), and a
    setting the chart values do not list is then assumed to come from the
    Secret. Strict outside dev (``env_name`` other than ``dev``, or ``APP_ENV``
    not exactly ``dev``), where the pod would refuse to start.
    """
    findings = JwtFindings(env_name=env_name)
    if effective_auth_policy(settings, values) != "jwt":
        return findings
    env = chart_env(values)
    findings.strict = not _modes.is_dev_env(env_name) or app_env(values) != "dev"
    algorithms = str(env.get("AUTH_JWT_ALGORITHMS") or _DEFAULT_JWT_ALGORITHMS)
    asymmetric = [
        a.strip().upper()
        for a in algorithms.split(",")
        if a.strip() and a.strip().upper() not in _HMAC and a.strip().lower() != "none"
    ]
    if asymmetric:
        sources = [_setting(env, key, secret_keys) for key in JWT_KEY_SOURCES]
        if None in sources and not any(sources):
            unknown = [k for k, v in zip(JWT_KEY_SOURCES, sources, strict=True) if v is None]
            findings.unverified.append(
                "no verification key in the chart values, so "
                + " or ".join(unknown)
                + " must come from the Secret"
            )
        if all(value == "" for value in sources):
            findings.problems.append(
                "no verification key (set AUTH_JWT_JWKS_URL to the issuer's JWKS, or "
                "AUTH_JWT_PUBLIC_KEY)"
            )
        elif all(value for value in sources):
            findings.problems.append(
                "both AUTH_JWT_JWKS_URL and AUTH_JWT_PUBLIC_KEY are set (set one)"
            )
    if findings.strict:
        for key in JWT_REQUIRED_OUTSIDE_DEV:
            if _setting(env, key, secret_keys) == "":
                findings.problems.append(f"{key} is empty")
    return findings


def sslmode(dsn: str) -> str | None:
    """The ``sslmode`` a libpq connection string (URL or ``key=value`` form) asks for."""
    match = _SSLMODE.search(dsn or "")
    return match.group(1).lower() if match else None


def dsn_without_tls(values: Mapping[str, Any], dsn: str) -> bool:
    """The DSN does not require TLS (and the chart env sets no ``PGSSLMODE`` that does)."""
    mode = sslmode(dsn)
    if mode is None:
        mode = str(chart_env(values).get("PGSSLMODE") or "").strip().lower() or None
    return mode not in TLS_SSLMODES


def _enabled(values: Mapping[str, Any], section: str) -> bool:
    block = values.get(section)
    value = block.get("enabled") if isinstance(block, Mapping) else None
    return value is True or (
        isinstance(value, str) and value.strip().lower() in ("true", "yes", "1")
    )


def dsn_keys(settings: DeploySettings, values: Mapping[str, Any]) -> list[str]:
    """Secret keys that hold an external database's connection string.

    None when the bundled dev subchart provides the database (the chart builds
    that DSN itself) or the fastapi runtime keeps its checkpoints in memory.
    """
    if _enabled(values, "postgresql"):
        return []
    if settings.runtime == "langgraph-server":
        return ["DATABASE_URI"]
    if str(chart_env(values).get("CHECKPOINTER") or "postgres").strip().lower() == "memory":
        return []
    return ["POSTGRES_DSN"]


def dsn_tls_warning(key: str) -> str:
    return (
        f"{key} does not require TLS (no sslmode=require, verify-ca or verify-full): the "
        "database password and every checkpoint cross the network unencrypted. Append "
        "?sslmode=verify-full&sslrootcert=<CA file mounted into the pod> (or "
        "sslrootcert=system for a publicly trusted certificate), or set PGSSLMODE in the "
        "chart env."
    )


# ---------------------------------------------------------------------------
# Peers (protocol: a2a APIs): the rules the runtime applies when it calls them
# ---------------------------------------------------------------------------

# A credential-carrying call to another agent may use plain http outside APP_ENV=dev only to
# these hosts: the runtime's `internal_host` (app_utils/api_client.py; a test keeps the two
# in step).
INTERNAL_SUFFIXES = (".svc", ".svc.cluster.local")
CREDENTIAL_AUTH_MODES = ("bearer", "forward", "exchange")
TOKEN_EXCHANGE_URL = "TOKEN_EXCHANGE_URL"
TOKEN_EXCHANGE_CLIENT_ID = "TOKEN_EXCHANGE_CLIENT_ID"
TOKEN_EXCHANGE_CLIENT_SECRET = "TOKEN_EXCHANGE_CLIENT_SECRET"
PRINCIPAL_HASH_SALT = "PRINCIPAL_HASH_SALT"
_TRUE = ("1", "true", "yes", "on")


def internal_host(host: str) -> bool:
    """Whether `host` is loopback, a single-label name or a cluster-internal (`.svc`) name."""
    host = host.strip("[]").lower().rstrip(".")
    if host == "localhost" or ("." not in host and ":" not in host):
        return True
    if host.endswith(INTERNAL_SUFFIXES):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _loopback(host: str) -> bool:
    host = host.strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass
class PeerFindings:
    """What keeps the agent from calling its peers in an environment (``strict`` outside dev:
    the runtime refuses those calls, or refuses to start)."""

    env_name: str = ""
    strict: bool = False
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def error(self) -> str | None:
        """The refusal (exit 3) outside dev."""
        if not (self.problems and self.strict):
            return None
        lines = "\n".join(f"  - {problem}" for problem in self.problems)
        return (
            f"The agent could not call the agents in api-policy.yaml in {self.env_name}:\n{lines}"
            f"\n  Set them in values-{self.env_name}.yaml (or values.yaml) under env:."
        )

    def notes(self) -> list[str]:
        """The warnings, and in dev the problems too (they warn there)."""
        return [*([] if self.strict else self.problems), *self.warnings]


def peer_findings(
    settings: DeploySettings,
    env_name: str,
    values: Mapping[str, Any],
    document: Mapping[str, Any] | None,
) -> PeerFindings:
    """The peer rules (``deploy``'s pre-checks; design 7.4) for ``env_name``.

    Refused outside dev: a credential-carrying peer URL over http to a host that
    is not internal (loopback, a single label, ``.svc``); an ``auth: exchange`` API
    without ``TOKEN_EXCHANGE_URL`` or ``TOKEN_EXCHANGE_CLIENT_ID``, or with an http
    token URL to a host that is not loopback (unless ``TOKEN_EXCHANGE_ALLOW_HTTP``).
    A peer URL still at ``CHANGE-ME`` is refused by ``env_placeholders`` already.
    Warned: an unset peer URL, and what ``secrets.keys`` lacks (the exchange
    client secret, ``PRINCIPAL_HASH_SALT``).
    """
    findings = PeerFindings(env_name=env_name)
    findings.strict = not _modes.is_dev_env(env_name) or app_env(values) != "dev"
    apis = (document or {}).get("apis") or {}
    peers = {
        str(n): a for n, a in apis.items() if isinstance(a, Mapping) and a.get("protocol") == "a2a"
    }
    env = chart_env(values)
    for name, api in peers.items():
        variable = str(api.get("base_url_env") or "")
        url = str(env.get(variable) or "").strip()
        if not url:
            findings.warnings.append(
                f"{variable} (the URL of the agent behind {name}) is not in the chart env: set it "
                f"in values-{env_name}.yaml, or the calls to {name} fail"
            )
            continue
        if _image.has_placeholder(url) or api.get("auth") not in CREDENTIAL_AUTH_MODES:
            continue
        parts = urllib.parse.urlsplit(url)
        if parts.scheme == "http" and not internal_host(parts.hostname or ""):
            findings.problems.append(
                f"{variable} ({url}) must use https outside APP_ENV=dev to carry credentials "
                f"(auth: {api.get('auth')}); plain http is for loopback and cluster-internal "
                "names only"
            )
    keys = set(settings.secret_keys)
    if any(api.get("auth") == "exchange" for api in apis.values() if isinstance(api, Mapping)):
        for variable in (TOKEN_EXCHANGE_URL, TOKEN_EXCHANGE_CLIENT_ID):
            if not str(env.get(variable) or "").strip():
                findings.problems.append(
                    f"{variable} is not set, and auth: exchange APIs need it (the issuer's token "
                    "endpoint and this agent's client there)"
                )
        token = str(env.get(TOKEN_EXCHANGE_URL) or "").strip()
        allow_http = str(env.get("TOKEN_EXCHANGE_ALLOW_HTTP") or "").strip().lower() in _TRUE
        parts = urllib.parse.urlsplit(token)
        if (
            token
            and not _image.has_placeholder(token)
            and parts.scheme == "http"
            and not (_loopback(parts.hostname or "") or allow_http)
        ):
            findings.problems.append(
                f"{TOKEN_EXCHANGE_URL} ({token}) must use https outside APP_ENV=dev (it carries "
                'the users\' tokens and the client secret); set TOKEN_EXCHANGE_ALLOW_HTTP: "true" '
                "only for a trusted in-cluster issuer"
            )
        if TOKEN_EXCHANGE_CLIENT_SECRET not in keys:
            findings.warnings.append(
                f"{TOKEN_EXCHANGE_CLIENT_SECRET} is not in secrets.keys: the pods never get it, so "
                "every token exchange fails"
            )
    if peers and PRINCIPAL_HASH_SALT not in keys:
        findings.warnings.append(
            f"{PRINCIPAL_HASH_SALT} is not in secrets.keys: the conversation ids sent to other "
            "agents are keyed without a secret"
        )
    return findings
