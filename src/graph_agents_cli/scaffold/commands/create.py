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

"""``create``: scaffold a LangGraph agent project."""

import dataclasses
import functools
import logging
import pathlib
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass

import click
from rich.prompt import IntPrompt, Prompt

from graph_agents_cli import _api_policy
from graph_agents_cli._defaults import (
    DEFAULT_AGENT_GUIDANCE_FILENAME,
    DEFAULT_AUTH_POLICY,
    DEFAULT_CD,
    DEFAULT_DEPLOYMENT_TARGET,
    DEFAULT_MODEL_PROVIDER,
    DEFAULT_MODELS,
    DEFAULT_REGISTRY_HOST,
    DEFAULT_REGISTRY_PLACEHOLDER,
    DEFAULT_RUNTIME,
)
from graph_agents_cli._output import Console
from graph_agents_cli._project import MANIFEST_FILENAME, read_project_config
from graph_agents_cli.dev import policy_check

from ..utils import cli_options, openapi_seed, remote_template, template
from ..utils.fs import standard_ignore_patterns
from ..utils.logging import display_welcome_banner
from ..utils.manifest import (
    API_POLICY_FILENAME,
    CreateParams,
    finalize_manifest,
)
from ..utils.version import get_current_version

__all__ = ["create"]

console = Console()

DEFAULT_AGENT = "langgraph"


@dataclass
class AgentSelection:
    """Outcome of resolving which agent/template to use and fetching it."""

    agent: str | None
    final_agent: str
    template_source_path: pathlib.Path | None
    temp_dir_to_clean: str | None
    remote_spec: remote_template.RemoteTemplateSpec | None
    recorded_spec: str | None


@dataclass
class LoadedTemplateConfig:
    """Outcome of loading and merging a selected template's config."""

    config: dict
    template_path: pathlib.Path
    base_template_name: str | None
    deployment_agent_name: str
    remote_config: dict | None
    cli_overrides: dict | None


def _handle_create_errors(f: Callable) -> Callable:
    """Convert create failures to Click's concise CLI errors."""

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except (click.ClickException, click.Abort):
            raise
        except ValueError as e:
            raise click.UsageError(str(e)) from e
        except Exception as e:
            raise click.ClickException(str(e)) from e

    return wrapper


