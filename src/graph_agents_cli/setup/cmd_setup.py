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

"""graph-agents-cli setup command — install the CLI and skills via npx skills.

Skills install ladder (each step is tried only when the previous one is unavailable):

1. ``npx skills add <source>`` from ``--skills-source``, else this repository
   at the running release's tag (``<repo>#v<version>``; the default branch for a
   development build), so the skills match the CLI (needs ``git`` and network).
2. ``npx skills add <bundled dir>`` from the skills shipped inside the wheel
   (needs ``npx`` only).
3. Copy the bundled skills straight into ``~/.agents/skills`` (or
   ``./.agents/skills`` with ``--workspace``) when ``npx`` is unavailable.

No authentication step: the CLI stores no credentials; run
``graph-agents-cli login`` for the preflight instead.

The CLI itself is installed from ``install_spec()`` (a pinned git reference to
the GitHub repository, or ``GRAPH_AGENTS_CLI_INSTALL_SPEC``).
"""

from __future__ import annotations

import random
import shlex
import shutil
from pathlib import Path

import click

from graph_agents_cli._runner import run
from graph_agents_cli._skills_check import SKILLS_NPX_PACKAGE
from graph_agents_cli._tools import default_skills_source, run_npx_skills
from graph_agents_cli.skills._bundle import (
    SKILL_BUNDLE_DIR,
    get_bundled_skills_dir,
    is_skill_dir,
)

PACKAGE_NAME = "graph-agents-cli"

_MOTTOS = [
    "Give your coding agent the power to build LangGraph agents.",
    "From prototype to your own cluster — one CLI away.",
    "Skills up. Ship faster.",
    "Agent skills, installed in seconds.",
    "Your coding agent just got an upgrade.",
]


def _print_logo():
    """Print the GRAPH AGENTS CLI banner with a random motto."""
    click.secho(
        " █▀▀ █▀█ █▀█ █▀█ █ █   █▀█ █▀▀ █▀▀ █▄ █ ▀█▀ █▀   █▀▀ █  █",
        fg="blue",
        bold=True,
    )
    click.secho(
        " █▄█ █▀▄ █▀█ █▀▀ █▀█   █▀█ █▄█ ██▄ █ ▀█  █  ▄█   █▄▄ █▄ █",
        fg="cyan",
        bold=True,
    )
    click.echo()
    click.echo(f" {random.choice(_MOTTOS)}")


def _print_section(number, title):
    """Print a numbered section header."""
    click.echo()
    click.secho(f" {number}. {title}", bold=True)
    click.echo(f" {'─' * (len(title) + 3)}")


def _get_source_root():
    """Return the cwd if it is the graph-agents-cli repo root, else None.

    Checks that a ``pyproject.toml`` with the ``graph-agents-cli`` package
    name exists in the current working directory.
    """
    candidate = Path.cwd() / "pyproject.toml"
    if candidate.is_file():
        try:
            text = candidate.read_text(encoding="utf-8")
            if f'name = "{PACKAGE_NAME}"' in text:
                return Path.cwd()
        except OSError:
            pass
    return None


def _build_skills_args(source, *, workspace, agent):
    """Build the ``npx skills add`` argument list for ``source``."""
    args = ["add", source, "-y"]
    if "all" in agent:
        args.append("--all")
    elif agent:
        for a in agent:
            args.extend(["--agent", a])
    if not workspace:
        args.append("-g")
    return args


def _skills_store_dir(*, workspace):
    """Return the canonical skills store (global home or workspace-relative)."""
    base = Path.cwd() if workspace else Path.home()
    return base / ".agents" / "skills"


def _display_path(path):
    """Render ``path`` with the home directory collapsed to ``~`` for output."""
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


def _copy_bundled_skills(bundled_dir, dest_root):
    """Copy every bundled skill into ``dest_root``.

    Returns the number of skills copied. Existing copies are replaced so the
    store always reflects the version shipped with this CLI.
    """
    dest_root.mkdir(parents=True, exist_ok=True)
    count = 0
    for skill in sorted(bundled_dir.iterdir()):
        if not is_skill_dir(skill):
            continue
        target = dest_root / skill.name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(skill, target)
        count += 1
    return count


