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

"""graph-agents-cli eval generate command: run the agent over a dataset into traces."""

from __future__ import annotations

import contextlib
import inspect
import os
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import click
from rich.markup import escape

from graph_agents_cli._chat_client import redact_credentials
from graph_agents_cli._output import Console
from graph_agents_cli._project import find_project_root
from graph_agents_cli._remote import deprecated_session_token, fold_session_token
from graph_agents_cli.eval import _paths
from graph_agents_cli.eval._client import (
    DEFAULT_TIMEOUT,
    STATUS_OK,
    build_case_headers,
    empty_trace,
    run_case,
)
from graph_agents_cli.eval._common import (
    EXIT_INCOMPLETE,
    EXIT_OK,
    EvalConfigError,
    project_env,
    project_meta,
    utc_now_iso,
    write_json_file,
)
from graph_agents_cli.eval.dataset import Dataset, EvalCase, load_dataset
from graph_agents_cli.run._signals import shielded, terminate_like_interrupt

DEFAULT_CONCURRENCY = 4
API_KEY_ENV = "GRAPH_AGENTS_CLI_API_KEY"
# Decisions on role:<name> gates are sent with this credential when set (an
# approver other than the eval identity); a gate that lists requester is
# decided as the eval identity, which started the run.
APPROVER_KEY_ENV = "GRAPH_AGENTS_CLI_APPROVER_API_KEY"


def _start_local_server(project_root: Path, meta: dict[str, Any]) -> tuple[str, Callable[[], None]]:
    """Start (or reuse) the project's local server; returns ``(base_url, teardown)``.

    Coded against ``run._local_server.ensure_server(project_root, agent_dir, *,
    runtime, ...)`` returning a ``ServerInfo`` with ``base_url`` (or ``port``),
    ``started`` and ``pid``. Only the required parameters the function actually
    declares are filled in (``agent_dir``, ``runtime``, ``language``), so a
    signature change on either side does not break the call.
    """
    from graph_agents_cli.run import _local_server

    ensure_server = _local_server.ensure_server
    params = inspect.signature(ensure_server).parameters
    args: list[Any] = [project_root]
    kwargs: dict[str, Any] = {}
    for name, param in list(params.items())[1:]:
        if param.default is not inspect.Parameter.empty:
            continue
        value: Any
        if name == "agent_dir":
            value = meta.get("agent_directory") or "app"
        elif name == "runtime":
            value = meta.get("runtime") or "fastapi"
        elif name == "language":
            value = "python"
        else:
            continue
        if param.kind is inspect.Parameter.KEYWORD_ONLY:
            kwargs[name] = value
        else:
            args.append(value)
    info = ensure_server(*args, **kwargs)

    base_url = getattr(info, "base_url", None)
    if not base_url:
        port = getattr(info, "port", None)
        if port is None:
            raise EvalConfigError("local server start returned no base_url or port")
        base_url = f"http://127.0.0.1:{port}"
    started = bool(getattr(info, "started", True))
    pid = getattr(info, "pid", None)
    # A long session must not look idle to a concurrent `run` (which would
    # replace the server under it): stamp last_activity at a coarse cadence.
    heartbeat = getattr(_local_server, "start_activity_heartbeat", None)
    stop_heartbeat = heartbeat(project_root) if callable(heartbeat) else (lambda: None)

    def teardown() -> None:
        # A SIGTERM or Ctrl-C now is held until the server is stopped and its
        # record removed, then handled (a stale record would name a dead PID).
        with shielded():
            stop_heartbeat()
            if not started:
                return
            stop = _local_server.stop_server
            if pid is not None and "pid" in inspect.signature(stop).parameters:
                stop(project_root, pid=pid)
            else:
                stop(project_root)

    return str(base_url), teardown