@click.command()
@click.pass_context
@click.argument("project_name", required=False, default=None)
@click.option(
    "--agent",
    "-a",
    help=(
        "Template to use: a bundled agent (default: langgraph), a local path "
        "(`local@/path/to/template`), or a remote spec (`org/repo/path@ref`, "
        "`https://github.com/org/repo/tree/main/path`)."
    ),
)
@click.option(
    "--output-dir",
    "-o",
    type=click.Path(),
    help="Output directory for the project (default: current directory)",
)
@click.option(
    "--skip-welcome",
    is_flag=True,
    hidden=True,
    help="Skip the welcome banner",
    default=False,
)
@click.option(
    "--quiet",
    is_flag=True,
    hidden=True,
    help="Internal flag: suppress the banner, the notices and the next steps",
    default=False,
)
@click.option(
    "--locked",
    is_flag=True,
    hidden=True,
    help="Internal flag for version-locked remote templates",
    default=False,
)
@cli_options.shared_template_options
@_handle_create_errors
def create(
    ctx: click.Context,
    project_name: str,
    *,
    agent: str | None,
    output_dir: str | None,
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
    skip_welcome: bool = False,
    quiet: bool = False,
    locked: bool = False,
    in_folder: bool = False,
    cli_overrides: dict | None = None,
) -> None:
    """Create a LangGraph agent project from a template."""
    # A quiet run builds a throwaway tree for the three-way merge behind
    # `scaffold enhance` and `scaffold upgrade`, so nothing it says is addressed
    # to anyone. Warnings and errors still print.
    if quiet:
        skip_welcome = True

    if not skip_welcome:
        display_welcome_banner(agent=agent, quiet=auto_approve)

    project_name = _resolve_project_name(
        project_name,
        interactive=interactive,
        auto_approve=auto_approve,
        output_dir=output_dir,
    )

    if debug:
        logging.basicConfig(level=logging.DEBUG, force=True)
        console.print("> Debug mode enabled")
        logging.debug("Starting CLI in debug mode")

    destination_dir = (pathlib.Path(output_dir) if output_dir else pathlib.Path.cwd()).resolve()

    if in_folder:
        # A re-render of a project still on the retired product API policy
        # would drop the policy silently: stop with the migration steps.
        _api_policy.ensure_no_legacy_api_policy(destination_dir)

    # Validate the seed policy before anything is rendered, including that every
    # OpenAPI spec it references can be copied into the project (lint reads them).
    api_document = _api_policy.load_policy_document(api_policy) if api_policy else None
    spec_copies = (
        openapi_seed.plan_spec_copies(api_document, api_policy)
        if api_document is not None and api_policy
        else []
    )

    project_path = _prepare_project_path(
        destination_dir,
        project_name,
        in_folder=in_folder,
        auto_approve=auto_approve,
        interactive=interactive,
    )

    if not skip_checks and not quiet:
        _preflight()

    resolved = _resolve_template(
        agent,
        deployment_target=deployment_target,
        interactive=interactive,
        auto_approve=auto_approve,
        locked=locked,
        project_name=project_name,
        base_template=base_template,
        cli_overrides=cli_overrides,
    )
    if resolved is None:
        # A version-locked template executed a nested command; nothing more to do.
        return

    selection, loaded = resolved
    agent = selection.agent
    final_agent = selection.final_agent
    template_source_path = selection.template_source_path
    temp_dir_to_clean = selection.temp_dir_to_clean
    remote_spec = selection.remote_spec
    recorded_spec = selection.recorded_spec
    config = loaded.config
    template_path = loaded.template_path
    remote_config = loaded.remote_config
    base_template_name = loaded.base_template_name
    cli_overrides = loaded.cli_overrides

    try:
        params = _resolve_create_params(
            deployment_target=deployment_target,
            runtime=runtime,
            model_provider=model_provider,
            model=model,
            checkpointer=checkpointer,
            registry=registry,
            cd=cd,
            auth_policy=auth_policy,
            api_document=api_document,
            api_policy_dir=pathlib.Path(api_policy).resolve().parent if api_policy else None,
            process=process,
            prototype=prototype,
            interactive=interactive,
            auto_approve=auto_approve,
            quiet=quiet,
            skip_checks=skip_checks,
            deployment_agent_name=loaded.deployment_agent_name,
            remote_config=remote_config,
            git_dir=destination_dir,
        )
    except Exception:
        if temp_dir_to_clean:
            shutil.rmtree(temp_dir_to_clean, ignore_errors=True)
        raise
    logging.debug("Resolved create params: %s", params)

    # An in-folder render (enhance/upgrade overwrite mode) re-renders the manifest
    # and the API policy from the template before finalize_manifest runs, so
    # the developer's recorded state has to be captured here and re-applied.
    existing_policy: bytes | None = None
    if in_folder:
        params = _carry_recorded_state(destination_dir, params)
        policy_path = destination_dir / API_POLICY_FILENAME
        if api_policy is None and policy_path.is_file():
            existing_policy = policy_path.read_bytes()
            apis, example = _read_project_policy(policy_path)
            params = dataclasses.replace(
                params, has_api_policy=True, apis=apis, example_call=example
            )
        logging.debug("Create params after carrying the recorded state: %s", params)
        # The carried APIs never went through _resolve_create_params' check: an
        # enhance that switches the runtime must be refused exactly like create.
        problem = _api_policy.forward_runtime_problem(params.apis, params.runtime)
        if problem:
            if temp_dir_to_clean:
                shutil.rmtree(temp_dir_to_clean, ignore_errors=True)
            raise click.UsageError(f"{problem} (in {API_POLICY_FILENAME})")

    if not template_source_path:
        template_path = template.get_template_path(final_agent)
    logging.debug("Template path: %s", template_path)

    destination_dir.mkdir(parents=True, exist_ok=True)

    final_cli_overrides = dict(cli_overrides or {})
    if agent_directory:
        final_cli_overrides.setdefault("settings", {})
        final_cli_overrides["settings"]["agent_directory"] = agent_directory

    # `local@.` overlays the current directory onto itself, so the manifest in
    # the overlay is this project's own and has to survive the copy. Every other
    # source is a template, whose manifest describes the template.
    overlay_is_project = isinstance(agent, str) and agent.strip().rstrip("/") == "local@."

    try:
        rendered_path = template.process_template(
            agent_name=final_agent,
            template_dir=template_path,
            project_name=project_name,
            deployment_target=params.deployment_target,
            runtime=params.runtime,
            model_provider=params.model_provider,
            model=params.model,
            checkpointer=params.checkpointer,
            registry=params.registry,
            cd=params.cd,
            auth_policy=params.auth_policy,
            has_api_policy=params.has_api_policy,
            apis=params.apis,
            example_call=params.example_call,
            process=params.process,
            output_dir=destination_dir,
            remote_template_path=template_source_path,
            remote_config=config,
            in_folder=in_folder,
            overlay_is_project=overlay_is_project,
            cli_overrides=final_cli_overrides or None,
            remote_spec=remote_spec,
            # The project records the spec it was fetched from, so enhance and
            # upgrade can fetch it again.
            recorded_base_template=recorded_spec if not in_folder else None,
            agent_guidance_filename=agent_guidance_filename,
            auth_policy_implemented=params.auth_policy_implemented,
        )

        spec_lines: list[str] = []
        if api_policy:
            shutil.copy2(api_policy, rendered_path / API_POLICY_FILENAME)
            logging.debug("Seeded %s from %s", API_POLICY_FILENAME, api_policy)
            spec_lines = openapi_seed.install_spec_copies(rendered_path, spec_copies)
        elif existing_policy is not None:
            # api-policy.yaml belongs to the project (it is the security
            # boundary the team reviews): an in-folder re-render must never
            # replace it with the template's example.
            (rendered_path / API_POLICY_FILENAME).write_bytes(existing_policy)
            logging.debug("Restored the project's own %s", API_POLICY_FILENAME)

        finalize_manifest(
            rendered_path,
            project_name=project_name,
            params=params,
            cli_version=get_current_version(),
        )

        # Remote templates inherit base-template files that import packages the
        # remote's own pyproject may not declare; re-add the base template's
        # extra_dependencies so the inherited code resolves. Skipped with
        # --skip-deps (reusing a saved config).
        if remote_config and not skip_deps:
            if base_template_name is None:
                raise RuntimeError("remote_config set without a base template")
            base_config = template.load_template_config(
                template.get_template_path(base_template_name)
            )
            base_deps = base_config.get("settings", {}).get("extra_dependencies", [])
            if base_deps:
                template.add_base_template_dependencies(
                    project_path,
                    base_deps,
                    base_template_name,
                    auto_approve=auto_approve,
                    interactive=interactive,
                )

    except ValueError as e:
        # process_template raises ValueError for input the user can fix.
        raise click.ClickException(str(e)) from e

    finally:
        if temp_dir_to_clean:
            try:
                shutil.rmtree(temp_dir_to_clean)
                logging.debug("Cleaned up temporary directory: %s", temp_dir_to_clean)
            except OSError as e:
                logging.warning(f"Failed to clean up temporary directory {temp_dir_to_clean}: {e}")

    # A quiet run's project lives in a temp directory that is deleted moments
    # later, so the next-steps banner would be wrong as well as noisy.
    if quiet:
        return

    if spec_lines:
        console.print(f"Copied the OpenAPI specs {API_POLICY_FILENAME} references:", style="cyan")
        for line in spec_lines:
            console.print(f"  {line}", style="cyan")

    if params.has_api_policy and params.example_call is None and not in_folder:
        tools_dir = (agent_directory or _rendered_agent_directory(rendered_path)) + "/tools"
        console.print(
            f"Note: {tools_dir}/example_api.py was not generated: the first API in "
            f"{API_POLICY_FILENAME} allows no GET the example could make (lint and the "
            "project's policy test would refuse it). Write your tools with their calls "
            "declared in API_CALLS (see 'Outbound API access' in README.md).",
            style="yellow",
        )

    _print_next_steps(
        in_folder=in_folder,
        destination_dir=destination_dir,
        project_name=project_name,
        output_dir=output_dir,
        params=params,
    )


