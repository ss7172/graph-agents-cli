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

"""graph-agents-cli run command: run the agent with a single prompt.

Client side of the chat API, the auth policies and the local server. ``chat`` mode streams the SSE
events of ``POST /chat``; ``a2a`` mode talks JSON-RPC through ``a2a-sdk``
(optional extra). Locally the server is started per the manifest runtime and
tracked in ``.graph-agents-cli/run_server.json``.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any, NamedTuple

import click
import httpx

from graph_agents_cli import _chat_client
from graph_agents_cli._chat_client import (
    EVENT_ERROR,
    EVENT_MESSAGE_DELTA,
    EVENT_MESSAGE_END,
    EVENT_MESSAGE_START,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    ChatHTTPError,
    SseEvent,
    post_chat,
)
from graph_agents_cli._project import (
    chdir_project_root,
    find_project_root,
    read_project_config,
    require_agent_directory,
)
from graph_agents_cli._remote import (
    API_KEY_ENV,
    DEFAULT_RUN_MODE,
    RUN_MODES,
    build_headers,
    classify_url,
    deprecated_session_token,
    fold_session_token,
)
from graph_agents_cli.run._local_server import (
    ensure_server,
    stop_server,
)

_RESULT_PREVIEW_CHARS = 400
# Credential-bearing flags are never echoed verbatim in the resume line: the
# footer lands in CI logs and pasted transcripts.
REDACTED = "<redacted>"
_SENSITIVE_HEADER_NAMES = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "x-session-token",
        "x-api-key",
        "x-auth-token",
    }
)
_SENSITIVE_HEADER_HINTS = ("auth", "token", "secret", "key", "cookie")


def _a2a_install_hint() -> str:
    from graph_agents_cli.scaffold.utils.version import get_current_version, install_command

    return (
        "A2A mode needs the optional 'a2a' extra: install it with "
        f"`{install_command('a2a', version=get_current_version())}`, or use --mode chat."
    )


def _require_a2a_sdk() -> None:
    """Fail with a one-line hint when the optional a2a-sdk is missing.

    Checked before any server is started so a missing extra costs nothing.
    """
    import importlib.util

    try:
        found = importlib.util.find_spec("a2a") is not None
    except (ImportError, ValueError):  # a blocked/None entry in sys.modules
        found = False
    if not found:
        raise click.ClickException(_a2a_install_hint())


class RunTarget(NamedTuple):
    """Where this invocation sends the prompt."""

    mode: str
    # Chat base URL (``<base>/chat``) or the resolved A2A base.
    base_url: str
    headers: dict[str, str]
    remote: bool
    # True only when this invocation started the local server; False when a
    # running server was reused or for remote (--url) runs.
    started_server: bool = False
    server_pid: int | None = None
    checkpointer: str = "memory"


class RunOutcome(NamedTuple):
    """What a query produced, for the footer and for tests."""

    thread_id: str | None
    run_id: str | None
    rendered: bool
    usage: dict[str, Any] | None = None
    latency_ms: int | None = None
    status: str | None = None


class AgentError(Exception):
    """The server sent an ``error`` event."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


# ---------------------------------------------------------------------------
# Message composition
# ---------------------------------------------------------------------------


def compose_message(message: str, files: Iterable[str] = ()) -> str:
    """Append each ``--file`` as extra context lines after the prompt.

    Attachments are text only (no multimodal input in this CLI); a file
    that does not decode as UTF-8 is refused with a clear message.
    """
    parts = [message]
    for raw in files:
        path = Path(raw)
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise click.ClickException(
                f"Cannot attach {path}: not a UTF-8 text file. Only text attachments are supported."
            ) from exc
        except OSError as exc:
            raise click.ClickException(f"Cannot read {path}: {exc}") from exc
        parts.append(
            f"\n\n--- Attached file: {path.name} ---\n{content.rstrip()}\n--- End of {path.name} ---"
        )
    return "".join(parts)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _json_preview(value: Any, limit: int | None) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if limit is not None and len(text) > limit:
        return text[:limit] + "..."
    return text


