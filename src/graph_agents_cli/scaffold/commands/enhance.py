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

"""``scaffold enhance``: add or change the deployment target, CD mode, or runtime."""

import dataclasses
import logging
import os
import pathlib
import shlex
import subprocess
import sys
from typing import Any

import click
from packaging import version as pkg_version
from rich.prompt import IntPrompt, Prompt

from graph_agents_cli import _api_policy
from graph_agents_cli._defaults import (
    DEFAULT_AGENT_GUIDANCE_FILENAME,
    DEFAULT_MODELS,
    DEFAULT_REGISTRY_HOST,
    DEFAULT_REGISTRY_PLACEHOLDER,
    PROVIDER_KEY_VARS,
    default_secret_keys,
    normalize_auth_policy,
)
from graph_agents_cli._output import Console
from graph_agents_cli._project import (
    API_POLICY_FILENAME,
    NotInProjectError,
    ProjectConfig,
    find_project_config,
    find_project_root,
)
from graph_agents_cli._runner import run_resolved
from graph_agents_cli._tools import ToolNotFoundError, require_tool

from ..utils import build_record, remote_template
from ..utils.backup import make_backup_pre_apply_hook
from ..utils.cli_options import shared_template_options
from ..utils.generation_metadata import metadata_to_cli_args
from ..utils.language import (
    find_agent_file,
    get_agent_file_hint,
    get_language_config,
    validate_agent_file,
)
from ..utils.logging import display_welcome_banner
from ..utils.manifest import (
    CreateParams,
    finalize_manifest,
    recompute_secret_keys,
    reconcile_secret_keys,
)
from ..utils.merge import (
    run_three_way_merge,
)
from ..utils.template import (
    default_checkpointer,
    get_available_agents,
    get_available_base_templates,
    get_deployment_targets,
    prompt_cd,
    prompt_checkpointer,
    prompt_deployment_target,
    prompt_runtime,
    validate_agent_directory_name,
    validate_base_template,
    validate_combination,
)
from ..utils.upgrade import (
    Followup,
    update_cli_metadata,
)
from ..utils.version import (
    get_current_version,
    pinned_install_spec,
    pinned_spec_unavailable,
)
from .create import _git_origin_owner, create

console = Console()

# Environment variable names for saved config handling
_ENV_USING_SAVED_CONFIG = "_GRAPH_AGENTS_CLI_USING_SAVED_CONFIG"
_ENV_SKIP_VERSION_LOCK = "GRAPH_AGENTS_CLI_SKIP_VERSION_LOCK"

# Directories to exclude when scanning for agent directories
_EXCLUDED_DIRS = {
    ".git",
    ".github",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "build",
    "dist",
    "deployment",
    "tests",
}

# create_params keys an enhance override may change (all strings on the CLI).
_CREATE_PARAM_KEYS = (
    "deployment_target",
    "runtime",
    "model_provider",
    "model",
    "checkpointer",
    "registry",
    "cd",
    "auth_policy",
)


def model_after_provider_change(
    project_config: ProjectConfig, new_provider: str, explicit_model: str | None = None
) -> str:
    """The model to record when ``--model-provider`` may change the provider.

    An explicit ``--model`` wins. With the provider unchanged the recorded model
    stays. When the provider changes and the recorded model is the old
    provider's default (or unset), the new provider's default replaces it, as
    ``create`` would pick. A model the developer chose for the old provider
    cannot be mapped: that is a usage error (exit 2) asking for ``--model``,
    because keeping it would deploy one provider with another provider's model.
    """
    if explicit_model:
        return str(explicit_model)
    old_provider = project_config.model_provider
    recorded = project_config.model
    if new_provider == old_provider:
        return recorded
    new_default = DEFAULT_MODELS.get(new_provider, "")
    if recorded in ("", DEFAULT_MODELS.get(old_provider, "")):
        return new_default
    raise click.UsageError(
        f"--model-provider {new_provider} changes the provider, but the recorded model "
        f"{recorded!r} was chosen for {old_provider} and cannot be carried over.\n"
        f"  Pass --model <name> as well (the {new_provider} default is {new_default!r})."
    )


def build_args_from_config(
    project_config: ProjectConfig,
    auto_approve: bool = False,
    cli_overrides: dict[str, Any] | None = None,
    cli_version: str | None = None,
) -> list[str]:
    """Build the ``scaffold enhance`` argv that replays the saved config plus overrides.

    ``cli_version`` is the CLI that will run the argv when it is not this one.
    """
    # --skip-deps: dependencies were installed on first run; --skip-welcome: one banner
    args = ["scaffold", "enhance", "--skip-deps", "--skip-welcome"]

    if auto_approve:
        args.append("--auto-approve")

    args.extend(metadata_to_cli_args(project_config, for_enhance=True, cli_version=cli_version))

    if cli_overrides:
        for arg_name, value in cli_overrides.items():
            cli_arg = f"--{arg_name.replace('_', '-')}"
            _drop_flag(args, cli_arg)
            if value is True:
                args.append(cli_arg)
            elif value is not False and value is not None:
                args.extend([cli_arg, str(value)])

        new_provider = cli_overrides.get("model_provider")
        if new_provider and not cli_overrides.get("model"):
            # The replayed --model belongs to the recorded provider.
            _drop_flag(args, "--model")
            model = model_after_provider_change(project_config, str(new_provider))
            if model:
                args.extend(["--model", model])

        new_target = cli_overrides.get("deployment_target")
        if new_target and new_target != project_config.deployment_target:
            # A target change re-derives the checkpointer, cd and registry
            # unless the caller pinned them; replaying the saved values would
            # fail the combination table in the subprocess (memory on kubernetes).
            for name in ("checkpointer", "cd", "registry"):
                if name not in cli_overrides:
                    _drop_flag(args, f"--{name}")

    return args


def _drop_flag(args: list[str], flag: str) -> None:
    """Remove every ``flag`` (and its value, when it has one) from ``args`` in place."""
    i = 0
    while i < len(args):
        if args[i] == flag:
            args.pop(i)
            if i < len(args) and not args[i].startswith("--"):
                args.pop(i)
        else:
            i += 1


def get_display_params_from_config(project_config: ProjectConfig) -> dict[str, Any]:
    """Display-worthy parameters from the saved manifest."""
    display_params: dict[str, Any] = {}
    if project_config.base_template:
        display_params["base_template"] = project_config.base_template
    if project_config.agent_directory:
        display_params["agent_directory"] = project_config.agent_directory
    if project_config.cli_version:
        display_params["cli_version"] = project_config.cli_version
    for key, value in project_config.create_params.items():
        if value is None or value == "":
            continue
        display_params[key] = value
    return display_params


def _display_saved_config(
    display_params: dict[str, Any],
    project_version: str | None,
    current_version: str,
    use_different_version: bool,
) -> None:
    """Display detected saved configuration to the user."""
    console.print()
    console.print("📋 [bold]Detected saved configuration from previous setup:[/bold]")
    console.print()
    for key, value in display_params.items():
        display_key = key.replace("_", " ").title()
        console.print(f"   • {display_key}: [cyan]{value}[/cyan]")

    if use_different_version and project_version:
        console.print()
        console.print(f"   • Version: [cyan]{project_version}[/cyan] (current: {current_version})")
    console.print()


