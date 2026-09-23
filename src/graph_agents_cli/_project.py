# Copyright 2026 Google LLC
# Modifications Copyright 2026 graph-agents-cli contributors
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

"""Project manifest reader (``graph-agents-cli-manifest.yaml``) and prerequisite guards.

The manifest fields are defined by the ProjectInfo model below. There is no legacy
``pyproject.toml`` fallback: the manifest is the only project marker.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click
import yaml

from graph_agents_cli._defaults import (
    DEFAULT_AGENT_GUIDANCE_FILENAME,
    DEFAULT_AUTH_POLICY,
    DEFAULT_CD,
    DEFAULT_MODEL_PROVIDER,
    DEFAULT_MODELS,
    DEFAULT_RUNTIME,
    ENVIRONMENTS,
    PROVIDER_KEY_VARS,
    default_secret_keys,
)

MANIFEST_FILENAME = "graph-agents-cli-manifest.yaml"
PRODUCT_POLICY_FILENAME = "product-policy.yaml"


@dataclass
class SecretsConfig:
    """The ``secrets:`` block: allow-listed Secret keys and their owner."""

    keys: list[str] = field(default_factory=list)
    owner: str = ""


@dataclass
class ProductApiConfig:
    """The ``product_api:`` block. ``policy_file`` is None when no policy is declared."""

    policy_file: str | None = None


@dataclass
class ProjectConfig:
    """Configuration read from ``graph-agents-cli-manifest.yaml``.

    Every manifest key is an attribute. ``create_params`` is kept as the raw
    dict (for ``generation_metadata``) and also exposed through typed
    properties with the defaults of ``graph_agents_cli._defaults``.
    """

    project_name: str = ""
    cli_version: str = ""
    agent_directory: str = "app"
    base_template: str = "langgraph"
    generated_at: str = ""
    language: str = "python"
    create_params: dict[str, Any] = field(default_factory=dict)
    environments: dict[str, dict[str, str]] = field(default_factory=dict)
    secrets: SecretsConfig = field(default_factory=SecretsConfig)
    product_api: ProductApiConfig = field(default_factory=ProductApiConfig)
    process: str | None = None

    # -- manifest aliases -------------------------------------------------

    @property
    def name(self) -> str:
        """The manifest's ``name`` key (alias of ``project_name``)."""
        return self.project_name

    # -- create_params, typed ----------------------------------------------

    @property
    def deployment_target(self) -> str:
        return str(self.create_params.get("deployment_target") or "none")

    @property
    def runtime(self) -> str:
        return str(self.create_params.get("runtime") or DEFAULT_RUNTIME)

    @property
    def model_provider(self) -> str:
        return str(self.create_params.get("model_provider") or DEFAULT_MODEL_PROVIDER)

    @property
    def model(self) -> str:
        recorded = self.create_params.get("model")
        if recorded:
            return str(recorded)
        return DEFAULT_MODELS.get(self.model_provider, "")

    @property
    def checkpointer(self) -> str:
        recorded = self.create_params.get("checkpointer")
        if recorded:
            return str(recorded)
        # D6: the deployed default is postgres; a local-only project uses memory.
        return "postgres" if self.deployment_target == "kubernetes" else "memory"

    @property
    def registry(self) -> str:
        return str(self.create_params.get("registry") or "")

    @property
    def cd(self) -> str:
        return str(self.create_params.get("cd") or DEFAULT_CD)

    @property
    def auth_policy(self) -> str:
        return str(self.create_params.get("auth_policy") or DEFAULT_AUTH_POLICY)

    @property
    def auth_policy_implemented(self) -> bool:
        recorded = self.create_params.get("auth_policy_implemented")
        if recorded is None:
            return self.auth_policy != "product-session"
        return bool(recorded)

    @property
    def agent_guidance_filename(self) -> str:
        return str(
            self.create_params.get("agent_guidance_filename") or DEFAULT_AGENT_GUIDANCE_FILENAME
        )

    # -- convenience --------------------------------------------------------

    @property
    def secret_keys(self) -> list[str]:
        return list(self.secrets.keys)

    @property
    def product_policy_file(self) -> str | None:
        return self.product_api.policy_file

    def provider_key_var(self) -> str:
        """The Secret key that carries the model provider's API key (CONTRACTS section 2)."""
        return PROVIDER_KEY_VARS.get(self.model_provider, "MODEL_API_KEY")

    def environment(self, env: str) -> tuple[str, str]:
        """Return ``(context, namespace)`` for ``env``.

        The manifest's ``environments:`` block wins. An environment the block
        does not name but the CLI knows (dev, staging, prod) falls back to an
        empty context and the ``<name>-<env>`` namespace convention
        (ASSUMPTIONS item 11). Anything else is a usage error.
        """
        recorded = self.environments.get(env)
        if recorded is not None:
            context = str(recorded.get("context") or "")
            namespace = str(recorded.get("namespace") or f"{self.project_name}-{env}")
            return context, namespace
        if env in ENVIRONMENTS:
            return "", f"{self.project_name}-{env}"
        known = ", ".join(sorted(set(self.environments) | set(ENVIRONMENTS)))
        raise click.UsageError(
            f"Unknown environment '{env}'. Known environments: {known}.\n"
            f"  Add it under environments: in {MANIFEST_FILENAME}."
        )

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any] | Any,
        filename: str = MANIFEST_FILENAME,
    ) -> ProjectConfig:
        """Create a ProjectConfig from a raw manifest mapping."""
        if not isinstance(data, Mapping):
            raise click.ClickException(f"malformed {filename}")

        cfg = cls()
        cfg.project_name = str(data.get("name") or cfg.project_name)
        cfg.cli_version = str(data.get("cli_version") or cfg.cli_version)
        cfg.agent_directory = str(data.get("agent_directory") or cfg.agent_directory)
        cfg.base_template = str(data.get("base_template") or cfg.base_template)
        cfg.generated_at = str(data.get("generated_at") or cfg.generated_at)
        cfg.language = str(data.get("language") or cfg.language)

        create_params = data.get("create_params")
        if create_params is None:
            create_params = {}
        elif not isinstance(create_params, Mapping):
            raise click.ClickException(f"malformed create_params in {filename}")
        cfg.create_params = dict(create_params)

        environments = data.get("environments") or {}
        if not isinstance(environments, Mapping):
            raise click.ClickException(f"malformed environments in {filename}")
        parsed_envs: dict[str, dict[str, str]] = {}
        for env_name, env_data in environments.items():
            if env_data is None:
                env_data = {}
            if not isinstance(env_data, Mapping):
                raise click.ClickException(f"malformed environments.{env_name} in {filename}")
            parsed_envs[str(env_name)] = {
                "context": str(env_data.get("context") or ""),
                "namespace": str(env_data.get("namespace") or f"{cfg.project_name}-{env_name}"),
            }
        cfg.environments = parsed_envs

        secrets = data.get("secrets") or {}
        if not isinstance(secrets, Mapping):
            raise click.ClickException(f"malformed secrets in {filename}")
        raw_keys = secrets.get("keys")
        if isinstance(raw_keys, str):
            raw_keys = [raw_keys]
        keys = [str(k) for k in raw_keys] if raw_keys else []
        if not keys:
            # Section 7 item 21: the default allow-list follows the provider and runtime.
            keys = default_secret_keys(cfg.model_provider, cfg.runtime)
        cfg.secrets = SecretsConfig(keys=keys, owner=str(secrets.get("owner") or ""))

        product_api = data.get("product_api") or {}
        if not isinstance(product_api, Mapping):
            raise click.ClickException(f"malformed product_api in {filename}")
        policy_file = product_api.get("policy_file")
        cfg.product_api = ProductApiConfig(policy_file=str(policy_file) if policy_file else None)

        process = data.get("process")
        cfg.process = str(process) if process else None

        return cfg