class _ChatRenderer:
    """Prints the chat API's events to the terminal as they stream."""

    def __init__(self, *, verbose: bool = False) -> None:
        self.verbose = verbose
        self.at_line_start = True
        self.rendered = False
        self.tagged = False
        self.thread_id: str | None = None
        self.run_id: str | None = None
        self.usage: dict[str, Any] | None = None
        self.latency_ms: int | None = None
        self.status: str | None = None

    def _newline_if_needed(self) -> None:
        if not self.at_line_start:
            click.echo()
            self.at_line_start = True

    def _tag(self) -> None:
        if not self.tagged:
            click.echo("[agent]: ", nl=False)
            self.tagged = True
            self.at_line_start = False

    def handle(self, ev: SseEvent) -> None:
        data = ev.data if isinstance(ev.data, dict) else {}
        if ev.event == EVENT_MESSAGE_START:
            self.thread_id = data.get("thread_id") or self.thread_id
            self.run_id = data.get("run_id") or self.run_id
        elif ev.event == EVENT_MESSAGE_DELTA:
            text = data.get("text") if data else (ev.data if isinstance(ev.data, str) else "")
            if text:
                self._tag()
                click.echo(text, nl=False)
                self.at_line_start = text.endswith("\n")
                self.rendered = True
        elif ev.event == EVENT_TOOL_CALL:
            self._newline_if_needed()
            name = data.get("name", "")
            args = data.get("args")
            rendered_args = "" if args is None else _json_preview(args, None)
            click.secho(f"[tool_call: {name}({rendered_args})]", dim=True)
            self.rendered = True
        elif ev.event == EVENT_TOOL_RESULT:
            self._newline_if_needed()
            name = data.get("name", "")
            limit = None if self.verbose else _RESULT_PREVIEW_CHARS
            result = _json_preview(data.get("result", ""), limit)
            marker = " (error)" if data.get("is_error") else ""
            click.secho(f"[tool_result{marker}: {name} -> {result}]", dim=True)
            self.rendered = True
        elif ev.event == EVENT_MESSAGE_END:
            self.thread_id = data.get("thread_id") or self.thread_id
            self.run_id = data.get("run_id") or self.run_id
            self.usage = data.get("usage") if isinstance(data.get("usage"), dict) else self.usage
            self.latency_ms = data.get("latency_ms", self.latency_ms)
            self.status = data.get("status", self.status)
        elif ev.event == EVENT_ERROR:
            self._newline_if_needed()
            code = str(data.get("code", "error")) if data else "error"
            message = str(data.get("message", ev.raw)) if data else str(ev.raw)
            raise AgentError(code, message)

        if self.verbose:
            self._newline_if_needed()
            payload = {"event": ev.event, "data": ev.data}
            click.secho(json.dumps(payload, indent=2, ensure_ascii=False, default=str), dim=True)

    def outcome(self) -> RunOutcome:
        return RunOutcome(
            thread_id=self.thread_id,
            run_id=self.run_id,
            rendered=self.rendered,
            usage=self.usage,
            latency_ms=self.latency_ms,
            status=self.status,
        )


def render_chat_events(events: Iterable[SseEvent], *, verbose: bool = False) -> RunOutcome:
    """Render a stream of chat events; raises :class:`AgentError` on ``error``."""
    renderer = _ChatRenderer(verbose=verbose)
    try:
        for ev in events:
            renderer.handle(ev)
    finally:
        renderer._newline_if_needed()
    return renderer.outcome()


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------


def _local_project_agent_directory() -> str | None:
    """The agent directory of the enclosing project, if any (no chdir)."""
    root = find_project_root(Path.cwd())
    if root is None:
        return None
    try:
        return read_project_config(str(root)).agent_directory
    except click.ClickException:
        return None