# ---------------------------------------------------------------------------
# Project name and destination
# ---------------------------------------------------------------------------


def _resolve_project_name(
    project_name: str,
    *,
    interactive: bool,
    auto_approve: bool,
    output_dir: str | None,
) -> str:
    """Resolve, validate, and normalize the project name.

    Prompts in interactive mode, defaults to "my-agent" under --auto-approve,
    and errors in strict programmatic mode when no name is supplied.
    """
    if not project_name:
        if interactive:
            project_name = _prompt_for_project_name(output_dir=output_dir)
        elif auto_approve:
            project_name = "my-agent"
            console.print(
                f"Info: Project name not specified. Defaulting to '{project_name}' in auto-approve mode.",
                style="yellow",
            )
        else:
            raise click.UsageError(
                "project-name is a required argument in programmatic mode.\n"
                "You can also use -i / --interactive for interactive mode or --auto-approve / --yes to select defaults."
            )

    errors = _validate_project_name(project_name)
    if errors:
        raise click.UsageError("\n".join(errors))

    return normalize_project_name(project_name)


def _prompt_for_project_name(output_dir: str | None) -> str:
    """Interactively prompt for a valid, unique project name."""
    check_dir = (pathlib.Path(output_dir) if output_dir else pathlib.Path.cwd()).resolve()
    while True:
        project_name = Prompt.ask(
            "\n> Enter a name for your project",
            default="my-agent",
            show_default=True,
        )
        errors = _validate_project_name(project_name)
        if errors:
            for error in errors:
                console.print(f"Error: {error}", style="bold red")
            continue
        normalized_name = normalize_project_name(project_name)
        if (check_dir / normalized_name).exists():
            console.print(
                f"Error: Project directory '{check_dir / normalized_name}' already exists. Please choose a different name.",
                style="bold red",
            )
            continue
        return project_name


def _validate_project_name(project_name: str) -> list[str]:
    """Validate a project name; returns a list of errors (empty when valid)."""
    errors = []
    if len(project_name) > 26:
        errors.append(
            f"Project name '{project_name}' exceeds 26 characters. Please use a shorter name."
        )
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]*$", project_name):
        errors.append(
            f"Project name '{project_name}' must start with a letter or digit and contain "
            "only letters, digits, hyphens and underscores (it becomes the Helm release name)."
        )
    return errors


