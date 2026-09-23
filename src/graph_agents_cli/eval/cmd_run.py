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

"""graph-agents-cli eval run command: generate then grade, worst exit code wins."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click

from graph_agents_cli._output import Console
from graph_agents_cli._project import find_project_root
from graph_agents_cli._remote import deprecated_session_token, fold_session_token
from graph_agents_cli.eval import _paths
from graph_agents_cli.eval._client import DEFAULT_TIMEOUT
from graph_agents_cli.eval._common import (
    EXIT_CONFIG_ERROR,
    EXIT_INCOMPLETE,
    EvalConfigError,
    worst_exit_code,
)
from graph_agents_cli.eval.cmd_generate import DEFAULT_CONCURRENCY, generate_traces
from graph_agents_cli.eval.cmd_grade import DEFAULT_JUDGE_TIMEOUT, grade_traces

if TYPE_CHECKING:
    from graph_agents_cli.extension._loader import ResolvedCommand


def _run_override_code(resolved: ResolvedCommand, argv: list[str], *, display_path: str) -> int:
    """Run an override's vector and return its exit code (never exits here).

    ``eval run`` must combine both stages' codes, so unlike
    ``_overrides.run_override`` a non-zero child code is returned, not raised.
    """
    from graph_agents_cli._runner import run_extension_command

    cwd = find_project_root() or Path.cwd()
    try:
        return run_extension_command(
            resolved.contribution.run,
            argv,
            cwd=cwd,
            extension_root=resolved.extension_root,
        )
    except FileNotFoundError as exc:
        raise click.ClickException(
            f"Extension '{resolved.extension_name}' command {display_path!r} could not run: "
            f"executable {resolved.contribution.run[0]!r} not found. "
            "Check the extension's `run:` entry."
        ) from exc


def _generate_override_argv(
    project_root: Path,
    *,
    dataset: str | None,
    traces_file: Path,
    url: str | None,
    app_name: str | None,
    concurrency: int,
    header: tuple[str, ...],
    cookie: tuple[str, ...],
    session_token: str | None,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[str]:
    # The override is a separate process, so the default dataset is resolved
    # here and passed absolute (run_extension_command forces cwd=project root).
    files = _paths.resolve_input_datasets(project_root, dataset)
    if not files:
        raise EvalConfigError(
            f"no dataset found: pass --dataset PATH or add {_paths.DEFAULT_INPUT_DATASET}"
        )
    dataset_arg = str(files[0].resolve()) if len(files) == 1 else str(files[0].parent.resolve())
    argv = ["--dataset", dataset_arg, "--output", str(traces_file)]
    if url:
        argv += ["--url", url]
    if app_name:
        argv += ["--app-name", app_name]
    if concurrency != DEFAULT_CONCURRENCY:
        argv += ["--concurrency", str(concurrency)]
    if timeout != DEFAULT_TIMEOUT:
        # Forwarded like --concurrency: only a non-default value, so an override
        # that does not know the flag keeps working at the defaults.
        argv += ["--timeout", format(timeout, "g")]
    for value in header:
        argv += ["--header", value]
    for value in cookie:
        argv += ["--cookie", value]
    if session_token:
        argv += ["--session-token", session_token]
    return argv


def _grade_override_argv(
    *,
    traces_file: Path,
    dataset: str | None,
    config_path: str | None,
    output_path: str | None,
    judge_provider: str | None,
    judge_model: str | None,
    judge_timeout: int = DEFAULT_JUDGE_TIMEOUT,
) -> list[str]:
    argv = ["--traces", str(traces_file)]
    if dataset:
        argv += ["--dataset", str(Path(dataset).resolve())]
    if config_path:
        argv += ["--config", str(Path(config_path).resolve())]
    if output_path:
        argv += ["--output", output_path]
    if judge_provider:
        argv += ["--judge-provider", judge_provider]
    if judge_model:
        argv += ["--judge-model", judge_model]
    if judge_timeout != DEFAULT_JUDGE_TIMEOUT:
        argv += ["--judge-timeout", str(judge_timeout)]
    return argv


@click.command("run")
@click.option(
    "--dataset", default=None, help="Dataset file or directory. Forwarded to `eval generate`."
)
@click.option(
    "--url", default=None, help="Base URL of a running agent. Forwarded to `eval generate`."
)
@click.option(
    "--concurrency",
    type=click.IntRange(min=1),
    default=DEFAULT_CONCURRENCY,
    show_default=True,
    help="Cases run in parallel. Forwarded to `eval generate`.",
)
@click.option("--header", "-H", multiple=True, help="Extra HTTP header 'Key: Value' (repeatable).")
@click.option("--cookie", multiple=True, help="Cookie 'name=value' (repeatable).")
@click.option(
    "--session-token",
    default=None,
    hidden=True,
    callback=deprecated_session_token,
    help="Deprecated alias of --header 'X-Session-Token: ...'.",
)
@click.option("--app-name", default=None, help="Agent name recorded in the traces.")
@click.option(
    "--timeout",
    type=click.FloatRange(min=1),
    default=DEFAULT_TIMEOUT,
    show_default=True,
    help="Seconds allowed per chat call. Forwarded to `eval generate`.",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(),
    default=None,
    help="Eval config. Forwarded to `eval grade`.",
)
@click.option(
    "--output",
    "-o",
    "output_path",
    default=None,
    help="Results file or directory. Forwarded to `eval grade`.",
)
@click.option(
    "--judge-provider", default=None, help="Override the judge provider. Forwarded to `eval grade`."
)
@click.option(
    "--judge-model", default=None, help="Override the judge model. Forwarded to `eval grade`."
)
@click.option(
    "--judge-timeout",
    type=click.IntRange(min=1),
    default=DEFAULT_JUDGE_TIMEOUT,
    show_default=True,
    help="Seconds allowed for the judge runner. Forwarded to `eval grade`.",
)
def cmd_run(
    *,
    dataset: str | None,
    url: str | None,
    concurrency: int,
    header: tuple[str, ...],
    cookie: tuple[str, ...],
    session_token: str | None,
    app_name: str | None,
    timeout: float,
    config_path: str | None,
    output_path: str | None,
    judge_provider: str | None,
    judge_model: str | None,
    judge_timeout: int,
) -> None:
    """Chain `eval generate` and `eval grade` in one command.

    Traces go to a fresh artifacts/traces/traces_<ts>.json and are graded
    immediately. An extension that overrides `eval generate` or `eval grade`
    is dispatched exactly as the standalone command would be. The exit code
    is the worse of the two stages (0 gate met, 1 gate failed, 2 incomplete,
    3 configuration error).
    """
    console = Console()
    # The deprecated --session-token is an X-Session-Token header from here on,
    # so an eval.generate override is handed --header only.
    header = fold_session_token(header, session_token)
    session_token = None
    project_root = find_project_root()
    if project_root is None:
        raise EvalConfigError(
            "not inside a graph-agents-cli project (no graph-agents-cli-manifest.yaml found)"
        )
    traces_file = _paths.default_traces_path(project_root)

    from graph_agents_cli.extension._overrides import installed_override

    console.rule("[bold]Step 1/2: eval generate[/bold]")
    generate_override = installed_override("eval.generate")
    if generate_override is not None:
        argv = _generate_override_argv(
            project_root,
            dataset=dataset,
            traces_file=traces_file,
            url=url,
            app_name=app_name,
            concurrency=concurrency,
            header=header,
            cookie=cookie,
            session_token=session_token,
            timeout=timeout,
        )
        generate_code = _run_override_code(generate_override, argv, display_path="eval generate")
    else:
        generate_code = generate_traces(
            dataset=dataset,
            output=str(traces_file),
            url=url,
            concurrency=concurrency,
            header=header,
            cookie=cookie,
            session_token=session_token,
            app_name=app_name,
            timeout=timeout,
            console=console,
        )

    if generate_code >= EXIT_CONFIG_ERROR or not traces_file.exists():
        code = (
            worst_exit_code(generate_code, EXIT_INCOMPLETE)
            if generate_code < EXIT_CONFIG_ERROR
            else generate_code
        )
        click.echo(
            f"eval generate produced no traces (exit code {generate_code}); skipping grade.",
            err=True,
        )
        sys.exit(code)

    console.rule("[bold]Step 2/2: eval grade[/bold]")
    grade_override = installed_override("eval.grade")
    if grade_override is not None:
        argv = _grade_override_argv(
            traces_file=traces_file,
            dataset=dataset,
            config_path=config_path,
            output_path=output_path,
            judge_provider=judge_provider,
            judge_model=judge_model,
            judge_timeout=judge_timeout,
        )
        grade_code = _run_override_code(grade_override, argv, display_path="eval grade")
    else:
        grade_code = grade_traces(
            traces_path=str(traces_file),
            dataset=dataset,
            config_path=config_path,
            output_path=output_path,
            judge_provider=judge_provider,
            judge_model=judge_model,
            judge_timeout=judge_timeout,
            console=console,
        )

    code = worst_exit_code(generate_code, grade_code)
    if code:
        click.echo(f"eval run: generate exit {generate_code}, grade exit {grade_code}", err=True)
        sys.exit(code)
