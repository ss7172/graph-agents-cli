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

A run that reaches a call its API's policy gates (``approval`` in
``api-policy.yaml``) pauses before sending it. ``run`` prints the call in full;
on a terminal, when the requester is one of the approvers, it asks
"Approve? [y/N]" and streams the continuation; otherwise it prints the
``graph-agents-cli approvals approve|reject`` commands and exits 0 with an
"Awaiting approval" line.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
import uuid
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any, NamedTuple

import click
import httpx

from graph_agents_cli import _chat_client
from graph_agents_cli._approvals import (
    DECISION_REFUSALS,
    Approval,
    awaiting_lines,
    safe_text,
    terminal_text,
)
from graph_agents_cli._chat_client import (
    APPROVAL_PENDING,
    DECISION_APPROVE,
    DECISION_REJECT,
    EVENT_ERROR,
    EVENT_MESSAGE_DELTA,
    EVENT_MESSAGE_END,
    EVENT_MESSAGE_START,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    STATUS_AWAITING_APPROVAL,
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
from graph_agents_cli.run._signals import terminate_like_interrupt

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
    # The paused call (``message.end`` ``approval``) when status is awaiting_approval.
    approval: dict[str, Any] | None = None


class _TransportFailure(click.ClickException):
    """The agent could not be reached or went silent: a tool failure (exit 2).

    An answer the agent gave (an HTTP error, an ``error`` event) stays exit 1.
    """

    exit_code = 2


class AgentError(Exception):
    """The server sent an ``error`` event."""

    def __init__(self, code: str, message: str, *, run_id: str | None = None) -> None:
        self.code = code
        self.message = message
        self.run_id = run_id
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


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(", ", ": "))