def normalize_project_name(project_name: str) -> str:
    """Normalize the project name (lowercase, hyphens) for Helm releases and namespaces."""
    needs_normalization = any(char.isupper() for char in project_name) or "_" in project_name

    if needs_normalization:
        normalized_name = project_name
        console.print(
            "Note: Project names are normalized (lowercase, hyphens only) because they name "
            "the Helm release and the Kubernetes namespaces.",
            style="dim",
        )
        if any(char.isupper() for char in normalized_name):
            normalized_name = normalized_name.lower()
            console.print(
                f"Info: Converting to lowercase for compatibility: '{project_name}' -> '{normalized_name}'",
                style="bold yellow",
            )

        if "_" in normalized_name:
            name_before_hyphenation = normalized_name
            normalized_name = normalized_name.replace("_", "-")
            console.print(
                f"Info: Replacing underscores with hyphens for compatibility: '{name_before_hyphenation}' -> '{normalized_name}'",
                style="yellow",
            )

        return normalized_name

    return project_name


def _prepare_project_path(
    destination_dir: pathlib.Path,
    project_name: str,
    *,
    in_folder: bool,
    auto_approve: bool,
    interactive: bool,
) -> pathlib.Path:
    """Resolve the project path and prepare the destination.

    In-folder mode backs up the existing directory (aborting cleanly if the user
    declines); otherwise verifies the target does not already exist.
    """
    if in_folder:
        from ..utils.backup import create_project_backup

        try:
            create_project_backup(
                destination_dir,
                console=console,
                auto_approve=auto_approve,
                interactive=interactive,
            )
        except click.Abort:
            console.print("✋ [red]Operation cancelled.[/red]")
            raise

        console.print()
        return destination_dir

    project_path = destination_dir / project_name
    if project_path.exists():
        raise click.UsageError(f"Project directory '{project_path}' already exists")
    return project_path


def _preflight() -> None:
    """Warn about missing local tooling; never blocks scaffolding."""
    if shutil.which("uv") is None:
        console.print(
            "⚠️  uv is not on PATH. Install it (https://docs.astral.sh/uv/) before "
            "`graph-agents-cli install`.",
            style="yellow",
        )


# ---------------------------------------------------------------------------
# Template selection
# ---------------------------------------------------------------------------


def _resolve_template(
    agent: str | None,
    *,
    deployment_target: str | None,
    interactive: bool,
    auto_approve: bool,
    locked: bool,
    project_name: str,
    base_template: str | None,
    cli_overrides: dict | None,
) -> tuple[AgentSelection, LoadedTemplateConfig] | None:
    """Resolve the agent selection and load its (possibly remote) template config."""
    agent_selection = _select_agent(
        agent,
        deployment_target=deployment_target,
        interactive=interactive,
        auto_approve=auto_approve,
        locked=locked,
        project_name=project_name,
    )
    if agent_selection is None:
        return None
    logging.debug("Selected agent: %s", agent_selection.final_agent)

    loaded_template_config = _load_template_config(
        agent_selection,
        base_template=base_template,
        cli_overrides=cli_overrides,
    )

    return agent_selection, loaded_template_config


def _select_agent(
    agent: str | None,
    *,
    deployment_target: str | None,
    interactive: bool,
    auto_approve: bool,
    locked: bool,
    project_name: str,
) -> AgentSelection | None:
    """Resolve which agent to use and fetch its template if remote/local."""
    if agent:
        return _resolve_specified_agent(agent, locked=locked, project_name=project_name)

    return _select_agent_interactively(
        deployment_target=deployment_target,
        interactive=interactive,
        auto_approve=auto_approve,
        locked=locked,
        project_name=project_name,
    )


def _resolve_specified_agent(
    agent: str,
    *,
    locked: bool,
    project_name: str,
) -> AgentSelection | None:
    """Resolve an explicit ``--agent`` value: local@ path, remote spec, or bundled name/number."""
    if agent.startswith("local@"):
        return _resolve_local_spec(agent, locked=locked, project_name=project_name)

    remote = _resolve_remote_spec(agent, locked=locked, project_name=project_name)
    if remote:
        return remote

    agents = template.get_available_agents(include_hidden=True)
    if any(p["name"] == agent for p in agents.values()):
        selected_agent = agent
    else:
        try:
            agent_num = int(agent)
            if agent_num in agents:
                selected_agent = agents[agent_num]["name"]
            else:
                raise ValueError(f"Invalid agent number: {agent_num}")
        except ValueError as err:
            available = ", ".join(a["name"] for a in agents.values())
            raise click.UsageError(
                f"Invalid agent name or number: {agent}. Available: {available}"
            ) from err

    return AgentSelection(
        agent=agent,
        final_agent=selected_agent,
        template_source_path=None,
        temp_dir_to_clean=None,
        remote_spec=None,
        recorded_spec=None,
    )


def _resolve_local_spec(
    agent: str,
    *,
    locked: bool,
    project_name: str,
) -> AgentSelection | None:
    """Resolve a ``local@<path>`` spec: copy the template to a temp dir."""
    path_str = agent.split("@", 1)[1]
    local_path = pathlib.Path(path_str).resolve()
    if not local_path.is_dir():
        raise click.ClickException(f"Local path not found or not a directory: {local_path}")

    # Record the absolute path: a relative local path would resolve against
    # wherever enhance / upgrade are later run from.
    recorded_spec = f"local@{local_path}"

    temp_dir = tempfile.mkdtemp(prefix="gacli_local_template_")
    template_source_path = pathlib.Path(temp_dir) / local_path.name
    shutil.copytree(local_path, template_source_path, ignore=standard_ignore_patterns)

    if remote_template.check_and_execute_with_version_lock(
        template_source_path, agent, locked, project_name
    ):
        shutil.rmtree(temp_dir, ignore_errors=True)
        return None

    if locked:
        console.print("✅ Using version-locked template", style="green")
    else:
        console.print(f"Using local template: {local_path}")
    logging.debug("Copied local template to temporary dir: %s", template_source_path)
    return AgentSelection(
        agent=agent,
        final_agent=f"local_{template_source_path.name}",
        template_source_path=template_source_path,
        temp_dir_to_clean=temp_dir,
        remote_spec=None,
        recorded_spec=recorded_spec,
    )