def _should_use_different_version(project_version: str | None, current_version: str) -> bool:
    """Determine if we need to switch to a different CLI version."""
    skip_version_lock = os.environ.get(_ENV_SKIP_VERSION_LOCK) == "1"
    return (
        not skip_version_lock
        and bool(project_version)
        and current_version != "0.0.0"
        and project_version != current_version
    )


def _ensure_uvx_available(project_version: str) -> None:
    """Ensure uvx is installed, exit with instructions if not."""
    try:
        require_tool("uvx")
    except ToolNotFoundError:
        console.print(
            f"❌ Project requires graph-agents-cli version {project_version}, "
            "but 'uvx' is not installed",
            style="bold red",
        )
        console.print("💡 Install uv to use version-locked projects:", style="bold blue")
        console.print("   curl -LsSf https://astral.sh/uv/install.sh | sh")
        console.print("   OR visit: https://docs.astral.sh/uv/getting-started/installation/")
        sys.exit(2)  # a missing tool, like every other command


def _execute_with_saved_config(
    args: list[str], project_version: str | None, use_different_version: bool
) -> bool:
    """Execute enhance with saved config args (in a subprocess); True on success."""
    if use_different_version and project_version:
        spec = pinned_install_spec(project_version)
        if spec is None:
            # Running the override would pass off another build as project_version.
            console.print(f"⚠️  {pinned_spec_unavailable(project_version)}.", style="yellow")
            console.print(
                "⚠️  Continuing with current version, but compatibility is not guaranteed",
                style="yellow",
            )
            return False
        console.print(f"📦 Using graph-agents-cli version {project_version}...", style="dim")
        _ensure_uvx_available(project_version)
        cmd = ["uvx", "--from", spec, "graph-agents-cli", *args]
    else:
        console.print("✅ Using saved configuration", style="dim")
        cmd = [sys.executable, "-m", "graph_agents_cli.main", *args]

    logging.debug("Executing command: %s", shlex.join(cmd))

    env = os.environ.copy()
    env[_ENV_USING_SAVED_CONFIG] = "1"

    try:
        run_resolved(cmd, check=True, env=env)
        return True
    except subprocess.CalledProcessError as e:
        if not use_different_version:
            # This CLI already ran the saved configuration and said why it
            # stopped. Falling back would only repeat the same enhance in process
            # (after the manifest was rewritten, so it could even report
            # success): keep its exit code, e.g. 1 when required items are left
            # (128 + N when a signal N ended it).
            code = e.returncode if e.returncode > 0 else 128 - e.returncode
            raise click.exceptions.Exit(code if e.returncode else 1) from e
        console.print(
            f"❌ Failed to execute with locked version {project_version}: {e}",
            style="bold red",
        )
        console.print(
            "⚠️  Continuing with current version, but compatibility is not guaranteed",
            style="yellow",
        )
        return False


def check_and_execute_with_saved_config(
    *,
    project_dir: pathlib.Path,
    auto_approve: bool = False,
    cli_overrides: dict[str, Any] | None = None,
    force: bool = False,
    dry_run: bool = False,
    interactive: bool = False,
) -> bool | dict[str, Any]:
    """Check for saved config and offer to reuse it.

    Returns:
        True if config was used and executed successfully.
        False if no saved config found or execution failed.
        dict if the user customized interactively: only the changed parameters.
    """
    if os.environ.get(_ENV_USING_SAVED_CONFIG) == "1":
        return False

    project_config = find_project_config(project_dir)
    if not project_config:
        return False

    display_params = get_display_params_from_config(project_config)
    if not display_params:
        return False

    current_version = get_current_version()
    project_version = project_config.cli_version
    use_different_version = _should_use_different_version(project_version, current_version)

    _display_saved_config(display_params, project_version, current_version, use_different_version)

    if interactive:
        return _prompt_customize_overrides(project_config)

    args = build_args_from_config(
        project_config,
        auto_approve,
        cli_overrides,
        cli_version=project_version if use_different_version else None,
    )
    is_older_version = (
        use_different_version
        and bool(project_version)
        and pkg_version.parse(project_version) < pkg_version.parse(current_version)
    )
    if not is_older_version:
        if force:
            args.append("--force")
        if dry_run:
            args.append("--dry-run")
    return _execute_with_saved_config(args, project_version, use_different_version)


def _prompt_customize_overrides(project_config: ProjectConfig) -> dict[str, Any]:
    """Prompt for the parameters enhance can change; return only the changed ones."""
    overrides: dict[str, Any] = {}

    base_template = project_config.base_template
    new_agent = display_base_template_selection(base_template)
    if new_agent != base_template:
        overrides["base_template"] = new_agent
    effective_agent = new_agent

    available_targets = get_deployment_targets(effective_agent)
    current_deployment = project_config.deployment_target
    if available_targets and len(available_targets) > 1:
        new_deployment = prompt_deployment_target(effective_agent, default_value=current_deployment)
    elif available_targets:
        new_deployment = available_targets[0]
    else:
        new_deployment = current_deployment
    if new_deployment != current_deployment:
        overrides["deployment_target"] = new_deployment

    current_runtime = project_config.runtime
    new_runtime = prompt_runtime(current_runtime)
    if new_runtime != current_runtime:
        overrides["runtime"] = new_runtime

    current_checkpointer = project_config.checkpointer
    suggested = (
        current_checkpointer
        if new_deployment == current_deployment
        else default_checkpointer(new_deployment)
    )
    new_checkpointer = prompt_checkpointer(suggested)
    if new_checkpointer != current_checkpointer:
        overrides["checkpointer"] = new_checkpointer

    current_cd = project_config.cd
    if new_deployment == "kubernetes":
        new_cd = prompt_cd(current_cd)
    else:
        new_cd = "skip"
    if new_cd != current_cd:
        overrides["cd"] = new_cd

    console.print()
    return overrides


def display_base_template_selection(current_base: str) -> str:
    """Display available base templates and prompt for selection."""
    agents = get_available_agents()

    if not agents:
        raise click.ClickException("No base templates available")

    console.print()
    console.print("🔧 [bold]Base Template Selection[/bold]")
    console.print()
    console.print(f"Your project currently inherits from: [cyan]{current_base}[/cyan]")
    console.print("Available base templates:")

    template_choices = {}
    choice_num = 1
    current_choice = None

    for agent in agents.values():
        template_choices[choice_num] = agent["name"]
        if agent["name"] == current_base:
            console.print(
                f"  {choice_num}. [bold cyan]{agent['name']}[/]"
                f" [dim]{agent['description']}[/]"
                "  [dim cyan](current)[/]"
            )
            current_choice = choice_num
        else:
            console.print(f"  [dim]{choice_num}. {agent['name']} - {agent['description']}[/]")
        choice_num += 1

    if current_choice is None:
        current_choice = 1

    console.print()
    choice = IntPrompt.ask("Select base template", default=current_choice, show_default=True)

    if choice in template_choices:
        return template_choices[choice]
    raise ValueError(f"Invalid base template selection: {choice}")