def _dispatch(
    cases: list[EvalCase],
    *,
    base_url: str,
    headers: dict[str, str],
    metadata: dict[str, Any],
    concurrency: int,
    timeout: float,
    console: Console,
    decision_headers: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Run every case in parallel; input order is preserved in the result."""
    traces: list[dict[str, Any] | None] = [None] * len(cases)

    def _one(index: int, case: EvalCase) -> tuple[int, dict[str, Any]]:
        try:
            return index, run_case(
                base_url,
                case,
                headers=headers,
                metadata=metadata,
                timeout=timeout,
                decision_headers=decision_headers,
            )
        except Exception as exc:  # a worker crash still yields a trace
            trace = empty_trace(case.id)
            trace["status"] = "error"
            trace["error"] = redact_credentials(f"worker crashed: {type(exc).__name__}: {exc}")
            return index, trace

    pool = ThreadPoolExecutor(max_workers=max(1, concurrency))
    try:
        futures = [pool.submit(_one, i, case) for i, case in enumerate(cases)]
        for future in as_completed(futures):
            index, trace = future.result()
            traces[index] = trace
            status = trace["status"]
            style = "green" if status == STATUS_OK else "red"
            detail = f"{trace['latency_ms']} ms" if status == STATUS_OK else str(trace.get("error"))
            console.print(f"[generate] {trace['case_id']}: [{style}]{status}[/{style}] ({detail})")
    except BaseException:
        # Ctrl-C / SIGTERM: queued cases never start, and the caller's teardown
        # stops the server now instead of after every in-flight call timed out
        # (those calls then fail fast against the stopped server).
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    return [t if t is not None else empty_trace(c.id) for t, c in zip(traces, cases, strict=True)]


_WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def display_url(url: str) -> str:
    """``url`` as printed and stored, with any ``user:password@`` replaced by ``***@``."""
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    return urlunsplit(parts._replace(netloc="***@" + parts.netloc.rsplit("@", 1)[1]))


def _policy_write_access(project_root: Path) -> list[str]:
    """``"api (METHOD, ...)"`` for each API whose policy allows a write method.

    Read from the project's own ``api-policy.yaml``; the deployed image carries
    the policy it was built with, so this is a hint, not a guarantee. Any
    problem reading it yields no hint (``lint`` reports policy problems).
    """
    from graph_agents_cli._api_policy import POLICY_FILENAME, load_policy_document

    path = project_root / POLICY_FILENAME
    if not path.is_file():
        return []
    try:
        document = load_policy_document(path)
    except Exception:
        return []
    found: list[str] = []
    for name, api in (document.get("apis") or {}).items():
        if not isinstance(api, dict):
            continue
        allowed = _expand([str(m).upper() for m in api.get("allowed_methods") or []])
        operations = api.get("allowed_operations")
        if isinstance(operations, list):
            # An allow-list narrows access to its entries (an entry without
            # `methods` takes the API's allowed_methods).
            methods: set[str] = set()
            for entry in operations:
                pinned = entry.get("methods") if isinstance(entry, dict) else None
                if pinned:
                    methods |= _expand([str(m).upper() for m in pinned]) & allowed
                else:
                    methods |= allowed
            allowed = methods
        writes = [m for m in _WRITE_METHODS if m in allowed]
        if writes:
            found.append(f"{name} ({', '.join(writes)})")
    return found


def _expand(methods: list[str]) -> set[str]:
    return set(_WRITE_METHODS) | set(methods) if "*" in methods else set(methods)


def warn_live_target(
    console: Console, base_url: str, project_root: Path, cases: int | None
) -> None:
    """Say, before any case runs, that `--url` runs the agent's tools for real there."""
    writes = _policy_write_access(project_root)
    what = f"{cases} case(s) will run" if cases else "The cases will run"
    console.print(
        f"[bold yellow]Warning:[/bold yellow] [yellow]{what} against the agent at "
        f"{escape(display_url(base_url))} (--url). Every tool call the agent makes runs for "
        "real in that environment, as the identity these requests authenticate as: tools "
        "that create, change or delete data do so there.[/yellow]"
    )
    if writes:
        console.print(
            "[yellow]This project's api-policy.yaml allows write methods: "
            f"{escape('; '.join(writes))}.[/yellow]"
        )
    console.print(
        "[yellow]Point eval at an environment whose data you can reset, with a dedicated test "
        "identity; never at production data.[/yellow]"
    )


# Headers that carry the eval identity's credential: an approver's decisions go without them.
_CREDENTIAL_HEADERS = frozenset({"authorization", "cookie", "x-api-key"})


def decision_headers_for(headers: dict[str, str], env: dict[str, str]) -> dict[str, str] | None:
    """The headers decisions on ``role:`` gates go with: the approver's credential when set.

    Only the approver's bearer credential identifies the decider: the eval
    identity's own credentials (its bearer, cookies, API key header) are left
    out, so a policy that reads them cannot take the decision as the eval
    identity. A gate that lists ``requester`` is decided as the eval identity.
    """
    key = env.get(APPROVER_KEY_ENV, "")
    if not key:
        return None
    decided = {k: v for k, v in headers.items() if k.lower() not in _CREDENTIAL_HEADERS}
    decided["Authorization"] = f"Bearer {key}"
    return decided


def approving_cases(cases: list[EvalCase]) -> list[str]:
    """Ids of the cases whose approvals instructions approve a gated call."""
    return [c.id for c in cases if any(i["decision"] == "approve" for i in c.approvals)]


def _print_incomplete_summary(traces: list[dict[str, Any]], *, remote: bool = False) -> None:
    bad = [t for t in traces if t["status"] != STATUS_OK]
    click.echo("", err=True)
    click.echo(
        f"Generate summary: {len(traces) - len(bad)}/{len(traces)} cases produced a response, "
        f"{len(bad)} did not.",
        err=True,
    )
    for trace in bad:
        click.echo(f"  - {trace['case_id']}: {trace['status']}: {trace.get('error')}", err=True)
    if any("HTTP 401 " in str(t.get("error") or "") for t in bad):
        # The same policy-aware hint `run` prints (which credential, and how to send it).
        from graph_agents_cli.run.cmd_run import _auth_hint, _project_auth_policy

        hint = _auth_hint(_project_auth_policy(remote=remote), remote=remote).strip()
        click.echo(f"  {hint}", err=True)


def generate_traces(
    *,
    dataset: str | None = None,
    output: str | None = None,
    url: str | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    header: tuple[str, ...] = (),
    cookie: tuple[str, ...] = (),
    session_token: str | None = None,
    app_name: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    console: Console | None = None,
) -> int:
    """Run the dataset and write the trace file; returns 0 (complete) or 2 (incomplete)."""
    console = console or Console()
    project_root = find_project_root()
    if project_root is None:
        raise EvalConfigError(
            "not inside a graph-agents-cli project (no graph-agents-cli-manifest.yaml found)"
        )
    files = _paths.resolve_input_datasets(project_root, dataset)
    if not files:
        raise EvalConfigError(
            f"no dataset found: pass --dataset PATH or add {_paths.DEFAULT_INPUT_DATASET}"
        )
    ds: Dataset = load_dataset(files)
    output_path = _paths.resolve_output_path(
        project_root,
        output,
        default_dir=_paths.default_traces_dir(project_root),
        prefix=_paths.TRACES_FILE_PREFIX,
    )
    meta = project_meta(project_root)
    app_name = app_name or meta["agent_directory"]

    env: dict[str, str] = dict(os.environ)
    if not url and not env.get(API_KEY_ENV):
        # The local server enforces the shared-bearer policy with the project's API_KEY.
        local_key = project_env(project_root).get("API_KEY")
        if local_key:
            env[API_KEY_ENV] = local_key
    headers = build_case_headers(header, cookie, session_token, env=env)
    decision_headers = decision_headers_for(headers, env)

    console.print(
        f"Running [cyan]{len(ds.cases)}[/cyan] case(s) from "
        f"[cyan]{', '.join(str(f.relative_to(project_root)) if f.is_relative_to(project_root) else str(f) for f in files)}[/cyan]"
    )
    metadata = {"dataset_hash": ds.hash, "app_name": app_name}
    # With a local server, SIGTERM/SIGHUP unwind like Ctrl-C: the server this run
    # started is stopped (and its pid file removed) even when a CI timeout kills it.
    with terminate_like_interrupt() if not url else contextlib.nullcontext():
        teardown: Callable[[], None] = lambda: None  # noqa: E731
        if url:
            base_url = url.rstrip("/")
            console.print(f"Target: [cyan]{escape(display_url(base_url))}[/cyan]")
            warn_live_target(console, base_url, project_root, len(ds.cases))
            approving = approving_cases(ds.cases)
            if approving:
                console.print(
                    f"[yellow]{len(approving)} case(s) approve gated calls "
                    f"({escape(', '.join(approving[:5]))}{', ...' if len(approving) > 5 else ''})"
                    ": those calls are sent there as approved.[/yellow]"
                )
        else:
            console.print("Starting the local server...")
            base_url, teardown = _start_local_server(project_root, meta)
            console.print(f"Local server at [cyan]{base_url}[/cyan]")

        try:
            traces = _dispatch(
                ds.cases,
                base_url=base_url,
                headers=headers,
                metadata=metadata,
                concurrency=concurrency,
                timeout=timeout,
                console=console,
                decision_headers=decision_headers,
            )
        finally:
            teardown()

    # The model is known only for the project's own local server, which runs the
    # project's settings. The agent at a --url target does not report its model,
    # and the local settings need not be what runs there: its traces name none.
    model = None if url else meta["model"]
    for trace, case in zip(traces, ds.cases, strict=True):
        trace["agent_version"] = meta["agent_version"]
        trace["model"] = model
        trace["case"] = case.raw

    doc = {
        "dataset_hash": ds.hash,
        "dataset_paths": [
            str(f.relative_to(project_root)) if f.is_relative_to(project_root) else str(f)
            for f in files
        ],
        "generated_at": utc_now_iso(),
        "agent_version": meta["agent_version"],
        "model": model,
        # The project's MODEL_PROVIDER: the agent's only for the local server.
        # `eval grade` warns when that agent ran on the fake model, and for a
        # --url target when these settings name it.
        "model_provider": meta["model_provider"],
        "target": "url" if url else "local",
        "base_url": display_url(base_url),
        "app_name": app_name,
        "traces": traces,
    }
    write_json_file(output_path, doc)
    console.print(f"Traces saved to [green]{output_path}[/green]")

    if any(t["status"] != STATUS_OK for t in traces):
        _print_incomplete_summary(traces, remote=bool(url))
        return EXIT_INCOMPLETE
    return EXIT_OK


@click.command("generate")
@click.option(
    "--dataset",
    default=None,
    help=(
        "Dataset file, or a directory of *.json datasets. Defaults to "
        f"{_paths.DEFAULT_INPUT_DATASET}, else every *.json in {_paths.DATASETS_DIR}/."
    ),
)
@click.option(
    "--output",
    "-o",
    default=None,
    help=(
        "Traces file, or a directory to write traces_<ts>.json into. Defaults to "
        f"{_paths.ARTIFACTS_DIR}/{_paths.TRACES_SUBDIR}/traces_<ts>.json."
    ),
)
@click.option(
    "--url",
    default=None,
    help=(
        "Base URL of a running agent (its POST /chat). Its tools run for real in that "
        "environment, write tools included. When omitted the project's local server is "
        "started for the run and stopped afterwards."
    ),
)
@click.option(
    "--concurrency",
    type=click.IntRange(min=1),
    default=DEFAULT_CONCURRENCY,
    show_default=True,
    help="Cases run in parallel, each on its own thread.",
)
@click.option(
    "--header",
    "-H",
    "header",
    multiple=True,
    help=(
        "Extra HTTP header 'Key: Value' (repeatable). For a bearer credential use "
        "GRAPH_AGENTS_CLI_API_KEY instead (argv is visible to other local users); an "
        "Authorization header overrides it."
    ),
)
@click.option(
    "--cookie",
    multiple=True,
    help="Cookie 'name=value' (repeatable), for a custom auth policy that reads cookies.",
)
@click.option(
    "--session-token",
    default=None,
    hidden=True,
    callback=deprecated_session_token,
    help="Deprecated alias of --header 'X-Session-Token: ...'.",
)
@click.option(
    "--app-name",
    default=None,
    help="Agent name recorded in the traces and sent as request metadata. Defaults to agent_directory.",
)
@click.option(
    "--timeout",
    type=click.FloatRange(min=1),
    default=DEFAULT_TIMEOUT,
    show_default=True,
    help="Seconds allowed per chat call.",
)
def cmd_generate(
    *,
    dataset: str | None,
    output: str | None,
    url: str | None,
    concurrency: int,
    header: tuple[str, ...],
    cookie: tuple[str, ...],
    session_token: str | None,
    app_name: str | None,
    timeout: float,
) -> None:
    """Run the agent over the eval dataset and write traces.

    Each case's user messages are sent to POST /chat (SSE) and the events are
    folded into one trace per case: response, tool calls, usage, latency,
    thread and run ids, and a status of ok, error or missing. The trace file
    records the dataset hash so `eval grade` can account for every planned case.

    Without --url the local server is started through `run`'s server manager
    and stopped after the run. Credentials follow the project's auth policy,
    locally and with --url. Put a bearer credential in GRAPH_AGENTS_CLI_API_KEY
    (sent as 'Authorization: Bearer <value>'): unlike --header, it stays out
    of the process list and your shell history.

    \b
      shared-bearer     GRAPH_AGENTS_CLI_API_KEY=<API_KEY> (locally: the API_KEY in .env)
      jwt               GRAPH_AGENTS_CLI_API_KEY=<token> (locally: auth dev-token)
      custom            --header 'Name: value' or --cookie name=value

    With --url every tool the agent calls runs for real in that environment
    (a warning names the target first): cases that create, change or delete
    data do so there. Use a dedicated test identity and data you can reset.

    A call the API policy gates (an approval block) pauses the run; it is
    decided as the case's "approvals" instructions say ({"decision":
    "approve"|"reject", "match": {...}}) and the run continues. A gate no
    instruction matches makes the case an error: generate never approves on
    its own, and rejects such a gate (as it does one whose decision was
    refused) so that no approval is left pending; the trace records it
    (approvals[].cleanup), and a gate it may not reject either is named in the
    case error. A gate that lists requester is decided as the eval identity (it
    started the run); any other with GRAPH_AGENTS_CLI_APPROVER_API_KEY as a
    bearer credential when it is set (a principal holding the gate's role).

    \b
    Exit codes:
      0  every case produced a trace with a response
      2  at least one case is error or missing (summary on stderr)
      3  configuration error (no dataset, malformed case, no project)
    """
    code = generate_traces(
        dataset=dataset,
        output=output,
        url=url,
        concurrency=concurrency,
        header=fold_session_token(header, session_token),
        cookie=cookie,
        session_token=None,
        app_name=app_name,
        timeout=timeout,
    )
    if code:
        sys.exit(code)