def _install_skills(remote_source, *, workspace, agent, allow_fallback):
    """Install skills, falling back progressively when the remote path fails.

    1. ``npx skills add`` from ``remote_source`` (the GitHub repo by default,
       which requires ``git`` and network access).
    2. ``npx skills add`` from the skills bundled with this CLI (no ``git`` or
       network needed).
    3. Copy the bundled skills straight into the ``.agents/skills`` store and
       warn the user.

    Fallbacks (2 and 3) are only attempted when ``allow_fallback`` is true — an
    explicit ``--skills-source`` disables them so the user's chosen source is
    never silently swapped for the bundled copy.

    Returns the strategy that succeeded: ``"remote"``, ``"bundled"`` or ``"copy"``.
    """

    args = _build_skills_args(remote_source, workspace=workspace, agent=agent)
    bundled_dir = get_bundled_skills_dir() if allow_fallback else None

    # Remote (or explicitly requested) source
    try:
        run_npx_skills(args, "Installing skills")
        return "remote"
    except click.ClickException:
        if bundled_dir is None:
            raise
        click.secho(f"  Could not install skills from {remote_source}.", fg="yellow")
        click.secho("  Falling back to the skills bundled with this CLI.", dim=True)

    # Bundled skills via npx (skips a redundant identical run)
    if bundled_dir.resolve() != Path(remote_source).resolve():
        local_args = _build_skills_args(str(bundled_dir), workspace=workspace, agent=agent)
        try:
            run_npx_skills(local_args, "Installing bundled skills")
            return "bundled"
        except click.ClickException:
            click.secho("  Could not install bundled skills via npx skills.", fg="yellow")

    # Copy the bundled skills directly (no npx / git needed)
    dest_root = _skills_store_dir(workspace=workspace)
    copied = _copy_bundled_skills(bundled_dir, dest_root)
    click.secho(
        f"  ⚠️  Installed {copied} skills by copying them into {_display_path(dest_root)} "
        "(npx skills was unavailable).",
        fg="yellow",
    )
    click.secho(
        f"     '{PACKAGE_NAME} update' may not manage these copied skills; re-run "
        f"'{PACKAGE_NAME} setup' once git and npx are available.",
        dim=True,
    )
    return "copy"


def _resolve_skills_source(skills_source, *, dev):
    """Pick the skills source from the flags.

    ``--skills-source`` wins; ``--dev`` uses the checkout's bundled copy;
    otherwise this repository at the tag of the running release
    (``default_skills_source``; the default branch for a development build).
    Local paths are made absolute so ``npx skills`` sees them regardless of its
    own cwd; remote references are left untouched.
    """
    if skills_source:
        source_path = Path(skills_source)
        # Only resolve to an absolute path if the source is a local file/directory
        # to prevent resolving remote URIs (e.g., GitHub URLs or package identifiers).
        if skills_source.startswith((".", "/")) or source_path.exists():
            return str(source_path.resolve())
        return skills_source
    if dev:
        # In dev mode, use skills from the local repo checkout
        return str(SKILL_BUNDLE_DIR)
    from graph_agents_cli.scaffold.utils.version import get_current_version

    return default_skills_source(get_current_version())


def _cli_install_args(*, dev):
    """Return the ``uv tool install`` argv for the CLI itself.

    Outside ``--dev`` the CLI installs from ``install_spec()`` pinned to the
    running version, so ``setup`` reproduces the CLI the user is running.
    """
    if dev:
        project_root = _get_source_root()
        if not project_root:
            raise click.ClickException(
                f"--dev requires running from the root of the {PACKAGE_NAME} repository"
            )
        return ["uv", "tool", "install", "--force", "--editable", str(project_root)]
    from graph_agents_cli.scaffold.utils.version import get_current_version, install_spec

    return ["uv", "tool", "install", install_spec(get_current_version())]


