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
"""Deployment settings read from the project manifest.

Adapts ``ProjectConfig`` (CONTRACTS section 2 field names) to the small set of
values deploy, secrets, and infra need. Attributes are read defensively so the
commands work while ``_project.py`` is being finalised; every fallback follows
the manifest schema and Section 7 defaults.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from graph_agents_cli import _project
from graph_agents_cli._defaults import ENVIRONMENTS, default_secret_keys
from graph_agents_cli.deploy._kube import ConfigError, Target


@dataclass
class DeploySettings:
    """Everything deploy/secrets/infra need from the manifest."""

    project_name: str
    deployment_target: str = "kubernetes"
    runtime: str = "fastapi"
    model_provider: str = "openai"
    registry: str = ""
    cd: str = "skip"
    auth_policy: str = "shared-bearer"
    auth_policy_implemented: bool = True
    environments: dict[str, dict[str, str]] = field(default_factory=dict)
    secret_keys: list[str] = field(default_factory=list)
    secrets_owner: str = ""

    # -- derived -------------------------------------------------------------

    @property
    def release(self) -> str:
        return self.project_name

    @property
    def secret_name(self) -> str:
        return f"{self.project_name}-app"

    @property
    def chart_dir(self) -> Path:
        return Path("deployment") / "helm" / self.project_name

    def values_file(self, env: str) -> Path:
        return self.chart_dir / f"values-{env}.yaml"

    @property
    def image_repository(self) -> str:
        if not self.registry:
            raise ConfigError(
                "No registry is configured (create_params.registry is empty).\n"
                "  Set it in graph-agents-cli-manifest.yaml, e.g. registry: ghcr.io/my-org"
            )
        return f"{self.registry.rstrip('/')}/{self.project_name}"

    def environment(self, env: str) -> dict[str, str]:
        if env not in self.environments:
            known = ", ".join(sorted(self.environments)) or "none"
            raise ConfigError(
                f"Unknown environment {env!r}. Environments in the manifest: {known}."
            )
        return self.environments[env]

    def target(self, env: str) -> Target:
        spec = self.environment(env)
        namespace = spec.get("namespace") or f"{self.project_name}-{env}"
        return Target(context=spec.get("context") or None, namespace=namespace)

    # -- construction --------------------------------------------------------

    @classmethod
    def from_project(cls, cfg: Any) -> DeploySettings:
        cp = getattr(cfg, "create_params", None)
        cp = dict(cp) if isinstance(cp, Mapping) else {}

        def pick(attr: str, default: Any) -> Any:
            value = getattr(cfg, attr, None)
            if value is None:
                value = cp.get(attr)
            return default if value is None else value

        name = getattr(cfg, "project_name", "") or ""
        if not name:
            raise ConfigError("The manifest has no project name.")
        deployment_target = pick("deployment_target", "none")
        if deployment_target != "kubernetes":
            raise ConfigError(
                f"This project has deployment_target: {deployment_target}; nothing to deploy.\n"
                "  Re-scaffold with --deployment-target kubernetes to add a Helm chart."
            )
        runtime = pick("runtime", "fastapi")
        model_provider = pick("model_provider", "openai")
        auth_policy = pick("auth_policy", "shared-bearer")
        implemented = pick("auth_policy_implemented", None)
        if implemented is None:
            implemented = auth_policy != "product-session"

        environments: dict[str, dict[str, str]] = {}
        raw_envs = getattr(cfg, "environments", None)
        if isinstance(raw_envs, Mapping):
            for env, spec in raw_envs.items():
                spec = spec if isinstance(spec, Mapping) else {}
                environments[str(env)] = {
                    "context": str(spec.get("context") or ""),
                    "namespace": str(spec.get("namespace") or f"{name}-{env}"),
                }
        if not environments:
            environments = {
                env: {"context": "", "namespace": f"{name}-{env}"} for env in ENVIRONMENTS
            }

        secret_keys = getattr(cfg, "secret_keys", None)
        owner = ""
        raw_secrets = getattr(cfg, "secrets", None)
        if isinstance(raw_secrets, Mapping):
            if secret_keys is None:
                secret_keys = raw_secrets.get("keys")
            owner = str(raw_secrets.get("owner") or "")
        if secret_keys is None:
            secret_keys = default_secret_keys(model_provider, runtime)
        owner = getattr(cfg, "secrets_owner", None) or owner

        return cls(
            project_name=name,
            deployment_target=deployment_target,
            runtime=runtime,
            model_provider=model_provider,
            registry=str(pick("registry", "") or ""),
            cd=str(pick("cd", "skip") or "skip"),
            auth_policy=auth_policy,
            auth_policy_implemented=bool(implemented),
            environments=environments,
            secret_keys=[str(k) for k in secret_keys],
            secrets_owner=owner,
        )


def load_settings() -> DeploySettings:
    """chdir to the project root and read the manifest into ``DeploySettings``."""
    _project.chdir_project_root()
    return DeploySettings.from_project(_project.read_project_config())