def _resolve_remote_spec(
    agent: str,
    *,
    locked: bool,
    project_name: str,
) -> AgentSelection | None:
    """Fetch a remote-template spec when ``agent`` names one; None otherwise."""
    remote_spec = remote_template.parse_agent_spec(agent)
    if not remote_spec:
        return None

    console.print(f"Fetching remote template: {agent}")
    template_source_path, temp_dir_path = remote_template.fetch_remote_template(
        remote_spec, agent, locked, project_name
    )
    return AgentSelection(
        agent=agent,
        final_agent=f"remote_{hash(agent)}",
        template_source_path=template_source_path,
        temp_dir_to_clean=str(temp_dir_path),
        remote_spec=remote_spec,
        recorded_spec=agent,
    )


def _select_agent_interactively(
    *,
    deployment_target: str | None,
    interactive: bool,
    auto_approve: bool,
    locked: bool,
    project_name: str,
) -> AgentSelection:
    """Select an agent when no explicit ``--agent`` was given.

    Prompts interactively; otherwise defaults to ``langgraph`` (or the first
    available agent) and says so.
    """
    agents = template.get_available_agents(deployment_target=deployment_target)

    if interactive:
        final_agent = display_agent_selection(deployment_target)
    else:
        if not agents:
            raise click.ClickException(
                "Error: No agents available for the selected deployment target."
            )
        names = [a["name"] for a in agents.values()]
        final_agent = DEFAULT_AGENT if DEFAULT_AGENT in names else names[0]
        console.print(
            f"Info: --agent not specified. Defaulting to '{final_agent}'.",
            style="yellow",
        )

    # A browse result may itself be a spec: process it like CLI input.
    if final_agent.startswith("local@"):
        local = _resolve_local_spec(final_agent, locked=locked, project_name=project_name)
        if local is None:
            raise click.ClickException("Version-locked template executed a nested command.")
        return local
    remote = _resolve_remote_spec(final_agent, locked=locked, project_name=project_name)
    if remote:
        return remote

    return AgentSelection(
        agent=final_agent,
        final_agent=final_agent,
        template_source_path=None,
        temp_dir_to_clean=None,
        remote_spec=None,
        recorded_spec=None,
    )


def display_agent_selection(deployment_target: str | None = None) -> str:
    """Display the bundled agents and prompt for a selection (or a custom spec)."""
    agents = template.get_available_agents(deployment_target=deployment_target)
    if not agents:
        if deployment_target:
            raise click.ClickException(
                f"No agents available for deployment target '{deployment_target}'"
            )
        raise click.ClickException("No valid agents found")

    console.print("\n> Please select an agent to get started:")
    console.print("\n  [bold cyan]🐍 Python[/]")
    for num, agent in agents.items():
        display_name = agent.get("display_name", agent["name"])
        console.print(
            f"     {num}. [bold]{display_name.ljust(14)}[/] [dim]{agent['description']}[/]"
        )

    custom_num = len(agents) + 1
    console.print("\n  [bold cyan]🔧 More Options[/]")
    console.print(
        f"     {custom_num}. [bold]{'Custom'.ljust(14)}[/] "
        "[dim]local@/path/to/template or org/repo/path@ref[/]"
    )

    while True:
        choice = IntPrompt.ask(
            "\nEnter the number of your template choice", default=1, show_default=True
        )
        if choice in agents:
            return agents[choice]["name"]
        if choice == custom_num:
            spec = Prompt.ask("\nEnter the template spec (local@<path> or a remote spec)").strip()
            if spec.startswith("local@") or remote_template.parse_agent_spec(spec):
                return spec
            console.print(f"Invalid template spec: {spec}", style="bold red")
            continue
        console.print(f"Invalid agent selection: {choice}", style="bold red")