@click.command("setup")
@click.option(
    "--workspace",
    is_flag=True,
    default=False,
    help=(
        "Install to project/workspace scope instead of global. "
        "Skills are installed relative to the current directory."
    ),
)
@click.option(
    "--dry-run",
    "--dryrun",
    is_flag=True,
    default=False,
    help="Show what would be done without making changes.",
)
@click.option(
    "--dev",
    is_flag=True,
    default=False,
    help="Install as editable from the local repo (for contributors).",
)
@click.option(
    "--skills-source",
    default=None,
    help=(
        "Skills source: local path, GitHub owner/repo, or URL (default: this repository at "
        "the running release's tag; no fallback to the bundled copy)."
    ),
)
@click.option(
    "--agent",
    multiple=True,
    help=(
        "Specify the agent to install skills to (e.g. --agent claude-code --agent cursor). "
        "Use 'all' to install for all supported agents."
    ),
)
def cmd_setup(*, workspace, dry_run, dev, skills_source, agent):
    """Install graph-agents-cli and skills to detected coding agents.

    Installs the graph-agents-cli tool (via uv tool install) and detects
    installed coding agents (Claude Code, Antigravity, Codex, Gemini CLI,
    Cursor, etc.) to install the LangGraph development skills via npx skills.
    The skills come from this repository at the tag of the running release
    (the default branch for a development build), falling back to the copy
    bundled with the CLI when that fails.

    By default, skills are installed globally for all detected agents.
    Use --workspace to install at the project level instead.
    Use --agent to specify specific coding agents (e.g. --agent claude-code --agent cursor) or 'all'.
    Use --dry-run to preview what would happen without executing.
    Use --dev to install graph-agents-cli as editable from the local repo (for contributors).

    This command stores no credentials; run 'graph-agents-cli login' to check
    provider keys and kubeconfig.
    """
    click.echo("Setting up...")
    click.echo()
    _print_logo()

    scope = "workspace" if workspace else "global"
    source = _resolve_skills_source(skills_source, dev=dev)
    args = _build_skills_args(source, workspace=workspace, agent=agent)

    # ── Dry Run ──
    if dry_run:
        _print_section(1, "Dry Run")
        click.echo()
        tool_args = _cli_install_args(dev=dev)
        click.echo(f"  Would install {PACKAGE_NAME}{' (editable)' if dev else ''}:")
        click.secho(f"  ▸ {shlex.join(tool_args)}", fg="cyan", dim=True)
        click.echo()
        click.echo("  Would install skills:")
        full_args = ["npx", "-y", SKILLS_NPX_PACKAGE, *args]
        click.secho(f"  ▸ {shlex.join(full_args)}", fg="cyan", dim=True)
        if not skills_source:
            click.secho(
                f"    (falls back to the bundled skills, then to a copy into "
                f"{_display_path(_skills_store_dir(workspace=workspace))})",
                dim=True,
            )
        click.echo(f"  Scope: {scope}")
        # Temporary compatibility step (see TODO at the real linking call below).
        if not workspace and (Path.home() / ".gemini").is_dir():
            click.echo(
                "  Would link global skills into Antigravity's skill directories "
                "(~/.gemini/config/skills, ~/.gemini/antigravity-cli/skills)."
            )
        click.echo()
        click.secho("  No changes made (dry run).", fg="yellow")
        click.echo()
        return

    step = 1

    # ── CLI Installation ──
    _print_section(step, "CLI Installation")
    step += 1
    click.echo()
    cli_installed = False
    tool_args = _cli_install_args(dev=dev)
    result = run(tool_args, capture=True, check=False)
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    if "already installed" in stdout.lower() or "already installed" in stderr.lower():
        from graph_agents_cli.scaffold.utils.version import check_for_updates

        needs_update, current, latest = check_for_updates()
        if needs_update:
            click.secho(
                f"  Installed ({current}), but {latest} is available.",
                fg="yellow",
            )
            click.secho(
                f"  Run '{PACKAGE_NAME} update' to update.",
                dim=True,
            )
        else:
            click.secho("  Already installed and up to date.", dim=True)
        cli_installed = True
    elif result.returncode != 0:
        click.secho(f"  Could not install {PACKAGE_NAME} automatically.", fg="yellow")
        if stderr.strip():
            for line in stderr.strip().splitlines():
                click.echo(f"  {line}")
        click.secho(f"  Install manually: {shlex.join(tool_args)}", dim=True)
    else:
        for line in stdout.strip().splitlines():
            click.echo(f"  {line}")
        cli_installed = True

    # ── Skills Installation ──
    _print_section(step, "Skills Installation")
    step += 1
    click.echo()

    strategy = _install_skills(
        source,
        workspace=workspace,
        agent=agent,
        allow_fallback=not skills_source,
    )

    # ── Antigravity skill links ──
    # Temporary until npx skills supports Antigravity's IDE / CLI / 2.0 paths:
    # npx installs global skills to ~/.agents/skills, which Antigravity does not
    # read, so mirror them into the locations the IDE/2.0 and CLI look in.
    # TODO: remove once Antigravity/npx align on skill paths.
    if not workspace:
        from graph_agents_cli.setup._antigravity import link_skills_for_antigravity

        for line in link_skills_for_antigravity():
            click.echo(f"  {line}")

    # ── Summary ──
    _print_section(step, "Summary")
    click.echo()

    # CLI tool status
    if cli_installed:
        if dev:
            click.echo(f"  CLI:    {PACKAGE_NAME} installed (editable)")
        else:
            click.echo(f"  CLI:    {PACKAGE_NAME} installed")
    else:
        click.echo(f"  CLI:    Not installed (run: {shlex.join(tool_args)})")

    # Skills status
    strategy_label = {
        "remote": "Installed",
        "bundled": "Installed (bundled copy)",
        "copy": "Installed (copied; npx unavailable)",
    }[strategy]
    click.echo(f"  Skills: {strategy_label}")
    click.echo(f"  Scope:  {scope}")
    click.echo(f"  Next:   {PACKAGE_NAME} login   (checks provider keys and kubeconfig)")

    click.echo()
    click.secho("  Done.", fg="green", bold=True)
    click.echo()
