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

"""Root Click group for the 'graph-agents-cli' CLI.

Every command is registered lazily via `add_lazy_command`. The command
modules are imported only when the user invokes the command (or asks for
its specific --help). See `LazyGroup` in `_click.py`.

The startup path must import no agent framework, model SDK, or Kubernetes
client: `tests/test_startup_imports.py` enforces that (a fast, light startup).
"""

from __future__ import annotations

import io
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import click

from graph_agents_cli.__init__ import __version__
from graph_agents_cli._click import LazyGroup, patch_source_in_help
from graph_agents_cli._project import find_project_root, is_project_moved
from graph_agents_cli._runner import DISABLE_OVERRIDES_ENV

# Type-only: nothing under `extension.` may be imported at module scope. A run
# that bypasses overrides (GRAPH_AGENTS_CLI_DISABLE_OVERRIDES=1 — set for any
# command an extension re-enters) must not load extension discovery at all.
if TYPE_CHECKING:
    from graph_agents_cli.extension._loader import ExtensionSet, ResolvedCommand

# Force utf-8 encoding and non-exception fallback for printing
if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if isinstance(sys.stderr, io.TextIOWrapper):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

NO_UPDATE_CHECK_ENV = "GRAPH_AGENTS_CLI_NO_UPDATE_CHECK"
# Set to 1 to see the traceback behind a one-line network/file/parse error.
DEBUG_ENV = "GRAPH_AGENTS_CLI_DEBUG"

# Exit codes: 0 ok, 1 refused or failed gate, 2 tool failure, 3 configuration error.
EXIT_TOOL_FAILURE = 2
EXIT_CONFIG_ERROR = 3


def _print_is_project_moved_tip() -> None:
    message = (
        "\n💡 Tip: It looks like the project folder may have been moved or renamed."
        " Try running `graph-agents-cli install --clean` to reset the environment, then"
        " re-run your original command"
    )
    if is_project_moved():
        from graph_agents_cli._output import Console

        Console(stderr=True).print(message, style="cyan")