def display_agent_directory_selection(
    current_dir: pathlib.Path,
    detected_directory: str,
) -> str:
    """Display available directories and prompt for agent directory selection."""
    agent_file_hint = get_language_config("python")["agent_file"]
    while True:
        console.print()
        console.print("📁 [bold]Agent Directory Selection[/bold]")
        console.print()
        console.print("Your project needs an agent directory containing:")
        console.print(f"  • [cyan]{agent_file_hint}[/cyan] exporting the compiled graph as `graph`")
        console.print()
        console.print("Choose where your agent code is located:")

        available_dirs = sorted(
            item.name
            for item in current_dir.iterdir()
            if item.is_dir() and not item.name.startswith(".") and item.name not in _EXCLUDED_DIRS
        )

        directory_choices = {}
        choice_num = 1
        default_choice = None

        if detected_directory in available_dirs:
            directory_choices[choice_num] = detected_directory
            current_indicator = " (detected)" if detected_directory != "app" else " (default)"
            console.print(f"  {choice_num}. [bold]{detected_directory}[/]{current_indicator}")
            default_choice = choice_num
            choice_num += 1
            available_dirs.remove(detected_directory)

        for dir_name in available_dirs:
            directory_choices[choice_num] = dir_name
            hint = get_agent_file_hint(current_dir / dir_name)
            console.print(f"  {choice_num}. [bold]{dir_name}[/]{hint}")
            if default_choice is None:
                default_choice = choice_num
            choice_num += 1

        custom_choice = choice_num
        directory_choices[custom_choice] = "__custom__"
        console.print(f"  {custom_choice}. [bold]Enter custom directory name[/]")

        if default_choice is None:
            default_choice = custom_choice

        console.print()
        choice = IntPrompt.ask("Select agent directory", default=default_choice, show_default=True)

        if choice not in directory_choices:
            console.print(f"[bold red]Error:[/] Invalid selection: {choice}", style="bold red")
            console.print("Please choose a valid option from the list.")
            console.print()
            continue

        selected = directory_choices[choice]
        if selected == "__custom__":
            console.print()
            while True:
                custom_dir = Prompt.ask(
                    "Enter custom agent directory name", default=detected_directory
                )
                try:
                    validate_agent_directory_name(custom_dir)
                    return custom_dir
                except ValueError as e:
                    console.print(f"[bold red]Error:[/] {e}", style="bold red")
                    console.print("Please try again with a valid directory name.")
        try:
            validate_agent_directory_name(selected)
            return selected
        except ValueError as e:
            console.print(f"[bold red]Error:[/] {e}", style="bold red")
            console.print(
                "This directory cannot be used as an agent directory. Please select another option."
            )
            console.print()


def _build_enhance_create_args(
    project_config: ProjectConfig,
    cli_overrides: dict[str, Any] | None = None,
    project_dir: pathlib.Path | None = None,
) -> list[str]:
    """Build ``create`` args for the enhanced snapshot from the effective parameters.

    Every create parameter comes from ``_effective_params`` (the values the
    combination check validated), so a target change renders with the
    checkpointer, cd and registry it re-derives instead of replaying the saved ones.
    """
    overrides = cli_overrides or {}
    params = _effective_params(project_config, cli_overrides, project_dir)
    args: list[str] = []

    base_template = overrides.get("base_template") or project_config.base_template
    if base_template:
        args.extend(["--agent", str(base_template)])
    agent_directory = overrides.get("agent_directory") or project_config.agent_directory
    if agent_directory and agent_directory != "app":
        args.extend(["--agent-directory", str(agent_directory)])
    guidance = overrides.get("agent_guidance_filename") or project_config.agent_guidance_filename
    args.extend(["--agent-guidance-filename", str(guidance)])

    args.extend(["--deployment-target", params.deployment_target])
    args.extend(["--runtime", params.runtime])
    args.extend(["--model-provider", params.model_provider])
    if params.model:
        args.extend(["--model", params.model])
    args.extend(["--checkpointer", params.checkpointer])
    if params.registry:
        args.extend(["--registry", params.registry])
    args.extend(["--cd", params.cd])
    args.extend(["--auth-policy", params.auth_policy])
    if params.process:
        args.extend(["--process", params.process])
    if overrides.get("prototype"):
        args.append("--prototype")
    return args


def _effective_params(
    project_config: ProjectConfig,
    cli_overrides: dict[str, Any] | None,
    project_dir: pathlib.Path | None = None,
) -> CreateParams:
    """The create parameters after applying ``cli_overrides`` to the saved config.

    A target change re-derives the checkpointer (postgres for kubernetes,
    memory for none) and the registry unless overridden. An explicit ``--cd`` is kept as given so
    ``validate_combination`` enforces the kubernetes rule the way ``create``
    does (``--prototype`` forces ``skip``, also like ``create``); only the saved
    cd is forced to ``skip`` when the target leaves kubernetes. The recorded
    ``auth_policy_implemented`` is carried through while the policy is unchanged.
    """
    overrides = cli_overrides or {}
    target = str(overrides.get("deployment_target", project_config.deployment_target))
    runtime = str(overrides.get("runtime", project_config.runtime))
    checkpointer = overrides.get("checkpointer")
    if not checkpointer:
        checkpointer = (
            project_config.checkpointer
            if target == project_config.deployment_target
            else default_checkpointer(target)
        )
    explicit_cd = overrides.get("cd")
    if overrides.get("prototype"):
        cd = "skip"
    elif explicit_cd:
        cd = str(explicit_cd)
    elif target == "kubernetes":
        cd = str(project_config.cd)
    else:
        cd = "skip"
    if target == "kubernetes":
        registry = str(overrides.get("registry") or project_config.registry or "")
        if not registry:
            registry = _default_registry(project_dir)
    else:
        registry = ""
    auth_policy = str(overrides.get("auth_policy", project_config.auth_policy))
    implemented = (
        project_config.auth_policy_implemented
        if auth_policy == project_config.auth_policy
        else None
    )
    has_policy = bool(project_config.api_policy_file)
    apis: tuple[_api_policy.ApiSummary, ...] = ()
    if has_policy and project_dir is not None:
        apis = _api_policy.read_summaries(project_dir / str(project_config.api_policy_file))
    process = overrides.get("process", project_config.process)
    model_provider = str(overrides.get("model_provider", project_config.model_provider))
    return CreateParams(
        deployment_target=target,
        runtime=runtime,
        model_provider=model_provider,
        model=model_after_provider_change(project_config, model_provider, overrides.get("model")),
        checkpointer=str(checkpointer),
        registry=registry,
        cd=cd,
        auth_policy=auth_policy,
        has_api_policy=has_policy,
        process=str(process) if process else None,
        auth_policy_implemented=implemented,
        apis=apis,
    )


