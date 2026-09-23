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

"""Template engine: four-layer cookiecutter rendering and conditional files.

Layers, later ones overwriting earlier ones: ``base_templates/_shared`` ->
``base_templates/python`` -> ``deployment_targets/<target>/{_shared,python}``
-> ``agents/<name>`` overlay. Runtime-specific files are selected with
``CONDITIONAL_FILES`` and ``select_runtime_files``, not with a fifth layer.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from typing import Any

import yaml
from cookiecutter.main import cookiecutter
from rich.prompt import Confirm, IntPrompt

from graph_agents_cli import _defaults
from graph_agents_cli._api_policy import POLICY_FILENAME as API_POLICY_FILENAME
from graph_agents_cli._api_policy import ApiSummary, bearer_token_envs
from graph_agents_cli._defaults import (
    DEFAULT_AGENT_GUIDANCE_FILENAME,
    DEFAULT_AUTH_POLICY,
    DEFAULT_CD,
    DEFAULT_MODEL_PROVIDER,
    DEFAULT_MODELS,
    PROVIDER_KEY_VARS,
    auth_policy_implemented_default,
    default_secret_keys,
)
from graph_agents_cli._output import Console

from .lock_utils import LOCK_FILENAMES, lock_filename, replace_lock_project_name
from .remote_template import get_base_template_name
from .version import cli_install_spec, get_current_version

# Root of the scaffold package: agents/, base_templates/, deployment_targets/.
# Module-level so tests can point the engine at a scratch tree.
SCAFFOLD_ROOT = pathlib.Path(__file__).resolve().parent.parent

TEMPLATE_CONFIG_FILE = "templateconfig.yaml"
MANIFEST_FILENAME = "graph-agents-cli-manifest.yaml"
EXAMPLE_API_TOOL = "{agent_directory}/tools/example_api.py"
LANGGRAPH_SERVER_DOCKERFILE = "Dockerfile.langgraph-server"


def agents_dir() -> pathlib.Path:
    return SCAFFOLD_ROOT / "agents"


def base_templates_dir() -> pathlib.Path:
    return SCAFFOLD_ROOT / "base_templates"


def deployment_targets_dir() -> pathlib.Path:
    return SCAFFOLD_ROOT / "deployment_targets"


# =============================================================================
# Choice tables (values from graph_agents_cli._defaults)
# =============================================================================

DEPLOYMENT_TARGETS: dict[str, dict[str, str]] = {
    "kubernetes": {
        "display_name": "kubernetes",
        "description": "Helm chart on your Kubernetes cluster",
    },
    "none": {
        "display_name": "none",
        "description": "No deployment (local development only)",
    },
}
assert tuple(DEPLOYMENT_TARGETS) == _defaults.DEPLOYMENT_TARGETS

RUNTIMES: dict[str, dict[str, str]] = {
    "fastapi": {
        "display_name": "fastapi",
        "description": "uvicorn + FastAPI; checkpointer bound in the app; no license",
    },
    "langgraph-server": {
        "display_name": "langgraph-server",
        "description": "LangGraph Server image; native Assistants/Threads API; needs Redis",
    },
}
assert tuple(RUNTIMES) == _defaults.RUNTIMES

MODEL_PROVIDERS: dict[str, dict[str, str]] = {
    "openai": {"display_name": "openai", "description": "OpenAI API (OPENAI_API_KEY)"},
    "anthropic": {"display_name": "anthropic", "description": "Anthropic API (ANTHROPIC_API_KEY)"},
    "gemini": {"display_name": "gemini", "description": "Google AI Studio (GOOGLE_API_KEY)"},
    "openai-compatible": {
        "display_name": "openai-compatible",
        "description": "Ollama, vLLM, TGI... (MODEL_API_KEY, OPENAI_BASE_URL)",
    },
}
assert tuple(MODEL_PROVIDERS) == _defaults.MODEL_PROVIDERS

CHECKPOINTERS: dict[str, dict[str, str]] = {
    "memory": {"display_name": "memory", "description": "In-process; state lost on restart"},
    "postgres": {
        "display_name": "postgres",
        "description": "Postgres in the cluster (deployed default)",
    },
}
assert tuple(CHECKPOINTERS) == _defaults.CHECKPOINTERS

CD_MODES: dict[str, dict[str, str]] = {
    "argocd": {
        "display_name": "argocd",
        "description": "GitOps: CI commits the image tag, Argo CD syncs",
    },
    "helm-push": {
        "display_name": "helm-push",
        "description": "CI runs helm upgrade from a self-hosted runner",
    },
    "skip": {"display_name": "skip", "description": "No CD; deploy from a workstation"},
}
assert tuple(CD_MODES) == _defaults.CD_MODES

AUTH_POLICIES: dict[str, dict[str, str]] = {
    "shared-bearer": {
        "display_name": "shared-bearer",
        "description": "Authorization: Bearer <API_KEY>",
    },
    "jwt": {
        "display_name": "jwt",
        "description": "Per-user principals from a verified OIDC/JWT bearer token",
    },
    "custom": {
        "display_name": "custom",
        "description": "Your own policy in app/policies/custom.py (fail-closed stub until implemented)",
    },
}
assert tuple(AUTH_POLICIES) == _defaults.AUTH_POLICIES


# =============================================================================
# Combination table (runtime x checkpointer x target)
# =============================================================================

# (runtime, checkpointer, deployment_target) -> (valid, note). An in-memory
# checkpointer is refused on kubernetes: replicas and restarts would lose state.
COMBINATIONS: dict[tuple[str, str, str], tuple[bool, str]] = {
    ("fastapi", "memory", "none"): (True, "local dev under uvicorn; state lost on restart"),
    ("fastapi", "memory", "kubernetes"): (
        False,
        "refused; multi-replica and restarts lose state",
    ),
    ("fastapi", "postgres", "none"): (True, "local dev against a local or docker Postgres"),
    ("fastapi", "postgres", "kubernetes"): (True, "default for kubernetes"),
    ("langgraph-server", "memory", "none"): (
        True,
        "`langgraph dev` in-memory server only; not deployable",
    ),
    ("langgraph-server", "memory", "kubernetes"): (False, "refused"),
    ("langgraph-server", "postgres", "kubernetes"): (
        True,
        "chart adds Redis; server owns persistence",
    ),
    ("langgraph-server", "postgres", "none"): (
        True,
        "`run` and `playground` use `langgraph dev` (in-memory) locally; postgres is only "
        "the recorded deployed default",
    ),
}


def default_checkpointer(deployment_target: str) -> str:
    """``none`` (local only) defaults to memory, kubernetes to postgres."""
    return "postgres" if deployment_target == "kubernetes" else "memory"


def validate_combination(
    runtime: str,
    checkpointer: str,
    deployment_target: str,
    cd: str | None = None,
) -> None:
    """Enforce the combination table and the ``--cd`` rule; raise ValueError with the reason."""
    key = (runtime, checkpointer, deployment_target)
    entry = COMBINATIONS.get(key)
    if entry is None:
        raise ValueError(
            f"Unknown combination runtime={runtime} checkpointer={checkpointer} "
            f"deployment_target={deployment_target}."
        )
    valid, note = entry
    if not valid:
        raise ValueError(
            f"Invalid combination: runtime={runtime} checkpointer={checkpointer} "
            f"deployment_target={deployment_target} ({note}).\n"
            "  Valid: fastapi or langgraph-server with postgres on kubernetes; any "
            "checkpointer with --deployment-target none."
        )
    if cd is not None and cd != "skip" and deployment_target != "kubernetes":
        raise ValueError(
            f"--cd {cd} requires --deployment-target kubernetes "
            f"(got deployment_target={deployment_target})."
        )


# =============================================================================
# Conditional files
# =============================================================================
# Maps a path in the rendered project to its inclusion condition. Paths that
# fail their condition are renamed to unused_* and removed afterwards.
#
# The config dict carries: deployment_target, runtime, cd, has_api_policy.
# `deployment/argocd` is listed before `deployment` so its own rule is applied
# even when the whole directory is kept.

CONDITIONAL_FILES: dict[str, Any] = {
    ".github/workflows/staging.yaml": lambda c: c.get("cd", "skip") != "skip",
    ".github/workflows/promote-to-prod.yaml": lambda c: c.get("cd", "skip") != "skip",
    ".github/CODEOWNERS": lambda c: c.get("cd", "skip") != "skip",
    "deployment/argocd": lambda c: c.get("cd") == "argocd",
    "deployment": lambda c: c.get("deployment_target") == "kubernetes",
    API_POLICY_FILENAME: lambda c: bool(c.get("has_api_policy", False)),
    EXAMPLE_API_TOOL: lambda c: bool(c.get("has_api_policy", False)),
}


def apply_conditional_files(
    project_path: pathlib.Path,
    config: dict[str, Any],
    agent_directory: str = "app",
) -> None:
    """Rename every conditional path whose condition is False to ``unused_*``.

    Args:
        project_path: Path to the generated project directory
        config: dict with deployment_target, runtime, cd, has_api_policy
        agent_directory: replaces the ``{agent_directory}`` placeholder in paths
    """
    for rel_path_template, condition_fn in CONDITIONAL_FILES.items():
        rel_path = rel_path_template.replace("{agent_directory}", agent_directory)
        file_path = project_path / rel_path

        if not file_path.exists():
            continue

        if condition_fn(config):
            logging.debug("Conditional file '%s' condition True, keeping", rel_path)
            continue

        unused_path = file_path.parent / f"unused_{file_path.name}"
        logging.debug(
            "Conditional file '%s' condition False, renaming to %s", rel_path, unused_path.name
        )
        if unused_path.exists():
            if unused_path.is_dir():
                shutil.rmtree(unused_path)
            else:
                unused_path.unlink()
        file_path.rename(unused_path)


def _remove_unused_paths(project_path: pathlib.Path) -> None:
    """Delete the ``unused_*`` files and directories left by conditional templates.

    ``rglob`` (not ``glob.glob``) so entries under dot-directories such as
    ``.github/workflows`` are found too.
    """
    # Longest paths first so a directory is removed after anything inside it.
    for unused_path in sorted(project_path.rglob("unused_*"), key=lambda p: -len(p.parts)):
        if unused_path.is_dir():
            shutil.rmtree(unused_path)
            logging.debug("Deleted unused directory: %s", unused_path)
        elif unused_path.exists():
            unused_path.unlink()
            logging.debug("Deleted unused file: %s", unused_path)


def select_runtime_files(project_path: pathlib.Path, runtime: str, project_name: str) -> None:
    """Pick the runtime's Dockerfile and bundled lock.

    The template ships ``Dockerfile`` (fastapi) and ``Dockerfile.langgraph-server``;
    under ``langgraph-server`` the latter replaces the former, otherwise it is
    deleted. Of ``uv-fastapi.lock`` and ``uv-langgraph-server.lock`` the matching
    one becomes ``uv.lock`` (project-name placeholder filled in) and the other is
    deleted. A template that ships neither bundled lock gets a warning.
    """
    console = Console()
    dockerfile = project_path / "Dockerfile"
    server_dockerfile = project_path / LANGGRAPH_SERVER_DOCKERFILE

    if runtime == "langgraph-server":
        if server_dockerfile.exists():
            if dockerfile.exists():
                dockerfile.unlink()
            server_dockerfile.rename(dockerfile)
            logging.debug("Selected %s as Dockerfile", LANGGRAPH_SERVER_DOCKERFILE)
        else:
            console.print(
                f"⚠️  The template ships no {LANGGRAPH_SERVER_DOCKERFILE}; keeping Dockerfile as is.",
                style="yellow",
            )
    elif server_dockerfile.exists():
        server_dockerfile.unlink()
        logging.debug("Removed %s (runtime %s)", LANGGRAPH_SERVER_DOCKERFILE, runtime)

    wanted = lock_filename(runtime)
    wanted_path = project_path / wanted
    uv_lock = project_path / "uv.lock"
    if wanted_path.exists():
        uv_lock.write_text(
            replace_lock_project_name(wanted_path.read_text(encoding="utf-8"), project_name),
            encoding="utf-8",
        )
        wanted_path.unlink()
        logging.debug("Selected %s as uv.lock", wanted)
    elif uv_lock.exists():
        logging.debug("Template ships its own uv.lock; no bundled runtime lock to select")
    else:
        console.print(
            f"⚠️  No bundled lock ({wanted}) in the template; run `uv lock` in the project.",
            style="yellow",
        )
    for other in LOCK_FILENAMES.values():
        if other != wanted:
            (project_path / other).unlink(missing_ok=True)


# =============================================================================
# Dependencies helper (remote templates inheriting base-template deps)
# =============================================================================


def _add_dependencies(
    project_path: pathlib.Path,
    dependencies: list[str],
    success_message: str,
    auto_approve: bool = False,
    interactive: bool = False,
) -> bool:
    """Add dependencies with ``uv add``, confirming first in interactive mode."""
    if not dependencies:
        return True

    console = Console()
    deps_str = shlex.join(dependencies)

    should_add = True
    if interactive:
        should_add = Confirm.ask("\n? Add these dependencies automatically?", default=True)

    if not should_add:
        console.print("\n⚠️  Skipped dependency installation.", style="yellow")
        console.print("   To add them manually later, run:", style="dim")
        console.print(f"       cd {project_path.name}", style="dim")
        console.print(f"       uv add {deps_str}\n", style="dim")
        return False

    try:
        if auto_approve:
            console.print(
                f"✓ Auto-installing dependencies: {', '.join(dependencies)}",
                style="bold cyan",
            )
        else:
            console.print(f"\n✓ Running: uv add {deps_str}", style="bold cyan")

        from graph_agents_cli._runner import run_resolved
        from graph_agents_cli._tools import ToolNotFoundError

        cmd = ["uv", "add", *dependencies]
        result = run_resolved(
            cmd,
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )

        if not auto_approve:
            output_lines = result.stderr.strip().split("\n")
            for line in output_lines:
                if "Resolved" in line or "Installed" in line:
                    console.print(f"  {line}", style="dim")
                    break

        console.print(f"✓ {success_message}\n", style="bold green")
        return True

    except subprocess.CalledProcessError as e:
        console.print(f"\n✗ Failed to add dependencies: {e.stderr.strip()}", style="bold red")
        console.print("  You can add them manually:", style="yellow")
        console.print(f"      cd {project_path.name}", style="dim")
        console.print(f"      uv add {deps_str}\n", style="dim")
        return False
    except ToolNotFoundError:
        console.print("\n✗ uv command not found. Please install uv first.", style="bold red")
        console.print("  Install from: https://docs.astral.sh/uv/", style="dim")
        console.print("\n  To add dependencies manually:", style="yellow")
        console.print(f"      cd {project_path.name}", style="dim")
        console.print(f"      uv add {deps_str}\n", style="dim")
        return False


def add_base_template_dependencies(
    project_path: pathlib.Path,
    base_dependencies: list[str],
    base_template_name: str,
    auto_approve: bool = False,
    interactive: bool = False,
) -> bool:
    """Add a base template's ``extra_dependencies`` to a project rendered from a remote template."""
    if not base_dependencies:
        return True

    console = Console()
    console.print(
        f"\n✓ Ensuring base template '{base_template_name}' dependencies",
        style="bold cyan",
    )
    console.print("  Adding the following dependencies:", style="white")
    for dep in base_dependencies:
        console.print(f"    • {dep}", style="yellow")

    return _add_dependencies(
        project_path=project_path,
        dependencies=base_dependencies,
        success_message="Dependencies added successfully",
        auto_approve=auto_approve,
        interactive=interactive,
    )


