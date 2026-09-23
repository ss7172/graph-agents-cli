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

AUTH_POLICIES: tuple[str, ...] = ("shared-bearer", "product-session")
DEFAULT_AUTH_POLICY = "shared-bearer"

ENVIRONMENTS: tuple[str, ...] = ("dev", "staging", "prod")

DEFAULT_REGISTRY_HOST = "ghcr.io"
DEFAULT_REGISTRY_PLACEHOLDER = "ghcr.io/CHANGE-ME"

DEFAULT_AGENT_GUIDANCE_FILENAME = "GEMINI.md"

# LangGraph Server base image; verify licensing and runtime behavior before use.
LANGGRAPH_API_IMAGE = "langchain/langgraph-api:3.12"


def default_secret_keys(
    model_provider: str, runtime: str, product_token_env: str | None = None
) -> list[str]:
    """Allow-listed Secret keys for a new project (DECISIONS.md Section 7 item 21).

    ``product_token_env`` is the variable a ``auth: bearer`` product policy reads
    (``token_env``, default ``PRODUCT_API_TOKEN``); it is appended when given so
    the token reaches the environment's Secret (CONTRACTS section 4).
    """
    keys = [PROVIDER_KEY_VARS.get(model_provider, "MODEL_API_KEY"), "JUDGE_API_KEY"]
    if runtime == "langgraph-server":
        keys += ["DATABASE_URI", "REDIS_URI"]
    else:
        keys.append("POSTGRES_DSN")
    keys += ["API_KEY", "LANGSMITH_API_KEY"]
    if product_token_env and product_token_env not in keys:
        keys.append(product_token_env)
    return keys