def _local_api_key(project_root: Path) -> str | None:
    """``API_KEY`` from the project's ``.env`` so local shared-bearer runs just work."""
    env_path = project_root / ".env"
    if not env_path.is_file():
        return None
    from dotenv import dotenv_values

    try:
        return dotenv_values(env_path).get("API_KEY") or None
    except Exception:
        return None


def _resolve_target(
    *,
    url: str | None,
    mode: str,
    header: tuple[str, ...],
    cookie: tuple[str, ...],
    session_token: str | None,
    start_server: bool,
) -> RunTarget:
    headers = build_headers(header, cookie, session_token)

    if url:
        agent_dir = _local_project_agent_directory() if mode == "a2a" else None
        remote = classify_url(url, mode=mode, agent_directory=agent_dir, headers=headers)
        base = remote.a2a_base if remote.mode == "a2a" else remote.base_url
        return RunTarget(
            mode=remote.mode, base_url=base or remote.base_url, headers=headers, remote=True
        )

    chdir_project_root()
    cfg = read_project_config()
    require_agent_directory(cfg)
    runtime = getattr(cfg, "runtime", "fastapi") or "fastapi"
    checkpointer = getattr(cfg, "checkpointer", "memory") or "memory"
    project_root = Path.cwd()
    server = ensure_server(
        project_root,
        cfg.agent_directory,
        runtime=runtime,
        checkpointer=checkpointer,
        keep_running=start_server,
    )
    if not any(k.lower() == "authorization" for k in headers):
        api_key = _local_api_key(project_root)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
    base = server.base_url
    if mode == "a2a":
        base = f"{base}/a2a/{cfg.agent_directory}"
    return RunTarget(
        mode=mode,
        base_url=base,
        headers=headers,
        remote=False,
        started_server=server.started,
        server_pid=server.pid,
        checkpointer=server.checkpointer,
    )


def _redact_header(raw: str) -> str:
    """``Name: value`` with the value replaced when the header carries a credential."""
    name, sep, _value = raw.partition(":")
    if not sep:
        return raw
    lowered = name.strip().lower()
    if lowered in _SENSITIVE_HEADER_NAMES or any(h in lowered for h in _SENSITIVE_HEADER_HINTS):
        return f"{name.strip()}: {REDACTED}"
    return raw


def _redact_cookie(raw: str) -> str:
    name, sep, _value = raw.partition("=")
    return f"{name}={REDACTED}" if sep else REDACTED


def _build_resume_flags(
    url: str | None,
    mode: str,
    header: tuple[str, ...],
    cookie: tuple[str, ...],
    session_token: str | None,
) -> str:
    """Flags a resumed run must repeat, with a leading space (empty for local chat).

    Credential values (``Authorization``/``X-Api-Key``-style headers, every
    cookie, the session token) are printed as ``<redacted>``; the reader
    re-supplies them. Routing headers such as ``X-Tenant`` stay readable.
    """
    flags: list[str] = []
    if url:
        flags.append(f"--url {shlex.quote(url)}")
    if mode != DEFAULT_RUN_MODE:
        flags.append(f"--mode {mode}")
    flags.extend(f"--header {shlex.quote(_redact_header(h))}" for h in header)
    flags.extend(f"--cookie {shlex.quote(_redact_cookie(c))}" for c in cookie)
    if session_token:
        flags.append(f"--session-token {REDACTED}")
    return (" " + " ".join(flags)) if flags else ""


