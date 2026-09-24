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
"""Which allow-listed Secret keys an environment cannot run without.

A missing key only shows up once the pods start: a crash loop (no provider key,
no database URI) or every request answered 503 (no ``API_KEY`` under
``shared-bearer``). Outside dev the chart requires the Secret itself
(``secretOptional: false``), so a missing Secret keeps the pods from starting,
but it cannot tell which keys the Secret holds.
``deploy`` checks the live Secret against this list before ``helm upgrade``,
and ``secrets status`` exits 1 only when one of these is missing, so both work
as gates while optional keys (judge key, LangSmith key, API tokens) stay
optional.

The list follows the environment's merged chart values, the same inputs the pod
gets: ``env.MODEL_PROVIDER`` (the manifest's provider when unset) needs its
key, except ``openai-compatible`` servers (often keyless on-network) and the
``fake`` test model; ``env.AUTH_POLICY`` ``shared-bearer`` needs ``API_KEY``;
the database and Redis URIs are needed unless the bundled subchart provides
them (``postgresql.enabled`` / ``redis.enabled``) or ``env.CHECKPOINTER`` is
``memory``. A key set as a plain chart ``env`` value does not need the Secret.
Only keys in the manifest's ``secrets.keys`` are ever required; removing one
from the allow-list is how a deployment opts out.

One key joins the allow-list without being listed there: ``AUTH_JWT_SECRET``,
the shared secret of the ``jwt`` policy's HS* algorithms. A project opts into
HS* with ``AUTH_JWT_ALLOW_HS=true`` / ``AUTH_JWT_ALGORITHMS=HS256`` in the
environment's chart values or its env file; the secret itself must then reach
the pods through the Secret (never the ConfigMap). It is required when the
chart values list an HS* algorithm: the pod refuses to start without it.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from graph_agents_cli._defaults import PROVIDER_KEY_VARS, normalize_auth_policy
from graph_agents_cli._output import Console
from graph_agents_cli.deploy._config import DeploySettings

# Providers whose key is optional: on-network OpenAI-compatible servers are often
# keyless, and the fake model needs none.
KEYLESS_PROVIDERS = frozenset({"openai-compatible", "fake"})

JWT_SECRET_KEY = "AUTH_JWT_SECRET"
# The HS* settings the app reads (see the template's app/app_utils/auth.py).
JWT_HS_SETTINGS = ("AUTH_JWT_ALLOW_HS", "AUTH_JWT_ALGORITHMS")
_HMAC_ALGORITHMS = frozenset({"HS256", "HS384", "HS512"})
_TRUE = ("1", "true", "yes", "on")


def _env_value(values: dict[str, Any], key: str) -> str:
    env = values.get("env") if isinstance(values.get("env"), dict) else {}
    value = env.get(key)
    return "" if value is None else str(value).strip()


def _auth_policy(settings: DeploySettings, values: dict[str, Any]) -> str:
    return normalize_auth_policy(
        _env_value(values, "AUTH_POLICY") or settings.auth_policy, warn=False
    )


def _lists_hmac(algorithms: str) -> bool:
    return any(a.strip().upper() in _HMAC_ALGORITHMS for a in algorithms.split(","))


def _hs_settings(source: Mapping[str, Any]) -> tuple[bool, bool]:
    """``(allow_hs, lists_hs)`` read from one source of settings."""
    allow = str(source.get("AUTH_JWT_ALLOW_HS") or "").strip().lower() in _TRUE
    return allow, _lists_hmac(str(source.get("AUTH_JWT_ALGORITHMS") or ""))


def jwt_hs_in_values(settings: DeploySettings, values: dict[str, Any]) -> bool:
    """The chart values run the ``jwt`` policy with an HS* algorithm (the pod needs the secret)."""
    env = values.get("env") if isinstance(values.get("env"), dict) else {}
    return _auth_policy(settings, values) == "jwt" and _hs_settings(env)[1]


def jwt_hs_indicated(
    settings: DeploySettings, values: dict[str, Any], file_values: Mapping[str, str] | None = None
) -> bool:
    """HS* JWTs are opted into by the chart values or the env file (``jwt`` policy only)."""
    if _auth_policy(settings, values) != "jwt":
        return False
    env = values.get("env") if isinstance(values.get("env"), dict) else {}
    return any(any(_hs_settings(source)) for source in (env, file_values or {}))


def for_environment(
    settings: DeploySettings, values: dict[str, Any], file_values: Mapping[str, str] | None = None
) -> DeploySettings:
    """``settings`` whose allow-list also holds ``AUTH_JWT_SECRET`` when HS* JWTs are in use."""
    if JWT_SECRET_KEY in settings.secret_keys or not jwt_hs_indicated(
        settings, values, file_values
    ):
        return settings
    return dataclasses.replace(settings, secret_keys=[*settings.secret_keys, JWT_SECRET_KEY])


def _enabled(values: dict[str, Any], section: str) -> bool:
    block = values.get(section) if isinstance(values.get(section), dict) else {}
    value = block.get("enabled")
    return value is True or (
        isinstance(value, str) and value.strip().lower() in ("true", "yes", "1")
    )


def required_keys(settings: DeploySettings, values: dict[str, Any]) -> list[str]:
    """Allow-listed keys the pods need from the Secret, in allow-list order."""
    needed: set[str] = set()
    provider = (_env_value(values, "MODEL_PROVIDER") or settings.model_provider).lower()
    if provider not in KEYLESS_PROVIDERS:
        needed.add(PROVIDER_KEY_VARS.get(provider, "MODEL_API_KEY"))
    if _auth_policy(settings, values) == "shared-bearer":
        needed.add("API_KEY")
    if jwt_hs_in_values(settings, values):
        needed.add(JWT_SECRET_KEY)
    if settings.runtime == "langgraph-server":
        if not _enabled(values, "postgresql"):
            needed.add("DATABASE_URI")
        if not _enabled(values, "redis"):
            needed.add("REDIS_URI")
    elif (
        not _enabled(values, "postgresql")
        and (_env_value(values, "CHECKPOINTER") or "postgres").lower() != "memory"
    ):
        needed.add("POSTGRES_DSN")
    allowed = for_environment(settings, values).secret_keys
    return [k for k in allowed if k in needed and not _env_value(values, k)]


def optional_keys(settings: DeploySettings, values: dict[str, Any]) -> list[str]:
    """The allow-listed keys that are not required, in allow-list order."""
    required = set(required_keys(settings, values))
    return [k for k in for_environment(settings, values).secret_keys if k not in required]


def unreached_hs_settings(
    settings: DeploySettings, values: dict[str, Any], file_values: Mapping[str, str]
) -> list[str]:
    """HS* settings the env file sets that never reach the pods.

    Only allow-listed keys go from the env file into the Secret, and these are
    plain settings that belong in the chart values' ``env`` (the ConfigMap).
    """
    if _auth_policy(settings, values) != "jwt":
        return []
    return [
        key
        for key in JWT_HS_SETTINGS
        if str(file_values.get(key) or "").strip()
        and key not in settings.secret_keys
        and not _env_value(values, key)
    ]


def print_unreached_hs_settings(
    settings: DeploySettings,
    env: str,
    values: dict[str, Any],
    file_values: Mapping[str, str],
    *,
    source: object,
    console: Console,
) -> None:
    """Warn that HS* settings in the env file stay local (see :func:`unreached_hs_settings`)."""
    keys = unreached_hs_settings(settings, values, file_values)
    if keys:
        console.print(
            f"  {', '.join(keys)} in {source} never reach the pods (only allow-listed keys go "
            f"into the Secret); set them under env: in {settings.values_file(env)}.",
            style="yellow",
            markup=False,
        )