def _default_registry(project_dir: pathlib.Path | None) -> str:
    """The registry ``create`` would pick for a project that gains the kubernetes target."""
    owner = _git_origin_owner(project_dir) if project_dir is not None else None
    return f"{DEFAULT_REGISTRY_HOST}/{owner}" if owner else DEFAULT_REGISTRY_PLACEHOLDER


def _validate_effective_params(
    params: CreateParams, project_dir: pathlib.Path | None = None
) -> None:
    """Refuse (usage error, exit 2) what ``create`` would refuse for the same parameters.

    That includes the project's own ``api-policy.yaml``: an ``auth: forward``
    API cannot move to the langgraph-server runtime, which persists the run
    context and so would store the forwarded credentials.
    """
    try:
        validate_combination(
            params.runtime, params.checkpointer, params.deployment_target, params.cd
        )
    except ValueError as e:
        raise click.UsageError(str(e)) from e
    apis = params.apis
    if not apis and project_dir is not None:
        apis = _api_policy.read_summaries(project_dir / API_POLICY_FILENAME)
    problem = _api_policy.forward_runtime_problem(apis, params.runtime)
    if problem:
        raise click.UsageError(f"{problem} (in {API_POLICY_FILENAME})")


def _backfill_create_params_from_config(
    current_dir: pathlib.Path,
    cli_params: dict[str, Any],
) -> dict[str, Any]:
    """Fill None CLI create params from the saved manifest; CLI values always win.

    When the CLI changes the deployment target, the saved checkpointer, cd and
    registry are not carried over: the checkpointer follows the new target's
    default and ``create`` derives cd and registry.
    """
    config = find_project_config(current_dir)
    if not config:
        return cli_params

    saved = config.create_params
    if not saved:
        return cli_params

    result = cli_params.copy()
    new_target = result.get("deployment_target")
    target_changed = bool(new_target) and new_target != saved.get("deployment_target")
    for key in result:
        if result[key] is not None:
            continue
        if target_changed and key in ("checkpointer", "cd", "registry"):
            if key == "checkpointer":
                result[key] = default_checkpointer(str(new_target))
            continue
        if key in saved and saved[key] not in (None, ""):
            result[key] = saved[key]
    if result.get("auth_policy"):
        # A manifest may still record a retired policy name.
        result["auth_policy"] = normalize_auth_policy(str(result["auth_policy"]))
    if cli_params.get("model_provider") and not cli_params.get("model"):
        # The saved model belongs to the saved provider.
        result["model"] = model_after_provider_change(config, str(cli_params["model_provider"]))
    return result


def _run_smart_merge(
    *,
    project_dir: pathlib.Path,
    project_config: ProjectConfig,
    cli_overrides: dict[str, Any] | None,
    auto_approve: bool,
    dry_run: bool,
    prefer_new: bool = False,
    interactive: bool = False,
) -> bool:
    """Smart-merge: 3-way comparison between the recorded render and the enhanced one.

    Returns True if smart-merge completed successfully, False otherwise.
    """
    project_name = project_config.project_name or project_dir.name
    agent_directory = project_config.agent_directory
    language = project_config.language

    params = _effective_params(project_config, cli_overrides, project_dir)
    _validate_effective_params(params, project_dir)
    previous = _recorded_params(project_config, project_dir)
    _print_recomputed_settings(project_config, previous, params)

    old_args = metadata_to_cli_args(project_config)
    new_args = _build_enhance_create_args(project_config, cli_overrides, project_dir)

    backup_hook = make_backup_pre_apply_hook(
        console=console,
        auto_approve=auto_approve,
        interactive=interactive,
    )

    current_build = build_record.running_build()
    try:
        recorded_build = build_record.recorded_build_for(project_config)
    except build_record.MalformedRecordError as e:
        console.print(f"[yellow]⚠️  {e}; it is ignored.[/yellow]")
        recorded_build = None
    digests: dict[str, str] = {}

    def _check_snapshots(old_dir: pathlib.Path, new_dir: pathlib.Path) -> None:
        # Both snapshots are this build's. The project is at this build's
        # templates when it was rendered by this very build, or by one that
        # renders its settings identically; otherwise files the template changed
        # since its build look like the developer's edits here.
        digests["old"] = build_record.template_digest(old_dir)
        digests["new"] = build_record.template_digest(new_dir)
        if recorded_build is not None and not _at_this_build(recorded_build, digests["old"]):
            console.print(
                f"[yellow]⚠️  This project was rendered by build {recorded_build.id}, whose "
                f"templates differ from this build's ({current_build.id}): enhance compares "
                "your files with this build's templates, so a file the template changed since "
                "then counts as your edit (kept, or listed as a conflict). Run "
                "`graph-agents-cli scaffold upgrade` to bring those changes in.[/yellow]"
            )

    def _at_this_build(record: build_record.BuildRecord, old_digest: str) -> bool:
        if record.same_build_as(current_build):
            return True
        return record.template_digest is not None and record.template_digest == old_digest

    def _record_build(proj_dir: pathlib.Path) -> None:
        """The new settings' snapshot is this build's; the project is at it only if it was before."""
        if recorded_build is None or "new" not in digests:
            return  # a manifest without cli_build stays compared by version only
        if _at_this_build(recorded_build, digests["old"]):
            record = build_record.BuildRecord.of(current_build, digests["new"])
        else:
            # Still the recorded build's files; its digest described the old settings.
            record = build_record.BuildRecord(recorded_build.id, recorded_build.commit, None)
        build_record.write_build_record(proj_dir, record)

    def _update_metadata(proj_dir: pathlib.Path, lang: str) -> list[str | Followup] | None:
        if not cli_overrides:
            return None
        has_policy = params.has_api_policy or (proj_dir / API_POLICY_FILENAME).is_file()
        apis = _api_policy.read_summaries(proj_dir / API_POLICY_FILENAME) if has_policy else ()
        current = dataclasses.replace(params, has_api_policy=has_policy, apis=apis)
        finalize_manifest(
            proj_dir,
            project_name=project_name,
            # dataclasses.replace keeps auth_policy_implemented and every other
            # field of the validated params; only the policy facts are refreshed.
            params=current,
            cli_version=get_current_version(),
        )
        added, removed = reconcile_secret_keys(proj_dir, previous=previous, current=current)
        extra: dict[str, Any] = {}
        if cli_overrides.get("agent_guidance_filename"):
            extra["agent_guidance_filename"] = cli_overrides["agent_guidance_filename"]
        if extra:
            update_cli_metadata(proj_dir, extra)
        _record_build(proj_dir)
        return [
            *_settings_followups(proj_dir, previous, current, added, removed),
            *_chart_followups(proj_dir, previous, current),
        ]

    return run_three_way_merge(
        project_dir=project_dir,
        project_name=project_name,
        agent_directory=agent_directory,
        language=language,
        old_args=old_args,
        new_args=new_args,
        auto_approve=auto_approve,
        dry_run=dry_run,
        prefer_new=prefer_new,
        interactive=interactive,
        operation_label="enhancement",
        pre_apply_hook=backup_hook,
        post_apply_hook=_update_metadata,
        merge_config=True,
        snapshot_check=_check_snapshots,
    )