def _load_template_config(
    selection: AgentSelection,
    *,
    base_template: str | None,
    cli_overrides: dict | None,
) -> LoadedTemplateConfig:
    """Load and merge the selected template's config.

    For remote / local-path templates, loads the remote config, merges it over
    the inherited base-template config, and derives the deployment agent name
    from the base template. For bundled agents, loads the local config and
    applies any CLI overrides.
    """
    final_agent = selection.final_agent
    template_source_path = selection.template_source_path
    remote_spec = selection.remote_spec

    base_template_name = None
    if template_source_path:
        if cli_overrides is None:
            cli_overrides = {}

        if base_template:
            if not template.validate_base_template(base_template):
                available_templates = template.get_available_base_templates()
                if selection.temp_dir_to_clean:
                    shutil.rmtree(selection.temp_dir_to_clean, ignore_errors=True)
                raise click.UsageError(
                    f"Base template '{base_template}' not found.\n"
                    f"Available base templates: {', '.join(available_templates)}"
                )
            cli_overrides["base_template"] = base_template

        source_config = remote_template.load_remote_template_config(
            template_source_path, cli_overrides
        )
        if source_config:
            logging.debug("Final remote template config: %s", source_config)

        base_template_name = remote_template.get_base_template_name(source_config)
        logging.debug("Using base template: %s", base_template_name)

        base_config = template.load_template_config(
            template.agents_dir() / base_template_name / ".template"
        )
        config = remote_template.merge_template_configs(base_config, source_config)
        template_path = template_source_path / ".template"
    else:
        template_path = template.agents_dir() / final_agent / ".template"
        config = template.load_template_config(template_path)
        if cli_overrides:
            config = remote_template.merge_template_configs(config, cli_overrides)
            logging.debug("Applied CLI overrides to local template config: %s", cli_overrides)

    if final_agent and not remote_spec and config.get("hidden", False):
        console.print(
            f"Warning: '{final_agent}' is an experimental template and is not fully supported.",
            style="yellow",
        )

    deployment_agent_name = final_agent
    remote_config = None
    if template_source_path:
        deployment_agent_name = remote_template.get_base_template_name(config)
        remote_config = config

    return LoadedTemplateConfig(
        config=config,
        template_path=template_path,
        base_template_name=base_template_name,
        deployment_agent_name=deployment_agent_name,
        remote_config=remote_config,
        cli_overrides=cli_overrides,
    )


# ---------------------------------------------------------------------------
# Create parameters (runtime, provider, model, checkpointer, target, cd, ...)
# ---------------------------------------------------------------------------


def _resolve_create_params(
    *,
    deployment_target: str | None,
    runtime: str | None,
    model_provider: str | None,
    model: str | None,
    checkpointer: str | None,
    registry: str | None,
    cd: str | None,
    auth_policy: str | None,
    api_document: dict | None,
    api_policy_dir: pathlib.Path | None,
    process: str | None,
    prototype: bool,
    interactive: bool,
    auto_approve: bool,
    quiet: bool,
    skip_checks: bool,
    deployment_agent_name: str,
    remote_config: dict | None,
    git_dir: pathlib.Path,
) -> CreateParams:
    """Resolve every create parameter (flag > prompt > default) and validate the combination."""
    final_deployment = _resolve_deployment_target(
        deployment_target=deployment_target,
        prototype=prototype,
        deployment_agent_name=deployment_agent_name,
        remote_config=remote_config,
        interactive=interactive,
        auto_approve=auto_approve,
        quiet=quiet,
    )
    logging.debug("Selected deployment target: %s", final_deployment)

    final_runtime = runtime or (
        template.prompt_runtime(DEFAULT_RUNTIME) if interactive else DEFAULT_RUNTIME
    )
    final_provider = model_provider or (
        template.prompt_model_provider(DEFAULT_MODEL_PROVIDER)
        if interactive
        else DEFAULT_MODEL_PROVIDER
    )
    default_model = DEFAULT_MODELS.get(final_provider, "")
    if model:
        final_model = model
    elif interactive:
        final_model = (
            Prompt.ask("\n> Model name", default=default_model, show_default=True).strip()
            or default_model
        )
    else:
        final_model = default_model

    default_checkpointer = template.default_checkpointer(final_deployment)
    final_checkpointer = checkpointer or (
        template.prompt_checkpointer(default_checkpointer) if interactive else default_checkpointer
    )

    final_cd = _resolve_cd(
        cd=cd,
        prototype=prototype,
        final_deployment=final_deployment,
        interactive=interactive,
        auto_approve=auto_approve,
        quiet=quiet,
    )

    try:
        template.validate_combination(final_runtime, final_checkpointer, final_deployment, final_cd)
    except ValueError as e:
        raise click.UsageError(str(e)) from e

    final_registry = _resolve_registry(
        registry=registry,
        final_deployment=final_deployment,
        interactive=interactive,
        quiet=quiet,
        git_dir=git_dir,
    )

    final_auth_policy = auth_policy or (
        template.prompt_auth_policy(DEFAULT_AUTH_POLICY) if interactive else DEFAULT_AUTH_POLICY
    )
    if final_auth_policy == "custom" and not quiet:
        console.print(
            "Info: custom ships as a fail-closed stub: implement CustomPolicy in "
            "app/policies/custom.py and set auth_policy_implemented: true before "
            "`deploy --env staging|prod`.",
            style="cyan",
        )
    elif final_auth_policy == "jwt" and not quiet:
        console.print(
            "Info: jwt verifies OIDC bearer tokens: set AUTH_JWT_JWKS_URL (or "
            "AUTH_JWT_PUBLIC_KEY), AUTH_JWT_ISSUER and AUTH_JWT_AUDIENCE (see .env.example).",
            style="cyan",
        )

    apis = _api_policy.summarize(api_document) if api_document else ()
    problem = _api_policy.forward_runtime_problem(apis, final_runtime)
    if problem:
        raise click.UsageError(problem)

    return CreateParams(
        deployment_target=final_deployment,
        runtime=final_runtime,
        model_provider=final_provider,
        model=final_model,
        checkpointer=final_checkpointer,
        registry=final_registry,
        cd=final_cd,
        auth_policy=final_auth_policy,
        has_api_policy=api_document is not None,
        process=(process or "").strip() or None,
        # Every bearer API's token variable joins secrets.keys.
        apis=apis,
        # A relative openapi: path is read next to the seed policy.
        example_call=(
            policy_check.example_call(api_document, base_dir=api_policy_dir)
            if api_document
            else None
        ),
    )