def _read_project_config_from_manifest(manifest_path: Path) -> ProjectConfig:
    """Read project configuration from a manifest file."""
    with open(manifest_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if data is None:
        data = {}
    return ProjectConfig.from_dict(data, filename=manifest_path.name)


def read_project_config(project_dir: str | None = None) -> ProjectConfig:
    """Read project metadata from ``graph-agents-cli-manifest.yaml``.

    Returns a default ``ProjectConfig`` when the directory holds no manifest.

    Args:
        project_dir: Directory containing the manifest. Defaults to the
            current working directory.
    """
    root = Path(project_dir) if project_dir else Path.cwd()
    manifest_path = root / MANIFEST_FILENAME
    if manifest_path.exists():
        return _read_project_config_from_manifest(manifest_path)
    return ProjectConfig()


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse a dotted numeric version into a comparable tuple.

    Raises ``ValueError`` on non-numeric components (e.g. a ``0.0.0-dev``
    source build), which callers treat as "version unknown, don't compare".
    """
    return tuple(int(x) for x in v.split("."))


def scaffold_older_than(cfg: ProjectConfig, version: str) -> bool:
    """True when the version that scaffolded ``cfg`` is older than ``version``.

    Best-effort: returns False when the scaffold version is missing or
    non-numeric, so callers fall back to generic guidance instead of guessing.
    """
    if not cfg.cli_version:
        return False
    try:
        return _parse_version(cfg.cli_version) < _parse_version(version)
    except ValueError:
        return False


def check_cli_version(cfg: ProjectConfig) -> None:
    """Warn if the running CLI version differs from the one that scaffolded the project.

    Emits a warning with upgrade guidance on a mismatch; never blocks execution.
    """
    cli_version = cfg.cli_version
    if not cli_version:
        return

    from graph_agents_cli import __version__

    try:
        project_ver = _parse_version(cli_version)
        cli_ver = _parse_version(__version__)
    except ValueError:
        return

    if cli_ver < project_ver:
        click.echo(
            f"\n⚠️  Version mismatch: project was scaffolded with graph-agents-cli {cli_version},"
            f" running {__version__}.\n"
            f"   Upgrade the CLI: uv tool install graph-agents-cli@{cli_version}\n",
            err=True,
        )
    elif cli_ver > project_ver:
        click.echo(
            f"\n⚠️  Version mismatch: project was scaffolded with graph-agents-cli {cli_version},"
            f" running {__version__}.\n"
            "   Upgrade the project: graph-agents-cli scaffold upgrade\n",
            err=True,
        )


def find_project_root(dir: Path | None = None) -> Path | None:
    """Find the project root by walking up looking for the manifest."""
    if dir is None:
        dir = Path.cwd()
    for parent in [dir, *dir.parents]:
        if (parent / MANIFEST_FILENAME).exists():
            return parent
    return None


def is_project_moved() -> bool:
    """Check if the project has been moved by comparing the current path with .venv/bin/activate."""
    root = find_project_root(Path.cwd())
    if not root:
        return False

    venv_dir = root / ".venv"
    # Support both Unix-style (bin) and Windows-style (Scripts) virtualenvs
    activate_script = venv_dir / "bin" / "activate"
    if not activate_script.exists():
        activate_script = venv_dir / "Scripts" / "activate"

    if not activate_script.exists():
        return False

    try:
        with open(activate_script, encoding="utf-8") as f:
            for line in f:
                if line.startswith("VIRTUAL_ENV="):
                    stored_path_str = line.split("=", 1)[1].strip().strip("'\"")
                    stored_path = Path(stored_path_str).resolve()
                    current_path = venv_dir.resolve()
                    return stored_path != current_path
    except Exception as e:
        logging.warning(f"Error checking if project moved: {e}")
    return False


# ---------------------------------------------------------------------------
# Prerequisite guards, reusable checks for CLI commands
# ---------------------------------------------------------------------------


def chdir_project_root(dir: Path | None = None) -> None:
    """Locate the project root relative to ``dir`` and chdir to it; raise if none."""
    if dir is None:
        dir = Path.cwd()
    root = find_project_root(dir)
    if not root:
        raise click.ClickException(
            f"No {MANIFEST_FILENAME} found in the current directory or its parents.\n"
            "  Run this command from your project root, or create a project first:\n"
            "    graph-agents-cli create my-agent"
        )
    # Only announce the root when we actually move (i.e. run from a subdir).
    if root.resolve() != Path.cwd().resolve():
        click.echo(f"Using project root directory: {root}", err=True)
    os.chdir(root)


def require_agent_directory(cfg: ProjectConfig) -> None:
    """Raise if the configured agent_directory doesn't exist. Assumes cwd is the project root."""
    agent_path = Path(cfg.agent_directory)
    if not agent_path.is_dir():
        raise click.ClickException(
            f"Agent directory '{cfg.agent_directory}' not found.\n"
            "  Ensure you're in the project root and that the directory exists.\n"
            f"  The agent_directory is configured in {MANIFEST_FILENAME}."
        )


def require_deployment_target(cfg: ProjectConfig) -> None:
    """Raise a UsageError when the project has no deployment target (``none``)."""
    if cfg.deployment_target in ("none", ""):
        raise click.UsageError(
            "No deployment target configured (deployment_target is 'none').\n"
            f"  Set create_params.deployment_target in {MANIFEST_FILENAME},\n"
            "  or add deployment support to your project:\n"
            "    graph-agents-cli scaffold enhance --deployment-target kubernetes"
        )


def find_project_config(project_dir: Path | None = None) -> ProjectConfig | None:
    """Read the manifest after resolving the project root from ``project_dir``.

    Returns None when no project root is found.
    """
    project_root_dir = find_project_root(project_dir)
    if project_root_dir is None:
        return None
    return read_project_config(str(project_root_dir))