def _recorded_params(project_config: ProjectConfig, project_dir: pathlib.Path) -> CreateParams:
    """The create parameters the manifest records (before an enhance changes them)."""
    apis: tuple[_api_policy.ApiSummary, ...] = ()
    if project_config.api_policy_file:
        apis = _api_policy.read_summaries(project_dir / str(project_config.api_policy_file))
    return CreateParams(
        deployment_target=project_config.deployment_target,
        runtime=project_config.runtime,
        model_provider=project_config.model_provider,
        model=project_config.model,
        checkpointer=project_config.checkpointer,
        registry=project_config.registry,
        cd=project_config.cd,
        auth_policy=project_config.auth_policy,
        has_api_policy=bool(project_config.api_policy_file),
        apis=apis,
    )


def _print_recomputed_settings(
    project_config: ProjectConfig, previous: CreateParams, params: CreateParams
) -> None:
    """Say which recorded settings the change recomputes (before anything is written)."""
    lines: list[str] = []
    for label, old, new in (
        ("runtime", previous.runtime, params.runtime),
        ("model_provider", previous.model_provider, params.model_provider),
        ("model", previous.model, params.model),
    ):
        if old != new:
            lines.append(f"{label}: {old} -> {new}")
    old_defaults = default_secret_keys(
        previous.model_provider,
        previous.runtime,
        previous.api_token_envs,
        auth_policy=previous.auth_policy,
    )
    new_defaults = default_secret_keys(
        params.model_provider,
        params.runtime,
        params.api_token_envs,
        auth_policy=params.auth_policy,
    )
    recorded = project_config.secret_keys
    keys = recompute_secret_keys(recorded, old_defaults, new_defaults)
    added = [k for k in keys if k not in recorded]
    removed = [k for k in recorded if k not in keys]
    if added or removed:
        change = ", ".join([*(f"+{k}" for k in added), *(f"-{k}" for k in removed)])
        lines.append(f"secrets.keys: {change} (keys you added are kept)")
    if not lines:
        return
    console.print()
    console.print("[bold]Recomputed for the new settings (graph-agents-cli-manifest.yaml):[/bold]")
    for line in lines:
        console.print(f"  • {line}")


def _settings_followups(
    project_dir: pathlib.Path,
    previous: CreateParams,
    current: CreateParams,
    keys_added: list[str],
    keys_removed: list[str],
) -> list[str]:
    """What the developer still has to do by hand after a provider or runtime change.

    ``.env`` holds the developer's own values and is never edited by enhance,
    and the environments' Secrets live in the cluster: both are listed here.
    """
    items: list[str] = []
    env_values: dict[str, str | None] = {}
    env_file = project_dir / ".env"
    if env_file.is_file():
        from dotenv import dotenv_values

        try:
            env_values = dict(dotenv_values(env_file))
        except Exception:  # an unreadable .env is reported by run/eval themselves
            env_values = {}
    if previous.model_provider != current.model_provider or previous.model != current.model:
        stale = [
            f"{name}={env_values.get(name)}"
            for name, want in (
                ("MODEL_PROVIDER", current.model_provider),
                ("MODEL_NAME", current.model),
            )
            if env_values.get(name) not in (None, "", want)
        ]
        if stale:
            items.append(
                f".env still sets {' '.join(stale)}: change it to "
                f"MODEL_PROVIDER={current.model_provider} MODEL_NAME={current.model} "
                "(run, playground and eval read .env)"
            )
        key_var = PROVIDER_KEY_VARS.get(current.model_provider, "MODEL_API_KEY")
        if previous.model_provider != current.model_provider and not env_values.get(key_var):
            items.append(
                f"Set {key_var} in .env (and in each environment's env file, e.g. "
                f".env.staging, before `secrets apply`)"
            )
    if previous.runtime != current.runtime and current.deployment_target == "kubernetes":
        needs = (
            "DATABASE_URI and REDIS_URI"
            if current.runtime == "langgraph-server"
            else "POSTGRES_DSN"
        )
        items.append(
            f"The {current.runtime} runtime reads {needs} in staging and prod: add them to "
            "each environment's env file (the dev chart runs its bundled Postgres"
            + (" and Redis)" if current.runtime == "langgraph-server" else ")")
        )
    if (keys_added or keys_removed) and current.deployment_target == "kubernetes":
        change = ", ".join([*(f"+{k}" for k in keys_added), *(f"-{k}" for k in keys_removed)])
        items.append(
            f"secrets.keys changed ({change}): run `graph-agents-cli secrets apply --env <env>` "
            "for every environment you deploy to, so each Secret carries the new keys"
        )
    return items


_ABSENT = object()