class _ChatRenderer:
    """Prints the chat API's events to the terminal as they stream.

    With ``verbose`` every event also gets one compact line (``event: <name>
    <json>``); a run of text deltas, whose text is already on screen, is
    summarised as one ``message.delta`` line with its count.
    """

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
        self.approval: dict[str, Any] | None = None
        # Events received so far: tells a connection that failed before the run
        # started from one that dropped while it streamed.
        self.events = 0
        self._pending_deltas = 0
        self._pending_chars = 0

    def _newline_if_needed(self) -> None:
        if not self.at_line_start:
            click.echo()
            self.at_line_start = True

    def _tag(self) -> None:
        if not self.tagged:
            click.echo("[agent]: ", nl=False)
            self.tagged = True
            self.at_line_start = False

    def _flush_deltas(self) -> None:
        if not self._pending_deltas:
            return
        self._newline_if_needed()
        count, chars = self._pending_deltas, self._pending_chars
        self._pending_deltas = self._pending_chars = 0
        plural = "s" if chars != 1 else ""
        click.secho(f"event: {EVENT_MESSAGE_DELTA} x{count} ({chars} character{plural})", dim=True)

    def _trace(self, ev: SseEvent) -> None:
        """The ``-v`` line of one event."""
        if ev.event == EVENT_MESSAGE_DELTA:
            text = ev.data.get("text") if isinstance(ev.data, dict) else ev.data
            self._pending_deltas += 1
            self._pending_chars += len(text) if isinstance(text, str) else 0
            return
        self._flush_deltas()
        self._newline_if_needed()
        click.secho(terminal_text(f"event: {ev.event} {_compact_json(ev.data)}"), dim=True)

    def write_text(self, text: Any) -> None:
        """Print answer text inline (after the ``[agent]:`` tag)."""
        if text:
            text = terminal_text(str(text))
            self._tag()
            click.echo(text, nl=False)
            self.at_line_start = text.endswith("\n")
            self.rendered = True

    def finish(self) -> None:
        """End the output: pending ``-v`` summary, then a final newline."""
        self._flush_deltas()
        self._newline_if_needed()

    def resume(self) -> None:
        """Ready for the continuation of a paused run: a new ``[agent]:`` tag, no pause."""
        self.tagged = False
        self.at_line_start = True
        self.status = None
        self.approval = None

    def handle(self, ev: SseEvent) -> None:
        self.events += 1
        data = ev.data if isinstance(ev.data, dict) else {}
        if self.verbose and ev.event != EVENT_MESSAGE_DELTA:
            self._trace(ev)
        if ev.event == EVENT_MESSAGE_START:
            self.thread_id = data.get("thread_id") or self.thread_id
            self.run_id = data.get("run_id") or self.run_id
        elif ev.event == EVENT_MESSAGE_DELTA:
            text = data.get("text") if data else (ev.data if isinstance(ev.data, str) else "")
            self.write_text(text)
        elif ev.event == EVENT_TOOL_CALL:
            self._newline_if_needed()
            name = terminal_text(str(data.get("name", "")))
            args = data.get("args")
            rendered_args = "" if args is None else terminal_text(_json_preview(args, None))
            click.secho(f"[tool_call: {name}({rendered_args})]", dim=True)
            self.rendered = True
        elif ev.event == EVENT_TOOL_RESULT:
            self._newline_if_needed()
            name = terminal_text(str(data.get("name", "")))
            limit = None if self.verbose else _RESULT_PREVIEW_CHARS
            result = terminal_text(_json_preview(data.get("result", ""), limit))
            marker = " (error)" if data.get("is_error") else ""
            click.secho(f"[tool_result{marker}: {name} -> {result}]", dim=True)
            self.rendered = True
        elif ev.event == EVENT_MESSAGE_END:
            self.thread_id = data.get("thread_id") or self.thread_id
            self.run_id = data.get("run_id") or self.run_id
            self.usage = data.get("usage") if isinstance(data.get("usage"), dict) else self.usage
            self.latency_ms = data.get("latency_ms", self.latency_ms)
            self.status = data.get("status", self.status)
            approval = data.get("approval")
            if self.status == STATUS_AWAITING_APPROVAL:
                self.approval = approval if isinstance(approval, dict) else {}
        elif ev.event == EVENT_ERROR:
            self._newline_if_needed()
            code = str(data.get("code", "error")) if data else "error"
            message = str(data.get("message", ev.raw)) if data else str(ev.raw)
            self.run_id = data.get("run_id") or self.run_id
            self.thread_id = data.get("thread_id") or self.thread_id
            raise AgentError(code, message, run_id=self.run_id)

        if self.verbose and ev.event == EVENT_MESSAGE_DELTA:
            self._trace(ev)

    def outcome(self) -> RunOutcome:
        return RunOutcome(
            thread_id=self.thread_id,
            run_id=self.run_id,
            rendered=self.rendered,
            usage=self.usage,
            latency_ms=self.latency_ms,
            status=self.status,
            approval=self.approval,
        )


def render_chat_events(
    events: Iterable[SseEvent],
    *,
    verbose: bool = False,
    renderer: _ChatRenderer | None = None,
) -> RunOutcome:
    """Render a stream of chat events; raises :class:`AgentError` on ``error``.

    Pass ``renderer`` to keep what the stream announced (the thread id, how
    many events arrived) when it ends with an exception.
    """
    renderer = renderer or _ChatRenderer(verbose=verbose)
    try:
        for ev in events:
            renderer.handle(ev)
    finally:
        renderer.finish()
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
    port: int | None = None,
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
        port=port,
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


def _thread_lines(
    thread_id: str, *, resume_flags: str, resumable: bool, server_stopped: bool = False
) -> list[str]:
    """The thread id and how to continue it (or why it cannot be continued).

    ``server_stopped``: the local server went away (crashed, or was stopped
    after a failure) whatever ``--start-server`` said; with an in-memory
    checkpointer its threads went with it.
    """
    lines = [f"Thread: {thread_id}"]
    if resumable:
        hint = "  (re-supply the redacted credential values)" if REDACTED in resume_flags else ""
        lines.append(
            f'  Resume with: graph-agents-cli run "<message>"{resume_flags}'
            f" --thread-id {thread_id}{hint}"
        )
    elif server_stopped:
        lines.append(
            "  The local server stopped, and its in-memory checkpointer lost this thread: the "
            "next run starts a new one (CHECKPOINTER=postgres keeps threads across restarts)."
        )
    else:
        lines.append(
            "  One-off server with an in-memory checkpointer: add --start-server to "
            "keep the server (and its threads) alive so you can resume with --thread-id."
        )
    return lines