# =============================================================================
# Agent directory validation (Python identifiers)
# =============================================================================


def validate_agent_directory_name(
    agent_dir: str, allow_dot: bool = False, language: str = "python"
) -> None:
    """Validate that an agent directory name is a valid Python module path.

    Args:
        agent_dir: The agent directory name to validate
        allow_dot: If True, allows "." as a special value indicating flat structure
        language: Kept for call compatibility; only ``python`` exists

    Raises:
        ValueError: If the agent directory name is not valid
    """
    if agent_dir == ".":
        if allow_dot:
            return
        raise ValueError(
            "Agent directory '.' is not valid in this context. "
            "Use '.' only to indicate flat structure templates."
        )
    # Allowlist: one or more path components of letters, digits, hyphens and
    # underscores. This blocks absolute paths, dot-dot, backslashes, tilde,
    # Windows drives, and any other path-traversal payload.
    if not re.match(r"^[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*$", agent_dir):
        raise ValueError(
            f"Invalid agent directory name '{agent_dir}'. It can only contain "
            "letters, numbers, hyphens, underscores, and forward slashes (no "
            "absolute paths or dot-dot components)."
        )
    if language != "python":
        raise ValueError(f"Unsupported language '{language}': only python templates exist.")
    if "-" in agent_dir:
        raise ValueError(
            f"Agent directory '{agent_dir}' contains hyphens (-) which are not allowed. "
            "Agent directories must be valid Python identifiers since they're used as module names. "
            "Please use underscores (_) or lowercase letters instead."
        )
    for component in agent_dir.split("/"):
        if not component.isidentifier():
            raise ValueError(
                f"Agent directory '{agent_dir}' is not a valid Python identifier. "
                "Agent directories must be valid Python identifiers since they're used as module names. "
                "Please use only lowercase letters, numbers, and underscores, and don't start with a number."
            )


