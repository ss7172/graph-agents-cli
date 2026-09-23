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

"""graph-agents-cli playground command: run the application with reload.

D22: the playground exercises the selected application, not a substitute.
``fastapi`` -> ``uvicorn <agent_dir>.fast_api_app:app --reload`` with
``APP_ENV=dev`` so the app serves ``/playground``; ``langgraph-server`` ->
``langgraph dev --no-browser`` (the server *is* the application and mounts
``/chat`` and ``/playground``). ``--graph`` runs ``langgraph dev`` for
LangGraph Studio under either runtime and bypasses the policy adapter.
"""

from __future__ import annotations

import shlex
import threading
import time
import webbrowser
from typing import NamedTuple

import click
from rich.panel import Panel

from graph_agents_cli._output import Console
from graph_agents_cli._project import (
    chdir_project_root,
    read_project_config,
    require_agent_directory,
)
from graph_agents_cli._runner import run

_console = Console()

DEFAULT_PORT = 8000
HOST = "127.0.0.1"
RUNTIME_FASTAPI = "fastapi"
RUNTIME_LANGGRAPH_SERVER = "langgraph-server"
_OPEN_TIMEOUT = 60.0


class PlaygroundPlan(NamedTuple):
    """The command to run, the env it needs, and the page to open."""

    args: list[str]
    env: dict[str, str]
    url: str
    # URL polled before the browser opens; None when the tool opens it itself.
    ready_url: str | None
    label: str


def build_plan(
    *,
    agent_dir: str,
    runtime: str,
    port: int,
    graph: bool,
    open_browser: bool,
) -> PlaygroundPlan:
    """Decide what to run for the manifest ``runtime`` and the flags."""
    base = f"http://{HOST}:{port}"
    if graph:
        args = ["uv", "run", "langgraph", "dev", "--port", str(port)]
        if not open_browser:
            args.append("--no-browser")
        return PlaygroundPlan(
            args=args,
            env={"APP_ENV": "dev"},
            url=f"https://smith.langchain.com/studio/?baseUrl={base}",
            ready_url=None,  # langgraph dev opens Studio itself.
            label="LangGraph Studio (graph debugging, bypasses the auth policy)",
        )
    if runtime == RUNTIME_FASTAPI:
        args = [
            "uv",
            "run",
            "uvicorn",
            f"{agent_dir}.fast_api_app:app",
            "--reload",
            "--host",
            HOST,
            "--port",
            str(port),
        ]
        label = "FastAPI application with reload"
    elif runtime == RUNTIME_LANGGRAPH_SERVER:
        args = ["uv", "run", "langgraph", "dev", "--no-browser", "--port", str(port)]
        label = "LangGraph Server (langgraph dev) with the mounted app"
    else:
        raise click.ClickException(
            f"Unsupported runtime {runtime!r} in graph-agents-cli-manifest.yaml.\n"
            f"  Expected one of: {RUNTIME_FASTAPI}, {RUNTIME_LANGGRAPH_SERVER}"
        )
    return PlaygroundPlan(
        args=args,
        env={"APP_ENV": "dev"},
        url=f"{base}/playground",
        ready_url=f"{base}/health",
        label=label,
    )


def _wait_and_open(url: str, ready_url: str, timeout: float = _OPEN_TIMEOUT) -> bool:
    """Poll ``ready_url`` until it answers, then open ``url`` in the browser."""
    import httpx

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(ready_url, timeout=1.0)
            if resp.status_code < 500:
                webbrowser.open(url)
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    return False


def _open_when_ready(plan: PlaygroundPlan) -> threading.Thread | None:
    if plan.ready_url is None:
        return None
    thread = threading.Thread(
        target=_wait_and_open, args=(plan.url, plan.ready_url), daemon=True, name="open-playground"
    )
    thread.start()
    return thread


@click.command("playground")
@click.option(
    "--port",
    default=DEFAULT_PORT,
    show_default=True,
    type=int,
    help="Port the application listens on.",
)
@click.option(
    "--graph",
    is_flag=True,
    default=False,
    help="Run `langgraph dev` for LangGraph Studio graph debugging (bypasses the auth policy).",
)
@click.option(
    "--no-open",
    "no_open",
    is_flag=True,
    default=False,
    help="Do not open the browser.",
)
def cmd_playground(port: int, graph: bool, no_open: bool) -> None:
    """Start the application locally with reload and the dev chat page.

    \b
    fastapi          uv run uvicorn <agent_dir>.fast_api_app:app --reload (APP_ENV=dev)
    langgraph-server uv run langgraph dev --no-browser
    --graph          uv run langgraph dev (LangGraph Studio) under either runtime

    The chat page at /playground talks to the same /chat endpoint and auth
    policy that `run` and `eval generate` use.
    """
    chdir_project_root()
    cfg = read_project_config()
    require_agent_directory(cfg)
    runtime = getattr(cfg, "runtime", RUNTIME_FASTAPI) or RUNTIME_FASTAPI

    plan = build_plan(
        agent_dir=cfg.agent_directory,
        runtime=runtime,
        port=port,
        graph=graph,
        open_browser=not no_open,
    )
    _print_banner(plan)
    if not no_open:
        _open_when_ready(plan)
    run(plan.args, env=plan.env, print_cmd=False, check_err_msg="Failed to start playground")


def _print_banner(plan: PlaygroundPlan) -> None:
    """Print a styled banner with a clickable URL pointing at the page."""
    cmd_str = shlex.join(plan.args)
    env_str = " ".join(f"{k}={v}" for k, v in plan.env.items())
    body = (
        f"[bold cyan]Starting {plan.label}...[/]\n"
        "\n"
        f"[bold]Running command:[/]       {env_str} {cmd_str}\n"
        f"[bold]Will be available at:[/]  [green underline]{plan.url}[/]"
    )
    _console.print(Panel(body, border_style="cyan"))