def _print_footer(
    outcome: RunOutcome,
    *,
    resume_flags: str,
    keep_server: bool,
    checkpointer: str,
    after_error: bool = False,
    pending: Approval | None = None,
    approval_flags: str = "",
    mode: str = DEFAULT_RUN_MODE,
    kept_for_approval: bool = False,
) -> None:
    """Usage, then the thread and the command that continues it.

    Printed after an ``error`` event as well (without the "no response"
    note): the thread keeps every turn that finished and can be continued.
    A run paused on a gated call (``pending``) cannot take a new message until
    the call is decided: the footer gives the commands that decide it instead.
    """
    if not outcome.rendered and not after_error:
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
    if after_error and outcome.run_id:
        click.secho(f"Run: {outcome.run_id}", dim=True)
    if pending is not None:
        click.echo()
        lines = (
            _a2a_awaiting_lines(pending)
            if mode == "a2a"
            else awaiting_lines(pending, pending.thread_id or outcome.thread_id, approval_flags)
        )
        click.secho(lines[0], fg="yellow", bold=True)
        for line in lines[1:]:
            click.secho(line, fg="yellow")
        if kept_for_approval:
            click.secho(
                "  The local server was kept running: its in-memory checkpointer holds the paused "
                "run. Stop it with `graph-agents-cli run --stop-server` once it is decided.",
                dim=True,
            )
        return
    if not outcome.thread_id:
        return
    click.echo()
    resumable = keep_server or checkpointer == "postgres"
    for line in _thread_lines(outcome.thread_id, resume_flags=resume_flags, resumable=resumable):
        click.secho(line, dim=True)


# ---------------------------------------------------------------------------
# Protocol handlers
# ---------------------------------------------------------------------------


def _query_chat(
    target: RunTarget,
    prompt: str,
    *,
    thread_id: str | None,
    renderer: _ChatRenderer,
    display_message: str,
) -> RunOutcome:
    click.echo(f"[user]: {display_message}")
    events = post_chat(target.base_url, prompt, thread_id=thread_id, headers=target.headers)
    return render_chat_events(events, renderer=renderer)


def _one_line(chunk: Any) -> str:
    """An A2A stream response on one line (protobuf text format, else ``str``)."""
    try:
        from google.protobuf import text_format

        return text_format.MessageToString(chunk, as_one_line=True)
    except Exception:
        return " ".join(str(chunk).split())


_INPUT_REQUIRED_STATES = frozenset(
    {"TASK_STATE_INPUT_REQUIRED", "input-required", "input_required"}
)


def _input_required(data: Any) -> bool:
    if isinstance(data, dict):
        if data.get("state") in _INPUT_REQUIRED_STATES:
            return True
        return any(_input_required(value) for value in data.values())
    if isinstance(data, list):
        return any(_input_required(item) for item in data)
    return False


def _approval_in(data: Any) -> dict[str, Any] | None:
    if isinstance(data, dict):
        if isinstance(data.get("approval_id"), str) and ("method" in data or "api" in data):
            return data
        for value in data.values():
            found = _approval_in(value)
            if found is not None:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _approval_in(item)
            if found is not None:
                return found
    return None


def find_approval_payload(data: Any) -> dict[str, Any] | None:
    """The approval an A2A stream response (as a dict) asks for, or None.

    A gated run moves the task to ``input-required`` with a data part holding
    the approval (``approval_id``, ``api``, ``method``, ``path``, ...). A data
    part that only names an approval (a decision sent back) is not one.
    """
    return _approval_in(data) if _input_required(data) else None