def _read_project_policy(
    path: pathlib.Path,
) -> tuple[tuple[_api_policy.ApiSummary, ...], _api_policy.ExampleCall | None]:
    """The APIs and the example call of a project's own policy (empty when unreadable).

    A relative ``openapi:`` path is resolved against the project root, as lint does.
    """
    document = _api_policy.read_policy_document(path)
    if document is None:
        return (), None
    return _api_policy.summarize(document), policy_check.example_call(
        document, base_dir=path.parent
    )


def _rendered_agent_directory(project_dir: pathlib.Path) -> str:
    """The agent directory the rendered manifest records (``app`` when unreadable)."""
    try:
        return read_project_config(str(project_dir)).agent_directory or "app"
    except click.ClickException:
        return "app"


def _carry_recorded_state(project_dir: pathlib.Path, params: CreateParams) -> CreateParams:
    """Keep what the existing manifest records where the re-render must not reset it.

    ``auth_policy_implemented`` is the developer's flag (flipped once the stub
    is replaced): it is kept when the policy is unchanged and re-derived only
    when ``auth_policy`` switches. The APIs of an existing ``api-policy.yaml``
    are carried so their bearer token variables stay in ``secrets.keys``.
    """
    if not (project_dir / MANIFEST_FILENAME).is_file():
        return params
    try:
        existing = read_project_config(str(project_dir))
    except click.ClickException as e:
        logging.warning("Could not read the existing manifest: %s", e)
        return params
    updates: dict[str, object] = {}
    if existing.auth_policy == params.auth_policy:
        updates["auth_policy_implemented"] = existing.auth_policy_implemented
    if not params.apis and existing.api_policy_file:
        apis, example = _read_project_policy(project_dir / existing.api_policy_file)
        if apis:
            updates["apis"] = apis
            updates["example_call"] = example
    return dataclasses.replace(params, **updates) if updates else params


def _resolve_deployment_target(
    *,
    deployment_target: str | None,
    prototype: bool,
    deployment_agent_name: str,
    remote_config: dict | None,
    interactive: bool,
    auto_approve: bool,
    quiet: bool,
) -> str:
    """Resolve the deployment target.

    Honors an explicit --deployment-target, defaults to 'none' in prototype
    mode, auto-selects when only one target exists, prompts interactively, and
    otherwise defaults to kubernetes.
    """
    if deployment_target:
        return deployment_target

    if prototype:
        if not quiet:
            console.print("Info: Prototype mode: using deployment_target='none'.", style="yellow")
        return "none"

    available_targets = template.get_deployment_targets(
        deployment_agent_name, remote_config=remote_config
    )
    if not available_targets:
        raise click.ClickException(
            f"Error: No deployment targets available for agent '{deployment_agent_name}'."
        )

    if len(available_targets) == 1:
        if not quiet:
            console.print(
                f"Info: Using '{available_targets[0]}' (only available deployment target for this agent).",
                style="yellow",
            )
        return available_targets[0]
    if interactive:
        return template.prompt_deployment_target(
            deployment_agent_name,
            remote_config=remote_config,
            default_value=DEFAULT_DEPLOYMENT_TARGET,
        )
    chosen = (
        DEFAULT_DEPLOYMENT_TARGET
        if DEFAULT_DEPLOYMENT_TARGET in available_targets
        else available_targets[0]
    )
    if not quiet:
        console.print(
            f"Info: --deployment-target not specified. Defaulting to '{chosen}'.",
            style="yellow",
        )
    return chosen


def _resolve_cd(
    *,
    cd: str | None,
    prototype: bool,
    final_deployment: str,
    interactive: bool,
    auto_approve: bool,
    quiet: bool,
) -> str:
    """Resolve the CD mode.

    --prototype forces 'skip'; a 'none' target only allows 'skip' (an explicit
    other value is a usage error); otherwise the flag, a prompt, or 'skip'.
    """
    if prototype:
        if cd and cd != "skip" and not quiet:
            console.print(
                f"Info: --cd '{cd}' ignored due to --prototype (CD forced to 'skip').",
                style="yellow",
            )
        logging.debug("Prototype mode: setting cd to 'skip'")
        return "skip"
    if final_deployment != "kubernetes":
        if cd and cd != "skip":
            raise click.UsageError(
                f"--cd {cd} requires --deployment-target kubernetes "
                f"(got deployment_target={final_deployment})."
            )
        return "skip"
    if cd:
        return cd
    if interactive:
        return template.prompt_cd(DEFAULT_CD)
    if auto_approve and not quiet:
        console.print(
            "Info: --cd not specified. Defaulting to 'skip' (deploy from a workstation).",
            style="yellow",
        )
    return DEFAULT_CD


