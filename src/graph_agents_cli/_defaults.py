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

"""Shared defaults: model providers, runtimes, targets, CD modes.

Model names are placeholders; verify
them at release time. Nothing here imports a model SDK.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

MODEL_PROVIDERS: tuple[str, ...] = ("openai", "anthropic", "gemini", "openai-compatible")

# Test-only provider (LangChain GenericFakeChatModel); never offered by `create`.
FAKE_PROVIDER = "fake"

DEFAULT_MODEL_PROVIDER = "openai"

DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-5-mini",
    "anthropic": "claude-sonnet-5",
    "gemini": "gemini-3.8-flash",
    "openai-compatible": "qwen2.5:14b",
}

PROVIDER_KEY_VARS: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GOOGLE_API_KEY",
    "openai-compatible": "MODEL_API_KEY",
}

RUNTIMES: tuple[str, ...] = ("fastapi", "langgraph-server")
DEFAULT_RUNTIME = "fastapi"

CHECKPOINTERS: tuple[str, ...] = ("memory", "postgres")

DEPLOYMENT_TARGETS: tuple[str, ...] = ("kubernetes", "none")
DEFAULT_DEPLOYMENT_TARGET = "kubernetes"

CD_MODES: tuple[str, ...] = ("argocd", "helm-push", "skip")
DEFAULT_CD = "skip"

AUTH_POLICIES: tuple[str, ...] = ("shared-bearer", "jwt", "custom")
DEFAULT_AUTH_POLICY = "shared-bearer"
# Policies scaffolded as a stub the project implements: the manifest records
# auth_policy_implemented: false for them, and deploy to staging/prod refuses
# until the developer flips it.
STUB_AUTH_POLICIES: frozenset[str] = frozenset({"custom"})
# Retired policy names still accepted from an existing manifest or AUTH_POLICY.
LEGACY_AUTH_POLICY_ALIASES: dict[str, str] = {"product-session": "custom"}

_warned_aliases: set[str] = set()


def normalize_auth_policy(value: str | None, *, warn: bool = True) -> str:
    """The current name of an auth policy read from a manifest or the environment.

    ``product-session`` is the retired name of ``custom``: it maps to ``custom``
    with a one-line deprecation warning (once per process). ``None`` or an
    empty value is the default policy.
    """
    name = (value or DEFAULT_AUTH_POLICY).strip().lower()
    alias = LEGACY_AUTH_POLICY_ALIASES.get(name)
    if alias is None:
        return name
    if warn and name not in _warned_aliases:
        _warned_aliases.add(name)
        print(
            f"Warning: auth_policy '{name}' is deprecated and read as '{alias}'; "
            f"record auth_policy: {alias} in graph-agents-cli-manifest.yaml.",
            file=sys.stderr,
        )
    return alias


def auth_policy_implemented_default(auth_policy: str) -> bool:
    """False for a policy scaffolded as a stub (``custom``), True otherwise."""
    return normalize_auth_policy(auth_policy, warn=False) not in STUB_AUTH_POLICIES


ENVIRONMENTS: tuple[str, ...] = ("dev", "staging", "prod")

DEFAULT_REGISTRY_HOST = "ghcr.io"
DEFAULT_REGISTRY_PLACEHOLDER = "ghcr.io/CHANGE-ME"
# The one command that replaces the placeholder everywhere `create` wrote it:
# `build` and `deploy` read the manifest, Argo CD and the chart read the values,
# and the CI workflows read .github/agent.env.
REGISTRY_FIX_COMMAND = "graph-agents-cli scaffold enhance --registry <host>/<org>"
REGISTRY_FIX_EFFECT = (
    "it sets create_params.registry in graph-agents-cli-manifest.yaml, image.repository "
    "in the chart's values.yaml and IMAGE_REPOSITORY in .github/agent.env"
)

DEFAULT_AGENT_GUIDANCE_FILENAME = "AGENTS.md"


def default_secret_keys(
    model_provider: str,
    runtime: str,
    api_token_envs: Sequence[str] = (),
    *,
    auth_policy: str = "shared-bearer",
) -> list[str]:
    """Allow-listed Secret keys for a new project.

    The list follows the model provider (its key variable), the runtime (the
    connection strings it reads) and the auth policy (``API_KEY`` only under
    ``shared-bearer``, the one policy that reads it). ``api_token_envs`` are
    the ``token_env`` variables of the ``auth: bearer`` APIs in
    ``api-policy.yaml``; they are appended so each token reaches the
    environment's Secret.
    """
    keys = [PROVIDER_KEY_VARS.get(model_provider, "MODEL_API_KEY"), "JUDGE_API_KEY"]
    if runtime == "langgraph-server":
        keys += ["DATABASE_URI", "REDIS_URI"]
    else:
        keys.append("POSTGRES_DSN")
    if normalize_auth_policy(auth_policy, warn=False) == "shared-bearer":
        keys.append("API_KEY")
    keys.append("LANGSMITH_API_KEY")
    for token_env in api_token_envs:
        if token_env and token_env not in keys:
            keys.append(token_env)
    return keys