def _a2a_approval(chunk: Any) -> dict[str, Any] | None:
    try:
        from google.protobuf.json_format import MessageToDict

        data = MessageToDict(chunk, preserving_proto_field_name=True)
    except Exception:
        return None
    return find_approval_payload(data)


def _query_a2a(
    target: RunTarget,
    prompt: str,
    *,
    thread_id: str | None,
    renderer: _ChatRenderer,
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

    verbose = renderer.verbose

    async def _go() -> RunOutcome:
        req_headers = dict(target.headers)
        req_headers.setdefault(VERSION_HEADER, PROTOCOL_VERSION_1_0)
        context_id: str | None = None

        def _render_parts(parts: Iterable[Any]) -> None:
            for part in parts:
                text = getattr(part, "text", "")
                if text:
                    renderer.write_text(text)
                elif getattr(part, "url", ""):
                    renderer.write_text(f"\n[file: {part.url}]")

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
                    renderer.events += 1
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
                    approval = _a2a_approval(chunk)
                    if approval is not None:
                        renderer.status = STATUS_AWAITING_APPROVAL
                        renderer.approval = approval
                    # Kept on the renderer so a failure mid-stream can still name the thread.
                    renderer.thread_id = context_id or thread_id
                    if verbose:
                        renderer._newline_if_needed()
                        click.secho(f"a2a: {_one_line(chunk)}", dim=True)
            finally:
                renderer.finish()
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


def _read_timeout_message(
    thread_id: str | None,
    resume_flags: str,
    *,
    local_server_kept: bool | None = True,
    thread_kept: bool = True,
) -> str:
    """``local_server_kept``: None for a remote agent, else whether the local server keeps
    running; ``thread_kept``: whether the thread outlives this command (False for a stopped
    one-off server with an in-memory checkpointer)."""
    seconds = _chat_client.STREAM_TIMEOUT.read
    text = (
        f"No event from the agent for {seconds:.0f} s; the run may still be in progress "
        "on the server (a long tool call or a non-streaming model phase)."
    )
    if local_server_kept is True:
        text += "\n  The server was left running."
    elif local_server_kept is False:
        text += "\n  The one-off local server was stopped."
    if thread_id and thread_kept:
        text += f'\n  Check the thread later with: graph-agents-cli run "<message>"{resume_flags} --thread-id {thread_id}'
    elif thread_id:
        text += (
            "\n  Its in-memory thread went with it; pass --start-server to keep the server "
            "(and its threads) between runs."
        )
    return text


def _transport_failure_message(
    exc: httpx.TransportError,
    *,
    url: str | None,
    renderer: _ChatRenderer,
    resume_flags: str,
    resumable: bool,
) -> str:
    """Tell "never reached" from "dropped after the run started", with the thread to continue."""
    where = f"the remote agent at {url}" if url else "the local server"
    if renderer.events == 0:
        if url is None:
            return (
                f"Could not reach the local server: {exc}\n"
                "  It has been stopped; retry to start a fresh one."
            )
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            return (
                f"Could not reach remote agent at: {url}\n"
                f"  {exc}\n"
                "  Check that the URL is correct and the service is running."
            )
        return (
            f"The connection to {where} failed before the run started: {exc}\n"
            "  Nothing was answered; retry, and check the service (and any proxy in between)."
        )
    incomplete = "The answer above is incomplete. " if renderer.rendered else ""
    kept = "; the thread keeps every turn that finished." if resumable else "."
    lines = [
        f"The connection to {where} dropped after the run started: {exc}",
        f"  {incomplete}The run was interrupted (the server stopped, or the connection was "
        f"cut){kept}",
    ]
    if url is None:
        lines.append(
            "  The local server has been stopped (its log: .graph-agents-cli/run_server.log)."
        )
    if renderer.thread_id:
        thread = _thread_lines(
            renderer.thread_id,
            resume_flags=resume_flags,
            resumable=resumable,
            server_stopped=url is None,
        )
        lines.extend(f"  {line}" for line in thread)
    return "\n".join(lines)


def _project_auth_policy(*, remote: bool) -> str | None:
    """The auth policy of the project in the current directory, if any (for hints only).

    For the local server, ``AUTH_POLICY`` from the environment or the project's
    ``.env`` (what that server reads) wins over the manifest. A deployed agent
    runs the manifest's policy (the chart's ``AUTH_POLICY``), so ``remote``
    reads the manifest alone.
    """
    import os

    root = find_project_root(Path.cwd())
    if root is None:
        return None
    from graph_agents_cli._defaults import normalize_auth_policy

    policy = "" if remote else (os.environ.get("AUTH_POLICY") or "").strip()
    env_path = root / ".env"
    if not policy and not remote and env_path.is_file():
        from dotenv import dotenv_values

        try:
            policy = (dotenv_values(env_path).get("AUTH_POLICY") or "").strip()
        except Exception:
            policy = ""
    if not policy:
        try:
            policy = read_project_config(str(root)).auth_policy
        except click.ClickException:
            return None
    return normalize_auth_policy(policy, warn=False)


_TOKEN_IN_ENV = (
    f"export {API_KEY_ENV}=<credential>: sent as 'Authorization: Bearer <credential>', and kept "
    "out of argv and shell history (unlike --header)"
)


def _auth_hint(policy: str | None, *, remote: bool) -> str:
    """How to send the credential the project's policy expects (bearer credentials via env)."""
    shared = f"shared-bearer: export {API_KEY_ENV}=<API_KEY>" + (
        "" if remote else " (a local run otherwise sends the API_KEY in .env)"
    )
    jwt = f"jwt: export {API_KEY_ENV}=<token>" + (
        ""
        if remote
        else "; for a local token: "
        f'export {API_KEY_ENV}="$(graph-agents-cli auth dev-token --sub <user>)"'
    )
    custom = "custom: pass what the policy reads with --header 'Name: value' or --cookie name=value"
    by_policy = {"shared-bearer": shared, "jwt": jwt, "custom": custom}
    if policy in by_policy:
        return f"\n  Authentication failed. {by_policy[policy]}."
    return (
        f"\n  Authentication failed. Send a bearer credential with {_TOKEN_IN_ENV}."
        f"\n    {shared}\n    {jwt}\n    {custom}"
    )


def _unavailable_hint(body: str, *, remote: bool) -> str:
    """A 503: which setting the server says it is missing (the body names the policy)."""
    if "AUTH_POLICY=jwt" in body:
        if remote:
            return (
                "\n  The server has no usable JWT settings: set AUTH_JWT_JWKS_URL (or "
                "AUTH_JWT_PUBLIC_KEY), AUTH_JWT_ISSUER and AUTH_JWT_AUDIENCE in the "
                "environment's chart values (the server log names the problem)."
            )
        return (
            "\n  The local server has no usable JWT settings. For local runs, "
            "`graph-agents-cli auth dev-token --sub <user>` writes a dev key, issuer and "
            "audience to .env and prints a token; then restart a kept server with "
            "`graph-agents-cli run --stop-server`. Or set AUTH_JWT_JWKS_URL to your issuer's keys."
        )
    if "API_KEY" in body:
        if remote:
            return (
                "\n  The server has no API_KEY: `graph-agents-cli secrets apply --env <env>` "
                "puts one in the app Secret."
            )
        return (
            "\n  API_KEY is not set: `graph-agents-cli login --write-env` generates one into .env."
        )
    if "AUTH_POLICY=custom" in body:
        return (
            "\n  The custom auth policy is still the fail-closed stub: implement "
            "CustomPolicy in <agent_directory>/policies/custom.py."
        )
    if "signing keys" in body:
        return "\n  The token issuer's keys (AUTH_JWT_JWKS_URL) are unreachable from the server."
    return "\n  The server refused the request (see its log)."


def _http_error_hint(
    exc: ChatHTTPError,
    *,
    remote: bool,
    thread_id: str | None,
    policy: str | None = None,
    approval_flags: str = "",
) -> str:
    if exc.status_code == 401:
        return _auth_hint(policy, remote=remote)
    if exc.status_code == 403:
        return (
            "\n  The agent knows who you are but refused the request (another principal's "
            "thread, or an action your roles do not allow)."
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
    if exc.status_code == 409 and APPROVAL_PENDING in (exc.body or ""):
        where = f" --thread-id {shlex.quote(thread_id)}" if thread_id else ""
        return (
            "\n  A run on this thread is paused on a call waiting for approval: decide it first "
            f"(graph-agents-cli approvals list{where}{approval_flags}), then send the next message."
        )
    if exc.status_code == 409:
        return "\n  The thread already has a run in progress; retry when it has finished."
    if exc.status_code == 503:
        return _unavailable_hint(exc.body or "", remote=remote)
    return ""


def _interactive() -> bool:
    """Whether a human can answer a prompt here (stdin and stdout are terminals)."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def print_approval(approval: Approval) -> None:
    """The paused call, in full, before anyone decides it."""
    click.echo()
    click.secho("Approval required before this call is sent:", fg="yellow", bold=True)
    for line in approval.lines():
        click.echo(line)


def _ask_decision() -> str | None:
    """``approve`` or ``reject`` from "Approve? [y/N]"; None when the prompt is abandoned."""
    try:
        approve = click.confirm("Approve?", default=False)
    except click.Abort:  # Ctrl-C or end of input: the approval stays pending
        click.echo()
        return None
    return DECISION_APPROVE if approve else DECISION_REJECT


class DecisionRefused(click.ClickException):
    """The server refused a decision (not an approver, already decided, expired): exit 1."""

    exit_code = 1


def decision_refused_message(
    exc: ChatHTTPError, approval_id: str, *, remote: bool, policy: str | None = None
) -> str:
    """Why the server refused to record a decision, in words."""
    if exc.status_code == 401:
        return "The decision was refused (HTTP 401): not authenticated." + _auth_hint(
            policy, remote=remote
        )
    text = f"The decision on approval {safe_text(approval_id)} was refused (HTTP {exc.status_code})"
    reason = DECISION_REFUSALS.get(exc.status_code)
    return f"{text}: {reason}." if reason else f"{text}:\n  {exc.body}"


def _settle_approvals(
    target: RunTarget,
    renderer: _ChatRenderer,
    outcome: RunOutcome,
    *,
    interactive: bool,
) -> tuple[RunOutcome, Approval | None]:
    """Show each call the run pauses on; on a terminal, ask for the decision and resume.

    Returns the final outcome and the approval still pending (None when the
    run finished). The prompt appears only when the requester is one of the
    approvers (a requester never decides a call gated for others) and the run
    used the chat API; otherwise the approval is left pending for the
    commands the footer prints.
    """
    while outcome.status == STATUS_AWAITING_APPROVAL:
        approval = Approval.from_payload(outcome.approval, thread_id=outcome.thread_id)
        if approval is not None and outcome.thread_id:
            # The decision is about this run's own thread, whatever the payload names.
            approval = replace(approval, thread_id=outcome.thread_id)
        if approval is None:
            raise click.ClickException(
                "The run paused for an approval, but the server named no approval id, so it "
                "cannot be decided from here (see the server log)."
            )
        print_approval(approval)
        if not (
            interactive
            and target.mode == DEFAULT_RUN_MODE
            and approval.thread_id
            and approval.requester_may_decide
        ):
            return outcome, approval
        decision = _ask_decision()
        if decision is None:
            return outcome, approval
        if decision == DECISION_APPROVE:
            click.secho("Approved: the call is sent as shown; the run resumes.", fg="green")
        else:
            click.secho("Rejected: the call is not sent; the run resumes.", fg="yellow")
        renderer.resume()
        try:
            events = _chat_client.decide_approval(
                target.base_url,
                approval.thread_id,
                approval.approval_id,
                decision,
                headers=target.headers,
            )
            outcome = render_chat_events(events, renderer=renderer)
        except ChatHTTPError as exc:
            raise DecisionRefused(
                decision_refused_message(
                    exc,
                    approval.approval_id,
                    remote=target.remote,
                    policy=_project_auth_policy(remote=target.remote),
                )
            ) from exc
        outcome = outcome._replace(thread_id=outcome.thread_id or approval.thread_id)
    return outcome, None


def _a2a_awaiting_lines(approval: Approval) -> list[str]:
    """How to decide an approval an A2A task asks for (a message on the same task)."""
    decision = {"approval_id": approval.approval_id, "decision": "approve"}
    who = ", ".join(safe_text(a) for a in approval.approvers) or "an allowed approver"
    return [
        f"Awaiting approval by {who}: the task is input-required; the call was not sent.",
        "  Decide it with a message on the same task carrying the data part "
        f'{safe_text(json.dumps(decision))} (or "decision": "reject"); '
        "`graph-agents-cli run --mode chat` asks on a terminal instead.",
    ]


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
    help=(
        "Custom HTTP header ('Key: Value'). Repeatable. An Authorization header overrides "
        "GRAPH_AGENTS_CLI_API_KEY; prefer the variable for bearer credentials (argv is visible "
        "to other local users)."
    ),
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
    "--port",
    type=click.IntRange(1, 65535),
    default=None,
    help=(
        "Port for the local server this run starts (default: the first free one of "
        "18080-18089, or GRAPH_AGENTS_CLI_RUN_PORT). Refused when the port is in use."
    ),
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    default=False,
    help="Also print each event on one compact line (text deltas are counted, not repeated).",
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
    port: int | None,
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
    request restarts it. The server listens on the first free port of
    18080-18089, or on --port / GRAPH_AGENTS_CLI_RUN_PORT when given.

    \b
    Use --url to query a deployed agent instead. --mode selects the protocol
    (default chat). Credentials follow the project's auth policy, locally and
    with --url. Put a bearer credential in GRAPH_AGENTS_CLI_API_KEY (sent as
    'Authorization: Bearer <value>'): unlike --header, it stays out of the
    process list and your shell history.
      shared-bearer     GRAPH_AGENTS_CLI_API_KEY=<API_KEY> (locally: the API_KEY in .env)
      jwt               GRAPH_AGENTS_CLI_API_KEY=<token> (locally: auth dev-token)
      custom            --header 'Name: value' or --cookie name=value

    \b
    --thread-id continues a conversation; the footer of every run, one that
    ends with an error included, prints the thread id and the command to
    resume it. --file attaches UTF-8 text files as extra context.

    \b
    A call gated by the API policy (an approval block in api-policy.yaml)
    pauses the run before it is sent, and the call is printed in full. On a
    terminal, when the requester is an approver, "Approve? [y/N]" decides
    it (Enter rejects) and the run continues. Otherwise the run ends with an
    "Awaiting approval" line and the `graph-agents-cli approvals approve` /
    `reject` commands that decide it; a one-off local server with an
    in-memory checkpointer is then kept running so the paused run survives.

    \b
    Exit codes:
      0  the agent answered, or the run is awaiting an approval
      1  the agent refused or reported an error (HTTP error, error event), or
         the decision was refused (not an approver, already decided, expired)
      2  the agent could not be reached or went silent
      3  configuration error (no project, port unavailable)
    """
    mode = mode.lower()
    # The deprecated --session-token is an X-Session-Token header from here on.
    header = fold_session_token(header, session_token)
    session_token = None
    if url and start_server:
        click.secho(
            "Warning: --start-server has no effect when using --url.", fg="yellow", err=True
        )
    if url and port is not None:
        click.secho("Warning: --port has no effect when using --url.", fg="yellow", err=True)

    # SIGTERM/SIGHUP unwind like Ctrl-C, so the server this run starts is always
    # stopped and its pid file removed (an IDE stop button, a CI timeout, kill).
    with terminate_like_interrupt():
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
            port=port,
        )
        if url:
            click.echo(f"Querying remote agent: {url} (mode: {target.mode})")

        resume_flags = _build_resume_flags(url, target.mode, header, cookie, session_token)
        # Only tear down a server this invocation started; a reused persistent
        # server (e.g. from --start-server) is left running.
        should_stop_server = not url and not start_server and target.started_server
        keep_server = bool(url) or not should_stop_server

        handler = _query_a2a if target.mode == "a2a" else _query_chat
        renderer = _ChatRenderer(verbose=verbose)
        failed: AgentError | None = None
        pending: Approval | None = None
        kept_for_approval = False
        try:
            try:
                outcome = handler(
                    target,
                    prompt,
                    thread_id=thread_id,
                    renderer=renderer,
                    display_message=message,
                )
                outcome, pending = _settle_approvals(
                    target, renderer, outcome, interactive=_interactive()
                )
                if pending is not None and should_stop_server and target.checkpointer != "postgres":
                    # The paused run lives in this server's memory: stopping it
                    # would drop the approval the footer tells the user to decide.
                    should_stop_server = False
                    kept_for_approval = True
            except AgentError as exc:
                click.secho(f"[error: {exc.code}]: {exc.message}", fg="red")
                failed = exc
                outcome = renderer.outcome()._replace(thread_id=renderer.thread_id or thread_id)
            except ChatHTTPError as exc:
                hint = _http_error_hint(
                    exc,
                    remote=bool(url),
                    thread_id=thread_id,
                    policy=_project_auth_policy(remote=bool(url)),
                    approval_flags=_build_resume_flags(url, DEFAULT_RUN_MODE, header, cookie, None),
                )
                raise click.ClickException(
                    f"Agent request failed (HTTP {exc.status_code}):\n  {exc.body}{hint}"
                ) from exc
            except httpx.ReadTimeout as exc:
                # The server answered and then went quiet (a long tool call or a
                # non-streaming model phase): it is neither unreachable nor wedged,
                # so a reused/persistent server is left alone and the message
                # says what happened. ReadTimeout is a TransportError: keep this first.
                raise _TransportFailure(
                    _read_timeout_message(
                        renderer.thread_id or thread_id,
                        resume_flags,
                        local_server_kept=None if url else not should_stop_server,
                        thread_kept=bool(url)
                        or not should_stop_server
                        or target.checkpointer == "postgres",
                    )
                ) from exc
            except httpx.TransportError as exc:
                if not url:
                    # The local server is unreachable, wedged or gone: stop it (even
                    # one we reused) so a later retry starts a fresh one.
                    should_stop_server = True
                raise _TransportFailure(
                    _transport_failure_message(
                        exc,
                        url=url,
                        renderer=renderer,
                        resume_flags=resume_flags,
                        # A stopped local server keeps only postgres threads.
                        resumable=bool(url) or target.checkpointer == "postgres",
                    )
                ) from exc
        finally:
            if should_stop_server:
                # cwd is the project root here (set by _resolve_target).
                stop_server(Path.cwd(), pid=target.server_pid)

        # After an error event too: the thread keeps every turn that finished.
        _print_footer(
            outcome,
            resume_flags=resume_flags,
            keep_server=keep_server,
            checkpointer=target.checkpointer,
            after_error=failed is not None,
            pending=pending,
            # `approvals` talks to the chat API whatever --mode this run used.
            approval_flags=_build_resume_flags(url, DEFAULT_RUN_MODE, header, cookie, None),
            mode=target.mode,
            kept_for_approval=kept_for_approval,
        )
        if failed is not None:
            raise click.exceptions.Exit(1) from failed