def _print_footer(
    outcome: RunOutcome,
    *,
    resume_flags: str,
    keep_server: bool,
    checkpointer: str,
) -> None:
    if not outcome.rendered:
        click.secho("(no response content)", fg="yellow")
    if outcome.usage or outcome.latency_ms is not None:
        bits = []
        if outcome.usage:
            bits.append(
                f"tokens in/out {outcome.usage.get('input_tokens', '?')}/"
                f"{outcome.usage.get('output_tokens', '?')}"
            )
        if outcome.latency_ms is not None:
            bits.append(f"{outcome.latency_ms} ms")
        click.secho("  ".join(bits), dim=True)
    if not outcome.thread_id:
        return
    click.echo()
    click.secho(f"Thread: {outcome.thread_id}", dim=True)
    if keep_server or checkpointer == "postgres":
        hint = "  (re-supply the redacted credential values)" if REDACTED in resume_flags else ""
        click.secho(
            f'  Resume with: graph-agents-cli run "<message>"{resume_flags}'
            f" --thread-id {outcome.thread_id}{hint}",
            dim=True,
        )
    else:
        click.secho(
            "  One-off server with an in-memory checkpointer: add --start-server to "
            "keep the server (and its threads) alive so you can resume with --thread-id.",
            dim=True,
        )


# ---------------------------------------------------------------------------
# Protocol handlers
# ---------------------------------------------------------------------------


def _query_chat(
    target: RunTarget,
    prompt: str,
    *,
    thread_id: str | None,
    verbose: bool,
    display_message: str,
) -> RunOutcome:
    click.echo(f"[user]: {display_message}")
    events = post_chat(target.base_url, prompt, thread_id=thread_id, headers=target.headers)
    return render_chat_events(events, verbose=verbose)