class _MainGroup(LazyGroup):
    """Click group with lazy command loading and full-traceback exception handling."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._extensions_applied = False

    def list_commands(self, ctx):
        self._apply_extensions()
        return super().list_commands(ctx)

    def get_command(self, ctx, cmd_name):
        self._apply_extensions()
        return super().get_command(ctx, cmd_name)

    def _apply_extensions(self) -> None:
        if self._extensions_applied:
            return
        self._extensions_applied = True
        if os.environ.get(DISABLE_OVERRIDES_ENV) == "1":
            return

        from graph_agents_cli.extension._loader import load_extension_set
        from graph_agents_cli.extension._paths import user_config_root

        project_root = find_project_root(Path.cwd())
        user_root = user_config_root()
        extension_set = load_extension_set(project_root, user_root)

        for inc in extension_set.incompatible:
            if inc.error_on_incompatible:
                logging.warning(
                    "graph-agents-cli: extension %r requires graph-agents-cli %s but running %s; "
                    "its commands will fail until you run `graph-agents-cli extension "
                    "update %s`, remove it, or pin a compatible CLI.",
                    inc.name,
                    inc.requires_agents_cli,
                    extension_set.running_version,
                    inc.name,
                )
            else:
                logging.warning(
                    "graph-agents-cli: extension %r requires graph-agents-cli %s but running %s; "
                    "applying anyway (may misbehave).",
                    inc.name,
                    inc.requires_agents_cli,
                    extension_set.running_version,
                )

        if not extension_set.commands:
            return

        applied: list[str] = []
        for name, resolved in extension_set.commands.items():
            if self._install(name, resolved):
                applied.append(name)
            applied.extend(self._mirror_onto_aliases(name, resolved, extension_set))

        if applied:

            def _detail(name: str) -> str:
                r = extension_set.commands.get(name)
                if r is None:
                    return name
                state = " BLOCKED" if r.blocked_requires else ""
                return f"{name} [{r.extension_name}/{r.scope}{state}]"

            details = [_detail(n) for n in sorted(applied)]
            # Logged at WARNING so the takeover is visible by default: auto-loaded
            # project-scope extensions run with the same trust as the repo you're in.
            logging.warning(
                "graph-agents-cli: applying %d extension command(s): %s",
                len(applied),
                ", ".join(details),
            )

    def _mirror_onto_aliases(
        self, dotted: str, resolved: ResolvedCommand, extension_set: ExtensionSet
    ) -> list[str]:
        """Apply an override of `group.sub` to top-level aliases of the same command.

        `create` and `scaffold create` are two registrations of one command
        (identical `module:obj` target), so overriding the canonical
        `scaffold.create` must also take over `create`.
        """
        if "." not in dotted:
            return []
        group_name, sub_name = dotted.split(".", 1)
        parent = self._load_builtin(group_name)
        if not isinstance(parent, LazyGroup):
            return []
        entry = parent._lazy_commands.get(sub_name)
        if entry is None:
            return []
        mirrored: list[str] = []
        for alias, (target, _short_help) in self._lazy_commands.items():
            if target != entry[0] or alias in extension_set.commands:
                continue
            if self._install(alias, resolved):
                mirrored.append(alias)
        return mirrored

    def _builtin_names(self) -> set[str]:
        return set(self._lazy_commands) | set(self.commands)

    def _load_builtin(self, name: str):
        import importlib

        loaded = self.commands.get(name)
        if loaded is not None:
            return loaded
        entry = self._lazy_commands.get(name)
        if entry is None:
            return None
        module_path, attr = entry[0].split(":")
        try:
            return getattr(importlib.import_module(module_path), attr)
        except Exception as e:  # import failure shouldn't crash extension application
            logging.warning("Could not load built-in %r for override check: %s", name, e)
            return None

    def _install(self, name: str, resolved: ResolvedCommand) -> bool:
        """Install one command; dotted names (group.sub) target a subcommand."""
        if "." in name:
            return self._install_subcommand(name, resolved)
        return self._install_top_level(name, resolved)

    def _synthesize(self, resolved: ResolvedCommand, *, leaf: str, display: str):
        from graph_agents_cli.extension._overrides import (
            make_blocked_command,
            make_override_command,
        )

        if resolved.blocked_requires is not None:
            return make_blocked_command(
                extension_name=resolved.extension_name,
                leaf_name=leaf,
                display_path=display,
                requires=resolved.blocked_requires,
                running=__version__,
            )
        return make_override_command(
            resolved.contribution,
            extension_name=resolved.extension_name,
            scope=resolved.scope,
            leaf_name=leaf,
            display_path=display,
            extension_root=resolved.extension_root,
        )

    def _install_top_level(self, name: str, resolved: ResolvedCommand) -> bool:
        is_builtin = name in self._builtin_names()
        if resolved.contribution.kind == "add" and is_builtin:
            logging.warning(
                "Extension %r tried to add command %r, which is a built-in; "
                "use commands.override instead. Skipping.",
                resolved.extension_name,
                name,
            )
            return False
        if resolved.contribution.kind == "override" and not is_builtin:
            logging.warning(
                "Extension %r overrides unknown command %r; skipping.",
                resolved.extension_name,
                name,
            )
            return False
        if is_builtin and isinstance(self._load_builtin(name), click.Group):
            logging.warning(
                "Extension %r cannot override command group %r; override a "
                "subcommand instead (e.g. %s.<sub>). Skipping.",
                resolved.extension_name,
                name,
                name,
            )
            return False
        self._overrides[name] = self._synthesize(resolved, leaf=name, display=name)
        return True

    def _install_subcommand(self, dotted: str, resolved: ResolvedCommand) -> bool:
        parts = dotted.split(".")
        if len(parts) != 2:
            logging.warning(
                "Extension %r: only single-level subcommand overrides "
                "(group.sub) are supported in v1; skipping %r.",
                resolved.extension_name,
                dotted,
            )
            return False
        group_name, sub_name = parts
        if group_name not in self._builtin_names():
            logging.warning(
                "Extension %r: parent command %r is not a built-in; skipping %r.",
                resolved.extension_name,
                group_name,
                dotted,
            )
            return False
        parent = self._load_builtin(group_name)
        if not isinstance(parent, LazyGroup):
            logging.warning(
                "Extension %r: %r is not a command group; skipping %r.",
                resolved.extension_name,
                group_name,
                dotted,
            )
            return False
        sub_known = sub_name in parent._lazy_commands or sub_name in parent.commands
        if resolved.contribution.kind == "override" and not sub_known:
            logging.warning(
                "Extension %r: %r has no subcommand %r; skipping.",
                resolved.extension_name,
                group_name,
                sub_name,
            )
            return False
        if resolved.contribution.kind == "add" and sub_known:
            logging.warning(
                "Extension %r: subcommand %r already exists; use override. Skipping.",
                resolved.extension_name,
                dotted,
            )
            return False
        parent._overrides[sub_name] = self._synthesize(
            resolved, leaf=sub_name, display=f"{group_name} {sub_name}"
        )
        return True

    def invoke(self, ctx: click.Context) -> None:
        try:
            super().invoke(ctx)
        except click.exceptions.Exit:
            raise
        except click.ClickException:
            click.echo(f"graph-agents-cli v{__version__}", err=True)
            _print_is_project_moved_tip()
            raise
        except click.Abort:
            # A declined prompt (or Ctrl-C/EOF inside click.prompt/confirm) is
            # a RuntimeError, not a ClickException: let Click's standalone
            # handler print "Aborted!" and exit 1 instead of a traceback.
            raise
        except KeyboardInterrupt as exc:
            from graph_agents_cli._output import Console

            console = Console(stderr=True)
            console.print(f"\ngraph-agents-cli v{__version__}", style="dim")
            # A SIGTERM/SIGHUP turned into an interrupt (run/_signals.py) exits
            # with 128 + its signal number, like a process the signal ended.
            exit_code = getattr(exc, "exit_code", 130)
            reason = "terminated by a signal" if exit_code != 130 else "cancelled by user"
            console.print(f"Operation {reason}", style="yellow")
            ctx.exit(exit_code)
        except Exception as exc:
            click.echo(f"graph-agents-cli v{__version__}", err=True)
            _print_is_project_moved_tip()
            known = _environment_error(exc)
            if known is not None and os.environ.get(DEBUG_ENV) != "1":
                # A network, file-system or parse problem is the user's to fix:
                # one line, not a traceback (which DEBUG_ENV=1 still shows).
                message, code = known
                click.echo(f"Error: {message}", err=True)
                click.echo(f"  (set {DEBUG_ENV}=1 to see the traceback)", err=True)
                ctx.exit(code)
            # Anything else is a bug in the CLI: keep the traceback for the report.
            traceback.print_exc()
            ctx.exit(EXIT_TOOL_FAILURE)

    def main(self, *args, **kwargs):  # type: ignore[override]
        _configure_logging()
        return super().main(*args, **kwargs)


def _environment_error(exc: BaseException) -> tuple[str, int] | None:
    """``(one-line message, exit code)`` for an error the user's environment caused, else None.

    Network failures and OS errors are tool failures (exit 2); unparseable YAML
    or JSON is a configuration error (exit 3). Library-specific classes are
    matched without importing the library, which keeps startup light.
    """
    import json

    import yaml

    name = type(exc).__name__
    first_line = (str(exc).strip().splitlines() or [""])[0][:300]
    detail = f"{name}: {first_line}" if first_line else name
    if isinstance(exc, yaml.YAMLError):
        mark = getattr(exc, "problem_mark", None)
        where = f" in {mark.name} (line {mark.line + 1})" if mark is not None else ""
        problem = getattr(exc, "problem", None) or first_line
        return f"invalid YAML{where}: {problem}", EXIT_CONFIG_ERROR
    if isinstance(exc, json.JSONDecodeError):
        return f"invalid JSON: {first_line}", EXIT_CONFIG_ERROR
    modules = {cls.__module__.split(".")[0] for cls in type(exc).__mro__}
    lowered = name.lower()
    if (
        isinstance(exc, ConnectionError)
        or modules & {"httpx", "httpcore", "requests", "urllib3"}
        or "connection" in lowered
    ):
        return f"network error: {detail}", EXIT_TOOL_FAILURE
    if isinstance(exc, TimeoutError) or "timeout" in lowered:
        return f"timed out: {detail}", EXIT_TOOL_FAILURE
    if isinstance(exc, OSError):
        return detail, EXIT_TOOL_FAILURE
    return None


class _CliLogHandler(logging.Handler):
    """Log records as ``Warning: ...`` / ``Error: ...`` on stderr (not ``WARNING:root:``)."""

    _PREFIX: ClassVar[dict[int, str]] = {
        logging.WARNING: "Warning: ",
        logging.ERROR: "Error: ",
        logging.CRITICAL: "Error: ",
    }

    def emit(self, record: logging.LogRecord) -> None:
        try:
            click.echo(self._PREFIX.get(record.levelno, "") + self.format(record), err=True)
        except Exception:
            self.handleError(record)


def _configure_logging() -> None:
    """Give the root logger a clean stderr handler unless something configured it already.

    Without one, the first ``logging.warning`` falls back to ``basicConfig`` and
    prints ``WARNING:root:...``. A ``--debug`` flag (``basicConfig(force=True)``)
    or a test harness that installed its own handler wins.
    """
    root = logging.getLogger()
    if root.handlers:
        return
    handler = _CliLogHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.WARNING)


@click.group(cls=_MainGroup)
@click.version_option(version=__version__, prog_name="graph-agents-cli")
def main():
    """Graph Agents CLI — LangGraph agents on Kubernetes.

    Build, evaluate, and deploy LangGraph agents with a single unified CLI.

    \b
    Quick start:
      graph-agents-cli setup                 Install skills to your coding agent
      graph-agents-cli create my-agent       Create a new agent project
      graph-agents-cli playground            Start the local playground
      graph-agents-cli eval run              Run the agent over the eval dataset and grade it
      graph-agents-cli deploy --env dev      Deploy to the current Kubernetes context
    """
    # Update and skills-version checks are opt-out for disconnected installs.
    if os.environ.get(NO_UPDATE_CHECK_ENV) == "1":
        return
    from graph_agents_cli._skills_check import check_skills_version
    from graph_agents_cli.scaffold.utils.version import display_update_message

    display_update_message()
    check_skills_version()


# Setup commands
main.add_lazy_command(
    "setup",
    "graph_agents_cli.setup.cmd_setup:cmd_setup",
    "Install graph-agents-cli and skills to detected coding agents.",
)
main.add_lazy_command(
    "update",
    "graph_agents_cli.setup.cmd_update:cmd_update",
    "Force reinstall skills to all detected coding agents.",
)

# Preflight / login
main.add_lazy_command(
    "login",
    "graph_agents_cli.setup.cmd_auth:cmd_login",
    "Check model provider keys, LangSmith, and kubeconfig; optionally write .env.",
)

# Scaffold command group + top-level `create` alias
main.add_lazy_command(
    "scaffold",
    "graph_agents_cli.scaffold.cmd_scaffold_group:scaffold_group",
    "Scaffold, enhance, and upgrade agent projects.",
)
main.add_lazy_command(
    "create",
    "graph_agents_cli.scaffold.commands.create:create",
    "Create a LangGraph agent project from a template.",
)

# Dev commands
main.add_lazy_command(
    "playground",
    "graph_agents_cli.dev.cmd_playground:cmd_playground",
    "Start the application locally with reload and the dev chat page.",
)
main.add_lazy_command(
    "run",
    "graph_agents_cli.run.cmd_run:cmd_run",
    "Run the agent with a single prompt (non-interactive).",
)
main.add_lazy_command(
    "lint",
    "graph_agents_cli.dev.cmd_lint:cmd_lint",
    "Run code quality checks and the API-policy check.",
)
main.add_lazy_command(
    "install",
    "graph_agents_cli.dev.cmd_install:cmd_install",
    "Install project dependencies.",
)
main.add_lazy_command(
    "build",
    "graph_agents_cli.dev.cmd_build:cmd_build",
    "Build the agent container image.",
)

# Eval commands
main.add_lazy_command(
    "eval",
    "graph_agents_cli.eval.cmd_eval_group:eval_group",
    "Evaluate agents and compare results.",
)

# Deploy, secrets, infra
main.add_lazy_command(
    "deploy",
    "graph_agents_cli.deploy.cmd_deploy:cmd_deploy",
    "Deploy the agent to Kubernetes (mode depends on the project's CD setting).",
)
main.add_lazy_command(
    "secrets",
    "graph_agents_cli.secrets.cmd_secrets:secrets_group",
    "Provision and inspect the application Secret per environment.",
)
main.add_lazy_command(
    "infra",
    "graph_agents_cli.infra.cmd_infra:infra_group",
    "Check cluster and repository prerequisites (read-only).",
)

# Extension commands
main.add_lazy_command(
    "extension",
    "graph_agents_cli.extension.cmd_extension_group:extension_group",
    "Manage graph-agents-cli extensions (experimental).",
)

# Info command
main.add_lazy_command(
    "info",
    "graph_agents_cli.info.cmd_info:cmd_info",
    "Show project configuration, paths, and CLI version.",
)

# Patch the root group itself to show source file in --help.
patch_source_in_help(main)


if __name__ == "__main__":
    main()