def _git_origin_owner(directory: pathlib.Path) -> str | None:
    """The owner of the git ``origin`` remote of ``directory``, lowercased, or None."""
    try:
        from graph_agents_cli._runner import run_resolved

        result = run_resolved(
            ["git", "remote", "get-url", "origin"],
            cwd=str(directory),
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as e:
        logging.debug("git remote lookup failed: %s", e)
        return None
    if result.returncode != 0 or not (result.stdout or "").strip():
        return None
    return parse_git_remote_owner(result.stdout.strip())


def parse_git_remote_owner(url: str) -> str | None:
    """Owner segment of a git remote URL (https, ssh:// or scp-like), lowercased."""
    url = url.strip()
    if url.endswith(".git"):
        url = url[: -len(".git")]
    path = None
    match = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/]+/(.+)$", url)
    if match:
        path = match.group(1)
    else:
        match = re.match(r"^[^@/]+@[^:]+:(.+)$", url)
        if match:
            path = match.group(1)
    if path is None:
        return None
    segments = [s for s in path.split("/") if s]
    if len(segments) < 2:
        return None
    owner = segments[0].lower()
    return owner or None


def _resolve_registry(
    *,
    registry: str | None,
    final_deployment: str,
    interactive: bool,
    quiet: bool,
    git_dir: pathlib.Path,
) -> str:
    """Resolve the container registry (empty when the target is none: nothing is pushed)."""
    if final_deployment != "kubernetes":
        if registry and not quiet:
            console.print(
                f"Info: --registry '{registry}' ignored for deployment_target='{final_deployment}'.",
                style="yellow",
            )
        return ""
    if registry:
        return registry.strip().rstrip("/")

    owner = _git_origin_owner(git_dir)
    detected = f"{DEFAULT_REGISTRY_HOST}/{owner}" if owner else None

    if interactive:
        value = Prompt.ask(
            "\n> Container registry (<host>/<org>)",
            default=detected or DEFAULT_REGISTRY_PLACEHOLDER,
            show_default=True,
        ).strip()
        return (value or detected or DEFAULT_REGISTRY_PLACEHOLDER).rstrip("/")

    if detected:
        if not quiet:
            console.print(
                f"Info: --registry not specified. Using '{detected}' from the git origin remote.",
                style="yellow",
            )
        return detected

    if not quiet:
        console.print(
            f"⚠️  --registry not specified and no git origin remote found; using "
            f"'{DEFAULT_REGISTRY_PLACEHOLDER}'. Set image.repository in the chart values "
            "or re-run with --registry <host>/<org>.",
            style="yellow",
        )
    return DEFAULT_REGISTRY_PLACEHOLDER


# ---------------------------------------------------------------------------
# Next steps
# ---------------------------------------------------------------------------


def _print_next_steps(
    *,
    in_folder: bool,
    destination_dir: pathlib.Path,
    project_name: str,
    output_dir: str | None,
    params: CreateParams,
) -> None:
    """Print the post-creation success banner and next-step hints."""
    if not in_folder:
        cd_path = (destination_dir / project_name) if output_dir else project_name
    else:
        cd_path = "."

    console.print("\n[bold green]✅ Success![/] Your agent project is ready.\n")

    console.print("[bold cyan]📖 Documentation[/]")
    console.print(f"   README:    [cyan]cat {cd_path}/README.md[/]")

    if params.deployment_target == "none":
        console.print(
            "\n[bold cyan]💡 Tip[/]\n"
            "   Add a deployment target later with: "
            "[cyan]graph-agents-cli scaffold enhance --deployment-target kubernetes[/]"
        )
    elif params.cd == "skip":
        console.print(
            "\n[bold cyan]💡 Tip[/]\n"
            "   Add continuous delivery later with: "
            "[cyan]graph-agents-cli scaffold enhance --cd argocd|helm-push[/]"
        )

    console.print("\n[bold cyan]🚀 Get Started[/]")
    console.print(f"   [bold bright_green]cd {cd_path}[/]")
    console.print("   [bold bright_green]graph-agents-cli install[/]")
    console.print("   [bold bright_green]graph-agents-cli playground[/]")
    console.print("   [bold bright_green]graph-agents-cli eval run[/]")
    if params.deployment_target == "kubernetes":
        console.print("   [bold bright_green]graph-agents-cli deploy --env dev[/]")
    if params.registry == DEFAULT_REGISTRY_PLACEHOLDER:
        console.print(
            f"\n   [yellow]Replace '{DEFAULT_REGISTRY_PLACEHOLDER}' in the chart values before "
            "deploying.[/]"
        )


# Kept importable for callers that referenced the old constant name.
GUIDANCE_FILENAME_DEFAULT = DEFAULT_AGENT_GUIDANCE_FILENAME