def _query_a2a(
    target: RunTarget,
    prompt: str,
    *,
    thread_id: str | None,
    verbose: bool,
    display_message: str,
) -> RunOutcome:
    try:
        from a2a.client import ClientConfig, create_client
        from a2a.types import Message, Part, Role, SendMessageRequest
        from a2a.utils.constants import (
            PROTOCOL_VERSION_1_0,
            VERSION_HEADER,
            TransportProtocol,
        )
    except ImportError as exc:
        raise click.ClickException(_a2a_install_hint()) from exc

    click.echo(f"[user]: {display_message}")

    async def _go() -> RunOutcome:
        req_headers = dict(target.headers)
        req_headers.setdefault(VERSION_HEADER, PROTOCOL_VERSION_1_0)
        renderer = _ChatRenderer(verbose=verbose)
        context_id: str | None = None

        def _render_parts(parts: Iterable[Any]) -> None:
            for part in parts:
                text = getattr(part, "text", "")
                if text:
                    renderer.handle(SseEvent(EVENT_MESSAGE_DELTA, {"text": text}, text))
                elif getattr(part, "url", ""):
                    renderer.handle(
                        SseEvent(EVENT_MESSAGE_DELTA, {"text": f"\n[file: {part.url}]"}, "")
                    )

        async with httpx.AsyncClient(
            headers=req_headers, timeout=_chat_client.STREAM_TIMEOUT
        ) as http_client:
            config = ClientConfig(
                httpx_client=http_client,
                # Keep JSONRPC first: the scaffolded A2A endpoint is JSON-RPC.
                supported_protocol_bindings=[
                    TransportProtocol.JSONRPC,
                    TransportProtocol.HTTP_JSON,
                ],
            )
            try:
                client = await create_client(target.base_url, config)
            except Exception as exc:
                raise click.ClickException(
                    f"Could not resolve an A2A agent at {target.base_url}: {exc}\n"
                    "  If this agent only exposes the chat API, try --mode chat."
                ) from exc

            msg = Message(
                message_id=str(uuid.uuid4()),
                role=Role.ROLE_USER,
                parts=[Part(text=prompt)],
                context_id=thread_id or "",
            )
            try:
                async for chunk in client.send_message(SendMessageRequest(message=msg)):
                    if chunk.HasField("artifact_update"):
                        context_id = context_id or chunk.artifact_update.context_id or None
                        _render_parts(chunk.artifact_update.artifact.parts)
                    elif chunk.HasField("task"):
                        context_id = context_id or chunk.task.context_id or None
                        for artifact in chunk.task.artifacts:
                            _render_parts(artifact.parts)
                    elif chunk.HasField("message"):
                        context_id = context_id or chunk.message.context_id or None
                        _render_parts(chunk.message.parts)
                    if verbose:
                        renderer._newline_if_needed()
                        click.secho(str(chunk), dim=True)
            finally:
                renderer._newline_if_needed()
        outcome = renderer.outcome()
        return outcome._replace(thread_id=context_id or thread_id)

    return asyncio.run(_go())


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def _handle_stop_server(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    """Eager callback for ``--stop-server``: stop and exit before argument parsing."""
    if not value:
        return
    chdir_project_root()
    if stop_server(Path.cwd()):
        ctx.exit(0)
    raise click.ClickException("No local server is running.")


def _read_timeout_message(thread_id: str | None, resume_flags: str) -> str:
    seconds = _chat_client.STREAM_TIMEOUT.read
    text = (
        f"No event from the agent for {seconds:.0f} s; the run may still be in progress "
        "on the server (a long tool call or a non-streaming model phase).\n"
        "  The server was left running."
    )
    if thread_id:
        text += f'\n  Check the thread later with: graph-agents-cli run "<message>"{resume_flags} --thread-id {thread_id}'
    return text


def _http_error_hint(exc: ChatHTTPError, *, remote: bool, thread_id: str | None) -> str:
    if exc.status_code in (401, 403):
        return (
            "\n  Authentication failed. For shared-bearer set "
            f"{API_KEY_ENV} or pass --header 'Authorization: Bearer <API_KEY>';"
            "\n  for jwt pass --header 'Authorization: Bearer <token>'; for a custom policy"
            "\n  pass whatever it reads with --header 'Name: value' or --cookie name=value."
        )
    if exc.status_code == 404 and thread_id:
        return f"\n  Thread {thread_id} was not found." + (
            "\n  In-memory threads do not survive a one-off local run; use "
            "--start-server or a postgres checkpointer."
            if not remote
            else ""
        )
    if exc.status_code in (404, 405):
        return "\n  Check that --url points at the app base URL (the chat API is at <url>/chat)."
    if exc.status_code == 503:
        return "\n  The server refused the request (is the auth policy implemented?)."
    return ""


@click.command("run")
@click.argument("message")
@click.option(
    "--mode",
    type=click.Choice(RUN_MODES, case_sensitive=False),
    default=DEFAULT_RUN_MODE,
    show_default=True,
    help="Protocol: 'chat' (POST /chat, SSE) or 'a2a' (JSON-RPC via a2a-sdk).",
)
@click.option(
    "--url",
    default=None,
    help="Base URL of a deployed agent. If given, no local server is started.",
)
@click.option(
    "--thread-id",
    default=None,
    help="Continue an existing thread (printed in the footer of a previous run).",
)
@click.option(
    "--header",
    "-H",
    "header",
    multiple=True,
    help="Custom HTTP header ('Key: Value'). Repeatable. Overrides the API-key default.",
)
@click.option(
    "--cookie",
    "cookie",
    multiple=True,
    help="Cookie ('name=value'), for a custom auth policy that reads cookies. Repeatable.",
)
@click.option(
    "--session-token",
    default=None,
    hidden=True,
    callback=deprecated_session_token,
    help="Deprecated alias of --header 'X-Session-Token: ...'.",
)
@click.option(
    "--file",
    "-f",
    "files",
    multiple=True,
    type=click.Path(exists=True, readable=True, dir_okay=False),
    help="Attach a UTF-8 text file as extra context. Repeatable.",
)
@click.option(
    "--start-server",
    "start_server",
    is_flag=True,
    default=False,
    help=(
        "Keep the local server running after execution. It persists until stopped with "
        "--stop-server, so later runs are faster and in-memory threads survive."
    ),
)
@click.option(
    "--stop-server",
    "stop_server_flag",
    is_flag=True,
    default=False,
    is_eager=True,
    expose_value=False,
    callback=_handle_stop_server,
    help="Stop the local background server and exit.",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    default=False,
    help="Print every event payload as JSON.",
)
def cmd_run(
    message: str,
    *,
    mode: str,
    url: str | None,
    thread_id: str | None,
    header: tuple[str, ...],
    cookie: tuple[str, ...],
    session_token: str | None,
    files: tuple[str, ...],
    start_server: bool,
    verbose: bool,
) -> None:
    """Run the agent with a single prompt (non-interactive).

    MESSAGE is the prompt to send to the agent.

    \b
    Run from your project directory to query the agent locally. A plain run
    starts a one-off server (uvicorn under the fastapi runtime, langgraph dev
    under langgraph-server) and shuts it down when it finishes; pass
    --start-server to keep it running. Later plain runs reuse a running
    server. Stop it with --stop-server. After 30 minutes idle, the next
    request restarts it.

    \b
    Use --url to query a deployed agent instead. --mode selects the protocol
    (default chat). Credentials follow the project's auth policy:
      shared-bearer     --header 'Authorization: Bearer ...' or GRAPH_AGENTS_CLI_API_KEY
      jwt               --header 'Authorization: Bearer <token>'
      custom            --header 'Name: value' or --cookie name=value

    \b
    --thread-id continues a conversation; the footer of every run prints the
    thread id and the command to resume it. --file attaches UTF-8 text files
    as extra context.
    """
    mode = mode.lower()
    # The deprecated --session-token is an X-Session-Token header from here on.
    header = fold_session_token(header, session_token)
    session_token = None
    if url and start_server:
        click.secho(
            "Warning: --start-server has no effect when using --url.", fg="yellow", err=True
        )

    prompt = compose_message(message, files)
    if mode == "a2a":
        _require_a2a_sdk()
    target = _resolve_target(
        url=url,
        mode=mode,
        header=header,
        cookie=cookie,
        session_token=session_token,
        start_server=start_server,
    )
    if url:
        click.echo(f"Querying remote agent: {url} (mode: {target.mode})")

    resume_flags = _build_resume_flags(url, target.mode, header, cookie, session_token)
    # Only tear down a server this invocation started; a reused persistent
    # server (e.g. from --start-server) is left running.
    should_stop_server = not url and not start_server and target.started_server
    keep_server = bool(url) or not should_stop_server

    handler = _query_a2a if target.mode == "a2a" else _query_chat
    try:
        try:
            outcome = handler(
                target,
                prompt,
                thread_id=thread_id,
                verbose=verbose,
                display_message=message,
            )
        except AgentError as exc:
            click.secho(f"[error: {exc.code}]: {exc.message}", fg="red")
            raise click.exceptions.Exit(1) from exc
        except ChatHTTPError as exc:
            hint = _http_error_hint(exc, remote=bool(url), thread_id=thread_id)
            raise click.ClickException(
                f"Agent request failed (HTTP {exc.status_code}):\n  {exc.body}{hint}"
            ) from exc
        except httpx.ReadTimeout as exc:
            # The server answered and then went quiet (a long tool call or a
            # non-streaming model phase): it is neither unreachable nor wedged,
            # so a reused/persistent server is left alone and the message
            # says what happened. ReadTimeout is a TransportError: keep this first.
            raise click.ClickException(_read_timeout_message(thread_id, resume_flags)) from exc
        except httpx.TransportError as exc:
            if not url:
                # The local server is unreachable or wedged: stop it (even one we
                # reused) so a later retry starts a fresh one.
                should_stop_server = True
                raise click.ClickException(
                    f"Could not reach the local server: {exc}\n"
                    "  It has been stopped; retry to start a fresh one."
                ) from exc
            raise click.ClickException(
                f"Could not reach remote agent at: {url}\n"
                f"  {exc}\n"
                "  Check that the URL is correct and the service is running."
            ) from exc
    finally:
        if should_stop_server:
            # cwd is the project root here (set by _resolve_target).
            stop_server(Path.cwd(), pid=target.server_pid)

    _print_footer(
        outcome,
        resume_flags=resume_flags,
        keep_server=keep_server,
        checkpointer=target.checkpointer,
    )