# =============================================================================
# Template discovery
# =============================================================================


def get_available_agents(
    deployment_target: str | None = None, include_hidden: bool = False
) -> dict:
    """Load the bundled agent templates, numbered from 1 for display.

    Each agent dict includes: name, display_name, description, language, framework.

    Args:
        deployment_target: Optional deployment target to filter agents
        include_hidden: If True, include agents marked as hidden in templateconfig
    """
    PRIORITY_ORDER = {"langgraph": 0}

    agents_list = []
    agents_root = agents_dir()
    if not agents_root.is_dir():
        return {}

    for agent_dir in sorted(agents_root.iterdir()):
        if not agent_dir.is_dir() or agent_dir.name.startswith("__"):
            continue
        template_config_path = agent_dir / ".template" / TEMPLATE_CONFIG_FILE
        if not template_config_path.exists():
            continue
        try:
            with open(template_config_path, encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
            agent_name = agent_dir.name
            settings = config.get("settings", {})

            if not include_hidden and config.get("hidden", False):
                continue

            if deployment_target:
                targets = settings.get("deployment_targets", [])
                if isinstance(targets, str):
                    targets = [targets]
                if deployment_target not in targets:
                    continue

            language = settings.get("language", "python")
            tags = settings.get("tags", [])
            framework = "langgraph" if "langgraph" in tags else "other"

            agents_list.append(
                {
                    "name": agent_name,
                    "display_name": config.get("display_name", agent_name),
                    "description": config.get("description", "No description available"),
                    "language": language,
                    "framework": framework,
                    "priority": PRIORITY_ORDER.get(agent_name, 100),
                }
            )
        except Exception as e:
            logging.warning(f"Could not load agent from {agent_dir}: {e}")

    agents_list.sort(key=lambda agent: (agent["priority"], agent["name"]))
    return {i + 1: agent for i, agent in enumerate(agents_list)}


def get_available_base_templates() -> list[str]:
    """Names of the bundled templates a remote template may inherit from."""
    agents = get_available_agents(include_hidden=True)
    return sorted(agent_info["name"] for agent_info in agents.values())


def validate_base_template(base_template: str) -> bool:
    """True when ``base_template`` names a bundled template."""
    return base_template in get_available_base_templates()


def load_template_config(template_dir: pathlib.Path) -> dict[str, Any]:
    """Read ``templateconfig.yaml`` from a ``.template`` directory ({} if absent)."""
    config_file = template_dir / TEMPLATE_CONFIG_FILE
    if not config_file.exists():
        return {}

    try:
        with open(config_file, encoding="utf-8") as f:
            config = yaml.safe_load(f)
            return config if config else {}
    except Exception as e:
        logging.error(f"Error loading template config: {e}")
        return {}


def get_agent_language(agent_name: str, remote_config: dict[str, Any] | None = None) -> str:
    """The template language: always ``python``; anything else is refused."""
    if remote_config:
        config = remote_config
    else:
        config = load_template_config(agents_dir() / agent_name / ".template")

    language = (config or {}).get("settings", {}).get("language", "python")
    if language != "python":
        raise ValueError(
            f"Template '{agent_name}' declares language '{language}'; only python templates "
            "are supported."
        )
    return "python"


def get_deployment_targets(agent_name: str, remote_config: dict[str, Any] | None = None) -> list:
    """Get available deployment targets for the selected agent."""
    if remote_config:
        config = remote_config
    else:
        config = load_template_config(agents_dir() / agent_name / ".template")

    if not config:
        return []

    targets = config.get("settings", {}).get("deployment_targets", [])
    return targets if isinstance(targets, list) else [targets]


def get_template_path(agent_name: str) -> pathlib.Path:
    """Get the absolute path to a bundled agent's ``.template`` directory."""
    template_path = agents_dir() / agent_name / ".template"
    logging.debug("Looking for template in: %s", template_path)
    if not template_path.exists():
        raise ValueError(f"Template directory not found at {template_path}")
    return template_path


# =============================================================================
# Interactive prompts
# =============================================================================


def prompt_choice(
    title: str,
    choices: dict[str, dict[str, str]],
    default_value: str | None = None,
    *,
    header: str | None = None,
    prompt: str = "Enter the number of your choice",
) -> str:
    """Numbered menu over ``choices`` (key -> {display_name, description})."""
    console = Console()
    keys = list(choices)
    default_idx = 1
    if default_value and default_value in keys:
        default_idx = keys.index(default_value) + 1

    console.print(f"\n> {title}")
    if header:
        console.print(f"\n  [bold cyan]{header}[/]")
    for idx, key in enumerate(keys, 1):
        info = choices[key]
        name_padded = info.get("display_name", key).ljust(18)
        description = info.get("description", "")
        current = "  [dim cyan](current)[/]" if key == default_value else ""
        console.print(f"     {idx}. [bold]{name_padded}[/] [dim]{description}[/]{current}")

    while True:
        choice = IntPrompt.ask(f"\n{prompt}", default=default_idx, show_default=True)
        if 1 <= choice <= len(keys):
            return keys[choice - 1]
        console.print(f"Please enter a number between 1 and {len(keys)}.", style="yellow")


def prompt_deployment_target(
    agent_name: str,
    remote_config: dict[str, Any] | None = None,
    default_value: str | None = None,
) -> str:
    """Ask the user to select a deployment target the agent supports."""
    targets = get_deployment_targets(agent_name, remote_config=remote_config)
    if not targets:
        return ""
    choices = {
        t: DEPLOYMENT_TARGETS.get(t, {"display_name": t, "description": ""}) for t in targets
    }
    return prompt_choice(
        "Please select a deployment target:",
        choices,
        default_value,
        header="☁️  Deployment Targets",
        prompt="Enter the number of your deployment target choice",
    )


def prompt_runtime(default_value: str | None = None) -> str:
    return prompt_choice("Please select a runtime:", RUNTIMES, default_value, header="Runtimes")


def prompt_model_provider(default_value: str | None = None) -> str:
    return prompt_choice(
        "Please select a model provider:", MODEL_PROVIDERS, default_value, header="Providers"
    )


def prompt_checkpointer(default_value: str | None = None) -> str:
    return prompt_choice(
        "Please select the deployed checkpointer:",
        CHECKPOINTERS,
        default_value,
        header="Checkpointers",
    )


def prompt_cd(default_value: str | None = None) -> str:
    return prompt_choice("Please select a CD mode:", CD_MODES, default_value, header="CD Modes")


def prompt_auth_policy(default_value: str | None = None) -> str:
    return prompt_choice(
        "Please select an authentication policy:",
        AUTH_POLICIES,
        default_value,
        header="Auth Policies",
    )


# =============================================================================
# Cookiecutter context
# =============================================================================

COPY_WITHOUT_RENDER: list[str] = [
    "deployment/helm/*/templates/*",
    "deployment/helm/*/templates/**/*",
    "*.tpl",
    ".github/workflows/*",
    "*.lock",
    "*.ipynb",
    "node_modules/**",
    ".venv/**",
    "__pycache__/**",
    ".git/*",
]


def build_cookiecutter_context(
    *,
    project_name: str,
    agent_name: str,
    deployment_target: str,
    runtime: str,
    model_provider: str = DEFAULT_MODEL_PROVIDER,
    model: str | None = None,
    checkpointer: str | None = None,
    registry: str = "",
    cd: str = DEFAULT_CD,
    auth_policy: str = DEFAULT_AUTH_POLICY,
    has_api_policy: bool = False,
    apis: tuple[ApiSummary, ...] | list[ApiSummary] = (),
    process: str | None = None,
    agent_guidance_filename: str = DEFAULT_AGENT_GUIDANCE_FILENAME,
    agent_directory: str = "app",
    template_config: dict[str, Any] | None = None,
    recorded_base_template: str | None = None,
    generated_at: str | None = None,
    auth_policy_implemented: bool | None = None,
) -> dict[str, Any]:
    """The variables every template may use.

    List-valued variables are wrapped in a one-element list because cookiecutter
    treats a bare list as a choice and would keep only its first item. ``apis``
    summarises the declared APIs of ``api-policy.yaml`` (name, base_url_env,
    auth, token_env; the first one drives the example tool) and every
    ``auth: bearer`` API's ``token_env`` joins ``secret_keys``.
    ``auth_policy_implemented`` is derived from ``auth_policy`` unless a
    recorded value is passed (an in-folder re-render keeps the developer's flip).
    """
    settings = (template_config or {}).get("settings", {})
    tags = settings.get("tags", []) or []
    model = model or DEFAULT_MODELS.get(model_provider, "")
    checkpointer = checkpointer or default_checkpointer(deployment_target)
    api_summaries = list(apis) if has_api_policy else []
    return {
        "project_name": project_name,
        "agent_name": agent_name,
        "package_version": get_current_version(),
        "generated_at": generated_at or datetime.now(tz=UTC).isoformat(),
        "agent_directory": agent_directory,
        "language": "python",
        "deployment_target": deployment_target,
        "runtime": runtime,
        "model_provider": model_provider,
        "model": model,
        "provider_key_var": PROVIDER_KEY_VARS.get(model_provider, "MODEL_API_KEY"),
        "checkpointer": checkpointer,
        "registry": registry or "",
        "cd": cd,
        "auth_policy": auth_policy,
        # Provided so the manifest template need not derive it.
        "auth_policy_implemented": (
            auth_policy_implemented_default(auth_policy)
            if auth_policy_implemented is None
            else bool(auth_policy_implemented)
        ),
        "agent_guidance_filename": agent_guidance_filename,
        "process": process or "",
        "has_api_policy": bool(has_api_policy),
        "apis": [[summary.as_context() for summary in api_summaries]],
        "secret_keys": [
            default_secret_keys(model_provider, runtime, bearer_token_envs(api_summaries))
        ],
        "default_judge_model": model,
        "cli_install_spec": cli_install_spec(),
        "tags": [list(tags)],
        "settings": settings,
        "recorded_base_template": recorded_base_template or agent_name,
        "_copy_without_render": list(COPY_WITHOUT_RENDER),
    }


# =============================================================================
# Rendering
# =============================================================================


def _resolve_agent_directory(
    template_config: dict[str, Any],
    cli_overrides: dict[str, Any] | None,
    remote_template_path: pathlib.Path | None,
) -> str:
    """Agent directory from CLI override or template settings; ``.`` means flat structure."""
    agent_dir = None
    if (
        cli_overrides
        and "settings" in cli_overrides
        and "agent_directory" in cli_overrides["settings"]
    ):
        agent_dir = cli_overrides["settings"]["agent_directory"]
    else:
        agent_dir = template_config.get("settings", {}).get("agent_directory", "app")

    if agent_dir == ".":
        if remote_template_path:
            folder_name = remote_template_path.name.replace("-", "_")
            logging.debug(
                "Flat structure (-dir .): deriving target '%s' from folder name", folder_name
            )
            agent_dir = folder_name
        else:
            logging.debug("Flat structure (-dir .): using 'app' as fallback")
            agent_dir = "app"

    validate_agent_directory_name(agent_dir)
    return agent_dir


def process_template(
    *,
    agent_name: str,
    template_dir: pathlib.Path,
    project_name: str,
    deployment_target: str,
    runtime: str,
    model_provider: str = DEFAULT_MODEL_PROVIDER,
    model: str | None = None,
    checkpointer: str | None = None,
    registry: str = "",
    cd: str = DEFAULT_CD,
    auth_policy: str = DEFAULT_AUTH_POLICY,
    has_api_policy: bool = False,
    apis: tuple[ApiSummary, ...] | list[ApiSummary] = (),
    process: str | None = None,
    output_dir: pathlib.Path | None = None,
    remote_template_path: pathlib.Path | None = None,
    remote_config: dict[str, Any] | None = None,
    in_folder: bool = False,
    overlay_is_project: bool = False,
    cli_overrides: dict[str, Any] | None = None,
    remote_spec: Any | None = None,
    recorded_base_template: str | None = None,
    agent_guidance_filename: str = DEFAULT_AGENT_GUIDANCE_FILENAME,
    auth_policy_implemented: bool | None = None,
) -> pathlib.Path:
    """Render the template layers into a new project and return its path.

    Args:
        agent_name: Name of the agent template to use
        template_dir: The ``.template`` directory of the selected template
        project_name: Name of the project to create
        deployment_target: ``kubernetes`` or ``none``
        runtime, model_provider, model, checkpointer, registry, cd, auth_policy:
            the create parameters (validated by the caller)
        has_api_policy: keep ``api-policy.yaml`` and the example API tool
        apis: the APIs the policy declares (see ``build_cookiecutter_context``)
        process: governing process document (path or string), recorded verbatim
        output_dir: Optional output directory path, defaults to current directory
        remote_template_path: Optional path to remote template for overlay
        remote_config: Optional remote template configuration
        in_folder: Render directly into ``output_dir`` instead of a subdirectory
        overlay_is_project: True when the overlay source is the output project
            itself (``--agent local@.``), so its manifest is the project's own
            and is copied. False for any other source, whose manifest describes
            the template and is skipped.
        cli_overrides: CLI override values that take precedence over template config
        remote_spec: the parsed remote spec, when any (unused, kept for callers)
        recorded_base_template: what the manifest records as base_template
        agent_guidance_filename: name of the root guidance file
        auth_policy_implemented: recorded flag to keep on an in-folder re-render (None derives it)
    """
    logging.debug("Processing template from %s", template_dir)
    logging.debug("Project name: %s", project_name)
    logging.debug("Output directory: %s", output_dir)

    is_remote = remote_template_path is not None

    if is_remote:
        base_template_name = get_base_template_name(remote_config or {})
        agent_path = agents_dir() / base_template_name
        logging.debug("Remote template using base: %s", base_template_name)
    elif cli_overrides and cli_overrides.get("base_template"):
        base_template_name = cli_overrides["base_template"]
        agent_path = agents_dir() / base_template_name
        logging.debug("Using base template override: %s", base_template_name)
    else:
        base_template_name = agent_name
        agent_path = pathlib.Path(template_dir).parent

    logging.debug("agent path: %s", agent_path)
    if not agent_path.exists():
        # Fail here rather than carry on: an unresolvable base silently copies
        # no agent layer and produces a half-built project.
        raise ValueError(
            f"Base template '{base_template_name}' not found at {agent_path}. "
            "A template declares its base in .template/templateconfig.yaml; "
            f"'{base_template_name}' is not one this CLI version provides."
        )

    template_config = (
        remote_config if remote_config else load_template_config(pathlib.Path(template_dir))
    )
    if not template_config:
        raise ValueError("Could not load template config")

    get_agent_language(agent_name, template_config)
    agent_directory = _resolve_agent_directory(template_config, cli_overrides, remote_template_path)

    available_targets = template_config.get("settings", {}).get("deployment_targets", [])
    if isinstance(available_targets, str):
        available_targets = [available_targets]
    if deployment_target not in available_targets:
        raise ValueError(
            f"Invalid deployment target '{deployment_target}'. Available targets: {available_targets}"
        )

    destination_dir = output_dir if output_dir else pathlib.Path.cwd()
    destination_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = pathlib.Path(temp_dir)
        original_dir = pathlib.Path.cwd()

        try:
            os.chdir(temp_path)

            cookiecutter_template = temp_path / "template"
            project_template = cookiecutter_template / "{{cookiecutter.project_name}}"
            project_template.mkdir(parents=True)

            # 1a. Shared base template files
            shared_base_path = base_templates_dir() / "_shared"
            if shared_base_path.exists():
                copy_files(shared_base_path, project_template, agent_name, overwrite=True)
                logging.debug("1a. Copied shared base template from %s", shared_base_path)

            # 1b. Python base template files
            language_base_path = base_templates_dir() / "python"
            if language_base_path.exists():
                copy_files(language_base_path, project_template, agent_name, overwrite=True)
                logging.debug("1b. Copied python base template from %s", language_base_path)
            else:
                raise FileNotFoundError(f"Language base template not found: {language_base_path}")

            # 2. Deployment target layer
            if deployment_target in DEPLOYMENT_TARGETS:
                for layer in ("_shared", "python"):
                    layer_path = deployment_targets_dir() / deployment_target / layer
                    if layer_path.exists():
                        copy_files(
                            layer_path, project_template, agent_name=agent_name, overwrite=True
                        )
                        logging.debug("2. Copied deployment files from %s", layer_path)

            # 3. Agent overlay: app/, tests/, deployment/, then the remaining
            # top-level items so a framework template can own the whole project.
            if is_remote or (cli_overrides and cli_overrides.get("base_template")):
                template_agent_directory = "app"
            else:
                template_agent_directory = template_config.get("settings", {}).get(
                    "agent_directory", "app"
                )

            source_agent_folder = agent_path / template_agent_directory
            if source_agent_folder.exists():
                logging.debug(
                    "3. Copying agent folder %s -> %s", template_agent_directory, agent_directory
                )
                copy_files(
                    source_agent_folder,
                    project_template / agent_directory,
                    agent_name,
                    overwrite=True,
                )

            other_folders = ["tests", "deployment"]
            for folder in other_folders:
                agent_folder = agent_path / folder
                if agent_folder.exists():
                    logging.debug("3. Copying %s folder with override", folder)
                    copy_files(agent_folder, project_template / folder, agent_name, overwrite=True)

            already_copied = {
                ".template",
                "__pycache__",
                template_agent_directory.split("/")[0],
                *other_folders,
            }
            for item in agent_path.iterdir():
                if item.name in already_copied:
                    continue
                logging.debug("3c. Overlaying agent-owned %s", item.name)
                copy_files(item, project_template / item.name, agent_name, overwrite=True)

            # 4. cookiecutter.json
            cookiecutter_config = build_cookiecutter_context(
                project_name=project_name,
                agent_name=agent_name,
                deployment_target=deployment_target,
                runtime=runtime,
                model_provider=model_provider,
                model=model,
                checkpointer=checkpointer,
                registry=registry,
                cd=cd,
                auth_policy=auth_policy,
                has_api_policy=has_api_policy,
                apis=apis,
                process=process,
                agent_guidance_filename=agent_guidance_filename,
                agent_directory=agent_directory,
                template_config=template_config,
                recorded_base_template=recorded_base_template,
                auth_policy_implemented=auth_policy_implemented,
            )
            with open(
                cookiecutter_template / "cookiecutter.json", "w", encoding="utf-8"
            ) as json_file:
                json.dump(cookiecutter_config, json_file, indent=4)

            logging.debug("Template structure created at %s", cookiecutter_template)

            cookiecutter(
                str(cookiecutter_template),
                no_input=True,
                overwrite_if_exists=True,
                extra_context={"project_name": project_name, "agent_name": agent_name},
            )
            logging.debug("Template processing completed successfully")

            generated_project_dir = temp_path / project_name

            # 5. Remote overlay (after cookiecutter, so its files are never rendered)
            if is_remote and remote_template_path:
                logging.debug(
                    "Copying remote template files from %s to %s",
                    remote_template_path,
                    generated_project_dir,
                )
                cli_agent_dir = (
                    cli_overrides.get("settings", {}).get("agent_directory")
                    if cli_overrides
                    else None
                )
                is_flat_structure = (cli_agent_dir == ".") or bool(
                    remote_config and remote_config.get("is_flat_structure", False)
                )
                if is_flat_structure:
                    copy_flat_structure_agent_files(
                        remote_template_path, generated_project_dir, agent_directory
                    )
                else:
                    copy_files(
                        remote_template_path,
                        generated_project_dir,
                        agent_name=agent_name,
                        overwrite=True,
                        skip_manifest=not overlay_is_project,
                        guidance_filename=agent_guidance_filename,
                    )
                logging.debug("Remote template files copied successfully")

            # 6. Move to the final destination
            if in_folder:
                final_destination = destination_dir
                if generated_project_dir.exists():
                    for item in generated_project_dir.iterdir():
                        dest_item = final_destination / item.name
                        if item.is_dir():
                            if dest_item.exists():
                                shutil.rmtree(dest_item)
                            shutil.copytree(item, dest_item, dirs_exist_ok=True)
                        else:
                            shutil.copy2(item, dest_item)
                    logging.debug("Project files copied to %s", final_destination)
            else:
                final_destination = destination_dir / project_name
                if generated_project_dir.exists():
                    if final_destination.exists():
                        shutil.rmtree(final_destination)
                    shutil.copytree(generated_project_dir, final_destination, dirs_exist_ok=True)
                    logging.debug("Project created at %s", final_destination)

            if not final_destination.exists():
                raise FileNotFoundError(
                    f"Final destination directory not found at {final_destination}"
                )

            # 7. Conditional files, then runtime-specific Dockerfile and lock
            conditional_config = {
                "agent_name": agent_name,
                "deployment_target": deployment_target,
                "runtime": runtime,
                "cd": cd,
                "has_api_policy": has_api_policy,
            }
            apply_conditional_files(final_destination, conditional_config, agent_directory)
            _remove_unused_paths(final_destination)
            select_runtime_files(final_destination, runtime, project_name)

        except Exception as e:
            logging.error(f"Failed to process template: {e!s}")
            raise
        finally:
            os.chdir(original_dir)

    return final_destination


# =============================================================================
# File copying
# =============================================================================

_TOOL_CACHES = frozenset({"__pycache__", ".ruff_cache", ".pytest_cache", ".mypy_cache", ".venv"})


def copy_files(
    src: pathlib.Path,
    dst: pathlib.Path,
    agent_name: str | None = None,
    overwrite: bool = False,
    *,
    skip_manifest: bool = False,
    guidance_filename: str | None = None,
) -> None:
    """Copy files with configurable behavior for exclusions and overwrites.

    Args:
        src: Source path
        dst: Destination path
        agent_name: Name of the agent (kept for call compatibility)
        overwrite: Whether to overwrite existing files (True) or skip them (False)
        guidance_filename: Write a root AGENTS.md under this name instead, so a
            template's guide replaces the base one whatever the project calls it.
        skip_manifest: Skip a root graph-agents-cli-manifest.yaml. Set when copying a
            fetched template, whose manifest describes the template rather than
            the project and has already been read for config.
    """

    def should_skip(path: pathlib.Path) -> bool:
        """Symlinks are never followed (CWE-59): a template could point one at
        a sensitive host file and the project would end up with its contents."""
        if path.is_symlink():
            logging.warning(
                f"Skipping symlink in template source (symlinks are not allowed): {path}"
            )
            return True
        if path.suffix in [".pyc"]:
            return True
        if "__pycache__" in str(path) or path.name in _TOOL_CACHES:
            return True
        if ".git" in path.parts:
            return True
        if path.is_dir() and path.name == ".template":
            return True
        if skip_manifest and path.name == MANIFEST_FILENAME:
            return True
        return False

    def log_windows_path_warning(path: pathlib.Path) -> None:
        if sys.platform == "win32":
            path_str = str(path.absolute())
            if len(path_str) >= 260:
                logging.error(
                    f"Path length ({len(path_str)} chars) may exceed Windows limit. "
                    "Try using a shorter output directory."
                )

    if src.is_dir():
        if not dst.exists():
            try:
                dst.mkdir(parents=True)
                logging.debug("Created directory: %s", dst)
            except OSError as e:
                logging.error(f"Failed to create directory: {dst}")
                logging.error(f"Error: {e}")
                raise
        for item in src.iterdir():
            if should_skip(item):
                logging.debug("Skipping file/directory: %s", item)
                continue

            # Root only: nested AGENTS.md files are the template's own docs.
            if guidance_filename and item.is_file() and item.name == "AGENTS.md":
                d = dst / guidance_filename
            else:
                d = dst / item.name
            if item.is_dir():
                copy_files(item, d, agent_name, overwrite)
            else:
                if overwrite or not d.exists():
                    try:
                        d.parent.mkdir(parents=True, exist_ok=True)
                        logging.debug("Copying file: %s -> %s", item, d)
                        shutil.copy2(item, d)
                    except OSError:
                        logging.error(f"Failed to copy: {item} -> {d}")
                        log_windows_path_warning(d)
                        raise
                else:
                    logging.debug("Skipping existing file: %s", d)
    else:
        if not should_skip(src):
            if overwrite or not dst.exists():
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    logging.debug("Copying file: %s -> %s", src, dst)
                    shutil.copy2(src, dst)
                except OSError:
                    logging.error(f"Failed to copy: {src} -> {dst}")
                    log_windows_path_warning(dst)
                    raise


def _assert_path_within(candidate: pathlib.Path, root: pathlib.Path) -> None:
    """Raise ValueError if *candidate* is not contained within *root*.

    Both paths are resolved to their real absolute forms before the check so
    that symbolic links and ``..`` components cannot bypass the boundary.
    """
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError as e:
        raise ValueError(
            f"Security check failed: '{candidate}' would be written "
            f"outside the project directory '{root}'. "
            "Aborting to prevent path-traversal exploitation."
        ) from e


def _skip_symlinks(directory: str, names: list[str]) -> set[str]:
    """Ignore callback for shutil.copytree to skip symlinks at every nesting level."""
    return {n for n in names if pathlib.Path(directory, n).is_symlink()}


def copy_flat_structure_agent_files(
    src: pathlib.Path,
    dst: pathlib.Path,
    agent_directory: str,
) -> None:
    """Copy agent files from a flat structure template to the agent directory.

    Python files (*.py) in the template root go to the agent directory; other
    files go to the project root. Symlinks are never followed and every
    destination is verified to be inside *dst* before any write.
    """
    agent_dst = dst / agent_directory
    _assert_path_within(agent_dst, dst)
    agent_dst.mkdir(parents=True, exist_ok=True)

    agent_file_extensions = {".py"}
    skip_files = {"pyproject.toml", "uv.lock", "README.md", ".gitignore"}

    for item in src.iterdir():
        if item.name.startswith(".") or item.name in skip_files:
            continue
        if item.name == "__pycache__":
            continue
        if item.is_symlink():
            logging.warning(
                f"Skipping symlink in flat-structure template source "
                f"(symlinks are not allowed): {item}"
            )
            continue

        if item.is_file():
            if item.suffix in agent_file_extensions:
                dest_file = agent_dst / item.name
                _assert_path_within(dest_file, dst)
                logging.debug(
                    "Flat structure: copying %s -> %s/%s", item.name, agent_directory, item.name
                )
                shutil.copy2(item, dest_file)
            else:
                dest_file = dst / item.name
                _assert_path_within(dest_file, dst)
                logging.debug("Flat structure: copying %s -> %s", item.name, item.name)
                shutil.copy2(item, dest_file)
        elif item.is_dir():
            dest_dir = dst / item.name
            _assert_path_within(dest_dir, dst)
            logging.debug("Flat structure: copying directory %s", item.name)
            if dest_dir.exists():
                shutil.rmtree(dest_dir)
            shutil.copytree(item, dest_dir, ignore=_skip_symlinks)
