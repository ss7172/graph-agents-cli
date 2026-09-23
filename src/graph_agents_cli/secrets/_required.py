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

The chart mounts the Secret with ``optional: true``, so a missing Secret or key
only shows up once the pods start: a crash loop (no provider key, no database
URI) or every request answered 503 (no ``API_KEY`` under ``shared-bearer``).
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
"""

from __future__ import annotations

from typing import Any

from graph_agents_cli._defaults import PROVIDER_KEY_VARS, normalize_auth_policy
from graph_agents_cli.deploy._config import DeploySettings

# Providers whose key is optional: on-network OpenAI-compatible servers are often
# keyless, and the fake model needs none.
KEYLESS_PROVIDERS = frozenset({"openai-compatible", "fake"})


def _env_value(values: dict[str, Any], key: str) -> str:
    env = values.get("env") if isinstance(values.get("env"), dict) else {}
    value = env.get(key)
    return "" if value is None else str(value).strip()


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
    policy = normalize_auth_policy(
        _env_value(values, "AUTH_POLICY") or settings.auth_policy, warn=False
    )
    if policy == "shared-bearer":
        needed.add("API_KEY")
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
    return [k for k in settings.secret_keys if k in needed and not _env_value(values, k)]


def optional_keys(settings: DeploySettings, values: dict[str, Any]) -> list[str]:
    """The allow-listed keys that are not required, in allow-list order."""
    required = set(required_keys(settings, values))
    return [k for k in settings.secret_keys if k not in required]