def _values_lookup(data: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(data, dict) or key not in data:
            return _ABSENT
        data = data[key]
    return data


def _chart_followups(
    project_dir: pathlib.Path, previous: CreateParams, current: CreateParams
) -> list[Followup]:
    """Chart values that still disagree with a new runtime or provider (required items).

    Whatever the merge could or could not apply, this reads the chart values as
    Helm will (values.yaml, overlaid by each values-<env>.yaml; a null removes
    a key) and names every file that still sets a key the new settings decide:
    ``runtime`` (the chart wires DATABASE_URI/REDIS_URI or POSTGRES_DSN and
    LANGGRAPH_SERVER from it), ``env.CHECKPOINTER`` under fastapi, and
    ``env.MODEL_PROVIDER`` / ``env.MODEL_NAME`` / ``env.OPENAI_BASE_URL`` after a
    provider change. A deploy with any of them left would run the old settings.
    """
    import yaml

    if current.deployment_target != "kubernetes":
        return []
    checks: list[_ChartCheck] = []
    if previous.runtime != current.runtime:
        checks.append(
            _ChartCheck(
                ("runtime",),
                current.runtime,
                f"set it to {current.runtime!r}",
                "the chart wires the database variables and LANGGRAPH_SERVER from it",
                # The chart reads an absent runtime as fastapi.
                absent_ok=current.runtime == "fastapi",
            )
        )
        if current.runtime == "fastapi" and current.checkpointer == "postgres":
            checks.append(
                _ChartCheck(
                    ("env", "CHECKPOINTER"),
                    "postgres",
                    "set it to 'postgres'",
                    "without it the fastapi runtime keeps threads in memory",
                )
            )
    if previous.model_provider != current.model_provider:
        # Absent from the chart: set elsewhere (the Secret, extra env), not ours to judge.
        checks.append(
            _ChartCheck(
                ("env", "MODEL_PROVIDER"),
                current.model_provider,
                f"set it to {current.model_provider!r}",
                "the provider the pod calls",
                absent_ok=True,
            )
        )
        checks.append(
            _ChartCheck(
                ("env", "MODEL_NAME"),
                current.model,
                f"set it to {current.model!r} or another {current.model_provider} model",
                f"a model chosen for {previous.model_provider} does not run on "
                f"{current.model_provider}",
                absent_ok=True,
            )
        )
        if current.model_provider == "openai-compatible":
            checks.append(
                _ChartCheck(
                    ("env", "OPENAI_BASE_URL"),
                    None,
                    "set it to the endpoint's base URL",
                    "openai-compatible has no default endpoint",
                )
            )
    config = find_project_config(project_dir)
    secret_keys = set(config.secret_keys) if config else set()
    # A variable the Secret carries does not have to be in the chart values.
    checks = [
        check
        for check in checks
        if not (check.path[0] == "env" and check.path[-1] in secret_keys and check.wanted is None)
    ]
    if not checks:
        return []

    items: list[Followup] = []
    for values in sorted((project_dir / "deployment" / "helm").glob("*/values.yaml")):
        layers: list[tuple[pathlib.Path, Any]] = []
        for path in [values, *sorted(values.parent.glob("values-*.yaml"))]:
            try:
                layers.append((path, yaml.safe_load(path.read_text(encoding="utf-8")) or {}))
            except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
                items.append(
                    Followup(
                        f"{path.relative_to(project_dir).as_posix()}: cannot be read ({exc}); "
                        "check it by hand against the new settings",
                        required=True,
                    )
                )
        if not layers or layers[0][0] != values:
            continue
        # values.yaml alone, then values.yaml under each environment's file.
        stacks = [layers[:1], *([layers[0], layer] for layer in layers[1:])]
        reported: set[tuple[pathlib.Path, tuple[str, ...]]] = set()
        for check in checks:
            for stack in stacks:
                actual, source = _ABSENT, values
                for path, data in stack:
                    found = _values_lookup(data, check.path)
                    if found is not _ABSENT:
                        actual, source = (_ABSENT if found is None else found), path
                if actual is _ABSENT:
                    ok = check.absent_ok
                elif check.wanted is None:
                    ok = str(actual).strip() != ""
                else:
                    ok = str(actual) == str(check.wanted)
                if ok or (source, check.path) in reported:
                    continue
                reported.add((source, check.path))
                where = source.relative_to(project_dir).as_posix()
                dotted = ".".join(check.path)
                state = "is not set" if actual is _ABSENT else f"is {actual!r}"
                items.append(
                    Followup(
                        f"{where}: {dotted} {state}: {check.todo} ({check.why})", required=True
                    )
                )
    return items


@dataclasses.dataclass(frozen=True)
class _ChartCheck:
    """A chart value the new settings decide (``wanted`` None: any non-empty value)."""

    path: tuple[str, ...]
    wanted: str | None
    todo: str
    why: str
    absent_ok: bool = False


@click.command()
@click.pass_context
@click.argument(
    "template_path",
    type=click.Path(path_type=pathlib.Path),
    default=".",
    required=False,
)
@click.option(
    "--name",
    "-n",
    help=(
        "Project name for templating (default: the manifest's name, else the current "
        "directory name)"
    ),
)
@shared_template_options
@click.option(
    "--force",
    is_flag=True,
    help="Force overwrite all files (skip smart-merge comparison)",
    default=False,
)
@click.option(
    "--dry-run",
    "--dryrun",
    is_flag=True,
    help="Preview changes without applying them (requires saved metadata)",
    default=False,
)
@click.option(
    "--prefer-new",
    is_flag=True,
    help="Resolve conflicts in favor of the new template version",
    default=False,
)
@click.option(
    "--skip-welcome",
    is_flag=True,
    hidden=True,
    help="Skip the welcome banner (used by nested commands)",
    default=False,
)
def enhance(
    ctx: click.Context,
    template_path: pathlib.Path,
    *,
    name: str | None,
    runtime: str | None,
    model_provider: str | None,
    model: str | None,
    checkpointer: str | None,
    deployment_target: str | None,
    registry: str | None,
    cd: str | None,
    auth_policy: str | None,
    api_policy: str | None,
    process: str | None,
    prototype: bool,
    agent_directory: str | None,
    agent_guidance_filename: str,
    base_template: str | None,
    interactive: bool,
    auto_approve: bool,
    skip_checks: bool,
    skip_deps: bool,
    debug: bool,
    force: bool,
    dry_run: bool,
    prefer_new: bool,
    skip_welcome: bool = False,
) -> None:
    """Add or change the deployment target, CD mode, or runtime of an existing project.

    Applies a template in-place, adding infrastructure files without touching
    your agent logic. It always enhances the current directory.

    TEMPLATE_PATH says which template to apply, not which project to enhance.
    It defaults to the current directory, which re-renders the scaffolding from
    the base template and puts your files back on top.

    If the project has a graph-agents-cli-manifest.yaml, the template recorded
    there is re-applied and TEMPLATE_PATH is ignored. Pass a local directory or
    a remote spec (org/repo@tag) for a project graph-agents-cli did not create.

    --base-template is separate. It names a base template this CLI ships, which
    sits underneath whatever TEMPLATE_PATH supplies.

    api-policy.yaml is never touched by enhance: it belongs to the project;
    change it with `graph-agents-cli api`.

    A runtime or model-provider change is applied to the files it shapes,
    including ones you edited (the chart values key by key, around your
    edits). What cannot be applied is listed under 'Left for you'; the steps
    marked (required) leave the image or the chart on the old settings (an
    edited Dockerfile gets the new version beside it as Dockerfile.new).

    Use --dry-run to preview changes before applying them.

    \b
    Exit codes:
      0  applied (anything left for you is optional)
      1  applied, but steps marked (required) are left for you
      2  usage error (e.g. a model chosen for another provider)
      3  configuration error (no project for --dry-run, a legacy policy file)
    """
    if not skip_welcome:
        display_welcome_banner(enhance_mode=True, quiet=auto_approve)

    if debug:
        logging.basicConfig(level=logging.DEBUG, force=True)
        console.print("> Debug mode enabled")
        logging.debug("Starting enhance command in debug mode")

    if api_policy:
        raise click.UsageError(
            "--api-policy is not accepted by enhance: api-policy.yaml belongs to the "
            "project, and enhance never touches it. Change it with `graph-agents-cli api` "
            "(add, access, allow, deny, revoke, limits, remove), which also keeps the "
            "manifest, .env.example and the chart values in step."
        )

    current_dir = pathlib.Path.cwd()
    _api_policy.ensure_no_legacy_api_policy(find_project_root(current_dir) or current_dir)

    cli_override_args: dict[str, Any] = {}
    for key, value in (
        ("deployment_target", deployment_target),
        ("runtime", runtime),
        ("model_provider", model_provider),
        ("model", model),
        ("checkpointer", checkpointer),
        ("registry", registry),
        ("cd", cd),
        ("auth_policy", auth_policy),
        ("base_template", base_template),
        ("agent_directory", agent_directory),
        ("process", process),
    ):
        if value:
            cli_override_args[key] = value
    if prototype:
        cli_override_args["prototype"] = True
    if agent_guidance_filename != DEFAULT_AGENT_GUIDANCE_FILENAME:
        cli_override_args["agent_guidance_filename"] = agent_guidance_filename

    is_saved_config_subprocess = os.environ.get(_ENV_USING_SAVED_CONFIG) == "1"
    has_cli_overrides = bool(cli_override_args)

    if dry_run and force:
        raise click.UsageError("--dry-run is not compatible with --force.")

    if base_template and not validate_base_template(base_template):
        hint = (
            f"  To apply a fetched template, pass it positionally: graph-agents-cli scaffold enhance {base_template}"
            if remote_template.is_template_spec(base_template)
            else f"  Available: {', '.join(get_available_base_templates())}"
        )
        raise click.ClickException(
            f"--base-template takes a template this CLI ships, not '{base_template}'.\n{hint}"
        )

    if not force and not is_saved_config_subprocess:
        project_config = find_project_config(current_dir)
        if project_config:
            overrides: dict[str, Any] | None = None
            if has_cli_overrides:
                overrides = cli_override_args
            elif interactive:
                saved_config_result = check_and_execute_with_saved_config(
                    project_dir=current_dir,
                    auto_approve=auto_approve,
                    cli_overrides=cli_override_args,
                    dry_run=dry_run,
                    interactive=interactive,
                )
                if saved_config_result is True:
                    return
                elif isinstance(saved_config_result, dict):
                    overrides = saved_config_result
            else:
                if dry_run:
                    raise click.UsageError(
                        "--dry-run requires specifying what to change (e.g. "
                        "--deployment-target kubernetes or --cd argocd) or --interactive."
                    )
                if check_and_execute_with_saved_config(
                    project_dir=current_dir,
                    auto_approve=auto_approve,
                    cli_overrides=cli_override_args,
                    interactive=interactive,
                ):
                    return

            effective_overrides = overrides if overrides else None
            if _run_smart_merge(
                project_dir=current_dir,
                project_config=project_config,
                cli_overrides=effective_overrides,
                auto_approve=auto_approve,
                dry_run=dry_run,
                prefer_new=prefer_new,
                interactive=interactive,
            ):
                return
            console.print("[yellow]⚠️  Smart-merge failed, falling back to standard mode.[/yellow]")
        elif dry_run:
            raise NotInProjectError(
                "--dry-run compares against the project's saved settings, and there is no "
                "graph-agents-cli-manifest.yaml here or in a parent directory.\n"
                "  Run it from a project created by graph-agents-cli."
            )
        elif has_cli_overrides:
            console.print("[dim]No saved metadata found - using standard overwrite mode.[/dim]")
    else:
        if not is_saved_config_subprocess:
            saved_config_result = check_and_execute_with_saved_config(
                project_dir=current_dir,
                auto_approve=auto_approve,
                cli_overrides=cli_override_args,
                force=force,
                interactive=interactive,
            )
            if saved_config_result is True:
                return
        elif not force:
            project_config = find_project_config(current_dir)
            if project_config:
                if _run_smart_merge(
                    project_dir=current_dir,
                    project_config=project_config,
                    cli_overrides=None,
                    auto_approve=auto_approve,
                    dry_run=False,
                    prefer_new=prefer_new,
                    interactive=interactive,
                ):
                    return

    # ---- Standard (overwrite) mode: render in-folder through `create` ----

    if not interactive and not cd and not prototype:
        if auto_approve and deployment_target == "kubernetes":
            console.print(
                "[yellow]Warning: --cd not specified with --auto-approve. "
                "Keeping the recorded value (or 'skip'). Use --cd to configure CD.[/yellow]"
            )

    recorded_config = find_project_config(current_dir)
    if name:
        project_name = name
    elif recorded_config is not None and recorded_config.project_name:
        # The name the project was created with (its chart directory, release
        # and image are named after it), whatever the checkout directory is called.
        project_name = recorded_config.project_name
        console.print(
            f"Using the project name recorded in graph-agents-cli-manifest.yaml: {project_name}",
            style="dim",
        )
    else:
        project_name = current_dir.name
        console.print(f"Using current directory name as project name: {project_name}", style="dim")

    if interactive:
        console.print()
        console.print("🚀 [blue]Ready to enhance your project with deployment capabilities[/blue]")
        console.print(f"📂 {current_dir}")
        console.print()
        console.print("[bold]What will happen:[/bold]")
        console.print("• New template files will be added to this directory")
        console.print("• Your existing files will be preserved")
        console.print("• A backup will be created in ~/.graph-agents-cli/backups/")
        console.print()

        if not click.confirm(
            f"Continue with enhancement? {click.style('[Y/n]: ', fg='blue', bold=True)}",
            default=True,
            show_default=False,
        ):
            console.print("✋ [yellow]Enhancement cancelled.[/yellow]")
            return
        console.print()

    if template_path == pathlib.Path("."):
        agent_spec = "local@."
    elif template_path.is_dir():
        agent_spec = f"local@{template_path.resolve()}"
    else:
        agent_spec = str(template_path)

    final_agent_directory: str | None = agent_directory

    if agent_spec.startswith("local@"):
        from ..utils.remote_template import get_base_template_name, load_remote_template_config

        cli_overrides: dict[str, Any] = {}
        if base_template:
            cli_overrides["base_template"] = base_template
        if agent_directory:
            cli_overrides["settings"] = {"agent_directory": agent_directory}

        source_config = load_remote_template_config(current_dir, cli_overrides)
        original_base_template_name = get_base_template_name(source_config)

        if not base_template and interactive:
            selected_base_template = display_base_template_selection(original_base_template_name)
            base_template = selected_base_template
            if selected_base_template != original_base_template_name:
                cli_overrides["base_template"] = selected_base_template
                console.print(f"✅ Selected base template: [cyan]{selected_base_template}[/cyan]")
                console.print()
        elif not base_template and not remote_template.is_template_spec(
            original_base_template_name
        ):
            # A project made from a fetched template records that template's
            # spec here, which names the source, not a base layer this CLI ships.
            base_template = original_base_template_name

        if cli_overrides.get("base_template"):
            source_config = load_remote_template_config(current_dir, cli_overrides)

        base_template_name = get_base_template_name(source_config)
        if interactive or base_template:
            console.print()
            console.print(f"Template inherits from base: [cyan]{base_template_name}[/cyan]")
            console.print()

    if template_path == pathlib.Path("."):
        gacli_config = find_project_config(current_dir)
        if not gacli_config:
            console.print(
                "[dim]No saved metadata found - defaulting to Python as project language.[/dim]"
            )

        detected_agent_directory = "app"
        if not agent_directory and gacli_config and gacli_config.agent_directory:
            detected_agent_directory = gacli_config.agent_directory

        if not agent_directory and interactive:
            final_agent_directory = display_agent_directory_selection(
                current_dir, detected_agent_directory
            )
            console.print(f"✅ Selected agent directory: [cyan]{final_agent_directory}[/cyan]")
            console.print()
        else:
            final_agent_directory = agent_directory or detected_agent_directory

        if agent_directory:
            console.print(
                f"Info: Using CLI-specified agent directory: [cyan]{agent_directory}[/cyan]"
            )
        elif detected_agent_directory != "app":
            console.print(
                f"Info: Auto-detected agent directory: [cyan]{detected_agent_directory}[/cyan]"
            )

        agent_folder = current_dir / final_agent_directory
        if not agent_folder.is_dir():
            console.print()
            console.print("⚠️  [bold yellow]PROJECT STRUCTURE WARNING[/bold yellow] ⚠️")
            console.print(
                f"📁 Expected [cyan]/{final_agent_directory}[/cyan] folder containing your agent code; "
                f"it is missing from {current_dir}."
            )
            console.print(
                "   Create it and move your agent code there, or use "
                "[cyan]--agent-directory <name>[/cyan] to name your existing directory."
            )
            console.print()
            if interactive and not click.confirm(
                f"Continue with enhancement despite missing /{final_agent_directory} folder?",
                default=True,
            ):
                console.print("✋ [yellow]Enhancement cancelled.[/yellow]")
                return
        else:
            required_var = get_language_config("python")["agent_variable"]
            agent_file = find_agent_file(current_dir, final_agent_directory)
            if agent_file:
                console.print(f"✅ Found [cyan]{agent_file.relative_to(current_dir)}[/cyan]")
                is_valid, error_msg = validate_agent_file(agent_file)
                if is_valid:
                    console.print(f"✅ Found '{required_var}' definition in {agent_file.name}")
                else:
                    console.print(f"⚠️  [yellow]{error_msg}[/yellow]")
                    console.print(
                        f"   app/agent.py must export the compiled graph as [cyan]{required_var}[/cyan]."
                    )
                    if interactive and not click.confirm(
                        f"Continue enhancement? (You can add '{required_var}' later)", default=True
                    ):
                        console.print("✋ [yellow]Enhancement cancelled.[/yellow]")
                        return
            else:
                console.print(
                    f"⚠️  [yellow]Warning: agent.py not found in {final_agent_directory}/[/yellow]"
                )
                console.print(
                    f"   Create {final_agent_directory}/agent.py exporting [cyan]{required_var}[/cyan]"
                )
                if interactive and not click.confirm(
                    "Continue enhancement? (An example agent.py will be created for you)",
                    default=True,
                ):
                    console.print("✋ [yellow]Enhancement cancelled.[/yellow]")
                    return

    final_cli_overrides: dict[str, Any] = {}
    if base_template:
        final_cli_overrides["base_template"] = base_template
    if template_path == pathlib.Path(".") and final_agent_directory:
        final_cli_overrides["settings"] = {"agent_directory": final_agent_directory}

    effective_create_params = _backfill_create_params_from_config(
        current_dir,
        {
            "deployment_target": deployment_target,
            "runtime": runtime,
            "model_provider": model_provider,
            "model": model,
            "checkpointer": checkpointer,
            "registry": registry,
            "cd": cd,
            "auth_policy": auth_policy,
        },
    )

    # Read before the render: create rewrites the manifest in place.
    existing_config = find_project_config(current_dir)
    previous_params = _recorded_params(existing_config, current_dir) if existing_config else None
    recorded_base = existing_config.base_template if existing_config else None
    effective_process = process or (existing_config.process if existing_config else None)
    # Keep the recorded guidance file unless one was asked for explicitly.
    effective_guidance = agent_guidance_filename
    if agent_guidance_filename == DEFAULT_AGENT_GUIDANCE_FILENAME and existing_config:
        effective_guidance = existing_config.agent_guidance_filename

    ctx.invoke(
        create,
        project_name=project_name,
        agent=agent_spec,
        output_dir=None,
        runtime=effective_create_params["runtime"],
        model_provider=effective_create_params["model_provider"],
        model=effective_create_params["model"],
        checkpointer=effective_create_params["checkpointer"],
        deployment_target=effective_create_params["deployment_target"],
        registry=effective_create_params["registry"],
        cd=effective_create_params["cd"],
        auth_policy=effective_create_params["auth_policy"],
        api_policy=None,
        process=effective_process,
        prototype=prototype,
        agent_directory=final_agent_directory
        if template_path == pathlib.Path(".")
        else agent_directory,
        agent_guidance_filename=effective_guidance,
        base_template=base_template,
        interactive=interactive,
        auto_approve=auto_approve,
        skip_checks=skip_checks,
        skip_deps=skip_deps,
        debug=debug,
        skip_welcome=True,
        in_folder=True,
        cli_overrides=final_cli_overrides if final_cli_overrides else None,
    )

    # An in-folder render rewrites the manifest from the template it rendered,
    # which for a fetched template is a synthesized local name. The project came
    # from the recorded spec and still has to re-fetch it next time.
    if recorded_base and remote_template.is_template_spec(recorded_base):
        update_cli_metadata(current_dir, {}, base_template=recorded_base)

    if existing_config is not None and previous_params is not None:
        new_config = find_project_config(current_dir)
        if new_config is not None:
            _reconcile_after_in_folder_render(
                project_dir=current_dir,
                project_name=project_name,
                existing_config=existing_config,
                previous=previous_params,
                new_config=new_config,
            )


# --api-policy is shared with create but refused here (the policy belongs to the
# project; `graph-agents-cli api` changes it), so --help does not offer it.
for _param in enhance.params:
    if _param.name == "api_policy" and isinstance(_param, click.Option):
        _param.hidden = True


def _reconcile_after_in_folder_render(
    *,
    project_dir: pathlib.Path,
    project_name: str,
    existing_config: ProjectConfig,
    previous: CreateParams,
    new_config: ProjectConfig,
) -> None:
    """Finish a ``--force`` (in-folder) render whose settings changed.

    The in-folder render overlays the project on the new render, so it only
    adds the files the project lacks: everything it already has (the chart
    values, ``.env.example``, ``pyproject.toml``'s dependencies, ...) would keep
    the old settings. The same three-way pass as the smart merge brings every
    file the developer did not edit to what a fresh ``create`` renders, merges
    the template's change into edited config files, recomputes ``secrets.keys``
    and prints what is left, keeping the developer's version of anything else
    they changed.
    """
    current = _recorded_params(new_config, project_dir)
    if current == previous:
        return

    def _after_merge(proj_dir: pathlib.Path, _language: str) -> list[str | Followup]:
        added, removed = reconcile_secret_keys(proj_dir, previous=previous, current=current)
        return [
            *_settings_followups(proj_dir, previous, current, added, removed),
            *_chart_followups(proj_dir, previous, current),
        ]

    console.print()
    console.print("Reconciling the files the new settings shape...", style="dim")
    run_three_way_merge(
        project_dir=project_dir,
        project_name=project_name,
        agent_directory=new_config.agent_directory,
        language=new_config.language,
        old_args=metadata_to_cli_args(existing_config),
        new_args=metadata_to_cli_args(new_config),
        auto_approve=True,
        dry_run=False,
        operation_label="enhancement",
        post_apply_hook=_after_merge,
        merge_config=True,
    )
