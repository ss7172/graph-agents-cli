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

"""graph-agents-cli approvals commands: list and decide the calls agent runs wait on.

A call an API's ``approval`` block gates is not sent until an allowed
approver decides it: ``requester`` (the principal who started the run) and/or
``role:<name>`` (another principal holding that role). These commands are the
client of the chat API's approval routes, locally or against a deployed agent
(``--url``), with the credentials ``run`` sends:

* ``GET /threads/{thread_id}/approvals`` lists a thread's approvals;
* ``GET /approvals`` lists, across threads, the ones the caller may see (its
  own, the ones its roles may decide, every one for a read-across role);
  against an agent without it, the caller's own threads are searched;
* ``POST /threads/{thread_id}/approvals/{approval_id}`` decides one and
  streams the resumed run (the same events as ``POST /chat``).

The decision carries only ``decision`` and ``comment``: the server sends
exactly the call it recorded, once, or nothing.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, NamedTuple

import click
import httpx

from graph_agents_cli import _chat_client
from graph_agents_cli._approvals import STATUS_PENDING, Approval, awaiting_lines, safe_text
from graph_agents_cli._chat_client import (
    DECISION_APPROVE,
    DECISION_REJECT,
    STATUS_AWAITING_APPROVAL,
    ChatClientError,
    ChatHTTPError,
)
from graph_agents_cli._click import LazyGroup
from graph_agents_cli._project import (
    chdir_project_root,
    read_project_config,
    require_agent_directory,
)
from graph_agents_cli._remote import API_KEY_ENV, build_headers
from graph_agents_cli.run import _local_server
from graph_agents_cli.run._signals import terminate_like_interrupt
from graph_agents_cli.run.cmd_run import (
    AgentError,
    DecisionRefused,
    _auth_hint,
    _build_resume_flags,
    _ChatRenderer,
    _local_api_key,
    _project_auth_policy,
    _TransportFailure,
    decision_refused_message,
    print_approval,
    render_chat_events,
)

# What `list` / a decision without --thread-id look through: GET /approvals
# pages of 100 (or, against an agent without that route, the caller's most
# recently active threads, GET /threads pages of 100).
THREAD_PAGE = 100
MAX_THREAD_PAGES = 5


class NoPendingRun(click.ClickException):
    """Nothing can be pending where the command looked (exit 1)."""

    exit_code = 1


class _Target(NamedTuple):
    base_url: str
    headers: dict[str, str]
    remote: bool
    flags: str  # repeated in printed commands (credentials redacted)


def _local_checkpointer(project_root: Path, configured: str) -> str:
    """The checkpointer a local server would run with (the environment, then .env, then the manifest)."""
    env = {**_local_server.dotenv_settings(project_root / ".env"), **os.environ}
    return (env.get("CHECKPOINTER") or configured or "memory").strip().lower()


@contextmanager
def _target(url: str | None, header: tuple[str, ...], cookie: tuple[str, ...]) -> Iterator[_Target]:
    """Where the approvals live: ``--url``, or the project's local server.

    Locally, the running server (``run --start-server``, or the one a paused
    run kept) is used. With none running, a temporary server is started (and
    stopped afterwards) where a paused run can outlive its server (a postgres
    checkpointer, or ``langgraph dev``); under ``fastapi`` with the in-memory
    checkpointer nothing can be pending.
    """
    headers = build_headers(header, cookie)
    flags = _build_resume_flags(url, "chat", header, cookie, None)
    if url:
        yield _Target(url.rstrip("/"), headers, True, flags)
        return
    chdir_project_root()
    cfg = read_project_config()
    require_agent_directory(cfg)
    root = Path.cwd()
    if not any(k.lower() == "authorization" for k in headers):
        api_key = _local_api_key(root)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
    port = _local_server.get_server_port(root)
    if port is not None:
        _local_server.touch_activity(root)
        yield _Target(f"http://127.0.0.1:{port}", headers, False, flags)
        return
    runtime = getattr(cfg, "runtime", "fastapi") or "fastapi"
    checkpointer = _local_checkpointer(root, getattr(cfg, "checkpointer", "memory") or "memory")
    # `langgraph dev` keeps its own state across restarts; an in-memory fastapi
    # server's paused runs die with it.
    if runtime == "fastapi" and checkpointer != "postgres":
        raise NoPendingRun(
            "No local server is running, and with the in-memory checkpointer (CHECKPOINTER="
            f"{checkpointer}) a paused run lives only in the server that ran it: nothing is "
            "pending. A run that pauses keeps its one-off server running; `run --start-server` "
            "keeps one explicitly. Pass --url for a deployed agent."
        )
    with terminate_like_interrupt():
        server = _local_server.ensure_server(
            root, cfg.agent_directory, runtime=runtime, checkpointer=checkpointer
        )
        try:
            yield _Target(server.base_url, headers, False, flags)
        finally:
            if server.started:
                _local_server.stop_server(root, pid=server.pid)


def _http_failure(exc: ChatHTTPError, target: _Target, what: str) -> click.ClickException:
    if exc.status_code == 401:
        hint = _auth_hint(_project_auth_policy(remote=target.remote), remote=target.remote)
        return click.ClickException(f"{what} failed (HTTP 401): not authenticated.{hint}")
    if exc.status_code == 403:
        return click.ClickException(
            f"{what} failed (HTTP 403): you may not see this thread's approvals (its owner, its "
            "approvers and read-across roles may)."
        )
    if exc.status_code == 404:
        return click.ClickException(
            f"{what} failed (HTTP 404): no such thread (a wrong id, a deleted thread, or an "
            "agent without approval routes: check --url)."
        )
    return click.ClickException(f"{what} failed (HTTP {exc.status_code}):\n  {exc.body}")


def _transport_failure(exc: httpx.TransportError, target: _Target) -> _TransportFailure:
    where = target.base_url if target.remote else "the local server"
    return _TransportFailure(f"Could not reach {where}: {exc}")


def _thread_approvals(target: _Target, thread_id: str) -> list[Approval]:
    """A thread's approvals; each is decided on the thread it was listed for."""
    rows = _chat_client.list_approvals(target.base_url, thread_id, headers=target.headers)
    found = [Approval.from_payload(row, thread_id=thread_id) for row in rows]
    return [replace(a, thread_id=thread_id) for a in found if a is not None]


def _own_thread_ids(target: _Target) -> tuple[list[str], bool]:
    """The caller's thread ids (most recent first, as the server lists them); True when cut."""
    ids: list[str] = []
    for page in range(MAX_THREAD_PAGES):
        rows = _chat_client.list_threads(
            target.base_url, headers=target.headers, limit=THREAD_PAGE, offset=page * THREAD_PAGE
        )
        ids.extend(str(r["thread_id"]) for r in rows if isinstance(r.get("thread_id"), str))
        if len(rows) < THREAD_PAGE:
            return ids, False
    return ids, True


def _visible_approvals(
    target: _Target, *, pending_only: bool
) -> tuple[list[Approval], bool] | None:
    """GET /approvals, page by page: every approval the caller may see; True when cut.

    None when the agent has no such route (404/405 on the first page).
    """
    found: list[Approval] = []
    for page in range(MAX_THREAD_PAGES):
        try:
            rows = _chat_client.list_visible_approvals(
                target.base_url,
                status=STATUS_PENDING if pending_only else None,
                headers=target.headers,
                limit=THREAD_PAGE,
                offset=page * THREAD_PAGE,
            )
        except ChatHTTPError as exc:
            if page == 0 and exc.status_code in (404, 405):
                return None
            raise
        for row in rows:
            approval = Approval.from_payload(row)
            if approval is not None and approval.thread_id:
                found.append(approval)
        if len(rows) < THREAD_PAGE:
            return found, False
    return found, True


class _Collected(NamedTuple):
    approvals: list[Approval]
    cut: bool  # more than the pages looked through
    own_threads_only: bool  # the fallback scan: another principal's thread needs --thread-id


def _collect(target: _Target, thread_id: str | None, *, pending_only: bool = False) -> _Collected:
    """Approvals of one thread, or every one the caller may see (see the module doc)."""
    if thread_id:
        return _Collected(_thread_approvals(target, thread_id), False, False)
    visible = _visible_approvals(target, pending_only=pending_only)
    if visible is not None:
        return _Collected(visible[0], visible[1], False)
    thread_ids, cut = _own_thread_ids(target)
    approvals: list[Approval] = []
    for tid in thread_ids:
        try:
            approvals.extend(_thread_approvals(target, tid))
        except ChatHTTPError as exc:
            if exc.status_code in (403, 404):  # deleted meanwhile, or not visible: skip it
                continue
            raise
    return _Collected(approvals, cut, True)


def _common_options(function: Callable[..., Any]) -> Callable[..., Any]:
    function = click.option(
        "--cookie",
        "cookie",
        multiple=True,
        help="Cookie ('name=value'), for a custom auth policy that reads cookies. Repeatable.",
    )(function)
    function = click.option(
        "--header",
        "-H",
        "header",
        multiple=True,
        help=(
            "Custom HTTP header ('Key: Value'). Repeatable. An Authorization header overrides "
            f"{API_KEY_ENV}; prefer the variable for bearer credentials."
        ),
    )(function)
    function = click.option(
        "--url",
        default=None,
        help="Base URL of a deployed agent. Without it, the project's local server.",
    )(function)
    return function


@click.group("approvals", cls=LazyGroup)
def approvals_group() -> None:
    """List and decide the gated API calls agent runs are waiting on.

    A call gated by an API's approval block in api-policy.yaml is not sent
    until an approver decides it: requester (the principal who started the
    run) and/or role:<name> (a principal holding that role; a requester
    decides their own call only when requester is listed). Approving sends
    exactly the call shown, once; rejecting, or letting it expire, sends
    nothing and the agent is told it was not approved.

    \b
    Credentials follow the project's auth policy, as for `run`: put a bearer
    credential in GRAPH_AGENTS_CLI_API_KEY (locally, the API_KEY in .env is
    used when it is unset), or pass --header / --cookie. Each approver uses
    their own credential.

    \b
    Exit codes:
      0  listed, or decided (the resumed run was shown)
      1  refused (not an allowed approver, already decided, expired, not
         found), or the resumed run ended with an error
      2  the agent could not be reached
      3  configuration error (not in a project without --url)
    """


@approvals_group.command("list")
@_common_options
@click.option(
    "--thread-id",
    default=None,
    help=(
        "The thread to list. Without it: every approval you may see (your own, the ones "
        "your roles may decide, all of them for a read-across role; the newest "
        f"{THREAD_PAGE * MAX_THREAD_PAGES})."
    ),
)
@click.option(
    "--all", "show_all", is_flag=True, default=False, help="Also list decided and expired ones."
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Print JSON.")
def cmd_list(
    url: str | None,
    header: tuple[str, ...],
    cookie: tuple[str, ...],
    thread_id: str | None,
    show_all: bool,
    as_json: bool,
) -> None:
    """List pending approvals (a thread's, or every one you may see)."""
    try:
        with _target(url, header, cookie) as target:
            flags = target.flags
            try:
                approvals, cut, own_threads_only = _collect(
                    target, thread_id, pending_only=not show_all
                )
            except ChatHTTPError as exc:
                raise _http_failure(exc, target, "Listing approvals") from exc
            except httpx.TransportError as exc:
                raise _transport_failure(exc, target) from exc
            except ChatClientError as exc:
                raise click.ClickException(f"Listing approvals failed: {exc}") from exc
    except NoPendingRun as exc:
        if as_json:
            click.echo(json.dumps({"approvals": [], "note": exc.message}, indent=2))
        else:
            click.echo(exc.message)
        return
    shown = [a for a in approvals if show_all or a.pending]
    if as_json:
        payload = {"approvals": [dict(a.raw, thread_id=a.thread_id) for a in shown]}
        if cut:
            payload["truncated"] = True
        # ASCII-escaped: the call's text came from the model.
        click.echo(json.dumps(payload, indent=2))
        return
    what = "approvals" if show_all else "pending approvals"
    if thread_id:
        where = f"thread {safe_text(thread_id)}"
    else:
        where = "your threads" if own_threads_only else "the threads you may see"
    if not shown:
        click.echo(f"No {what} on {where}.")
    for approval in shown:
        click.echo()
        for line in approval.lines():
            click.echo(line)
        if approval.pending and approval.thread_id:
            for line in awaiting_lines(approval, approval.thread_id, flags)[1:3]:
                click.echo(line)
    if cut:
        searched = (
            f"your {THREAD_PAGE * MAX_THREAD_PAGES} most recent threads"
            if own_threads_only
            else f"the newest {THREAD_PAGE * MAX_THREAD_PAGES} approvals"
        )
        click.secho(
            f"Only {searched} were searched; pass --thread-id for an older one.", fg="yellow"
        )
    if own_threads_only:
        click.secho(
            "This agent lists approvals per thread only: one on another principal's thread "
            "(a role: approver's) needs --thread-id.",
            dim=True,
        )


def _find(target: _Target, approval_id: str, thread_id: str | None) -> Approval:
    approvals, cut, own_threads_only = _collect(target, thread_id)
    for approval in approvals:
        if approval.approval_id == approval_id:
            return approval
    if thread_id:
        where, extra = f"thread {safe_text(thread_id)}", ""
    elif own_threads_only:
        where = "your threads"
        extra = (
            "; an approval on another principal's thread needs --thread-id (the requester's "
            "`run` printed it)"
        )
    else:
        where, extra = "the threads you may see", ""
    if cut:
        extra += "; only the most recent ones were searched (pass --thread-id)"
    raise NoPendingRun(f"No approval {safe_text(approval_id)} on {where}{extra}.")


def _decide(
    decision: str,
    approval_id: str,
    *,
    url: str | None,
    header: tuple[str, ...],
    cookie: tuple[str, ...],
    thread_id: str | None,
    comment: str | None,
    verbose: bool,
) -> None:
    with _target(url, header, cookie) as target:
        try:
            approval = _find(target, approval_id, thread_id)
        except ChatHTTPError as exc:
            raise _http_failure(exc, target, "Looking up the approval") from exc
        except httpx.TransportError as exc:
            raise _transport_failure(exc, target) from exc
        except ChatClientError as exc:
            raise click.ClickException(f"Looking up the approval failed: {exc}") from exc
        # The decider sees the call before it is decided, whoever started the run.
        print_approval(approval)
        if not approval.pending:
            raise DecisionRefused(
                f"Approval {safe_text(approval_id)} is {safe_text(approval.status)}, not pending: "
                "an approval is decided once."
            )
        assert approval.thread_id is not None
        verb = "Approving" if decision == DECISION_APPROVE else "Rejecting"
        click.secho(
            f"{verb}; the resumed run follows.",
            fg="green" if decision == DECISION_APPROVE else "yellow",
        )
        renderer = _ChatRenderer(verbose=verbose)
        try:
            events = _chat_client.decide_approval(
                target.base_url,
                approval.thread_id,
                approval.approval_id,
                decision,
                comment=comment,
                headers=target.headers,
            )
            outcome = render_chat_events(events, renderer=renderer)
        except ChatHTTPError as exc:
            raise DecisionRefused(
                decision_refused_message(
                    exc,
                    approval_id,
                    remote=target.remote,
                    policy=_project_auth_policy(remote=target.remote),
                )
            ) from exc
        except AgentError as exc:
            click.secho(f"[error: {exc.code}]: {exc.message}", fg="red")
            raise click.exceptions.Exit(1) from exc
        except httpx.TransportError as exc:
            raise _TransportFailure(
                f"The connection dropped while the resumed run streamed: {exc}\n"
                "  The decision was recorded if the server answered; check with "
                f"`graph-agents-cli approvals list --all --thread-id {approval.thread_id}`."
            ) from exc
        if outcome.status == STATUS_AWAITING_APPROVAL:
            nxt = Approval.from_payload(outcome.approval, thread_id=approval.thread_id)
            if nxt is not None:
                nxt = replace(nxt, thread_id=approval.thread_id)
                print_approval(nxt)
                click.echo()
                lines = awaiting_lines(nxt, nxt.thread_id, target.flags)
                click.secho(lines[0], fg="yellow", bold=True)
                for line in lines[1:]:
                    click.secho(line, fg="yellow")


def _decision_command(decision: str) -> click.Command:
    approve = decision == DECISION_APPROVE
    summary = (
        "Approve a pending call: it is sent exactly as shown, once, and the run resumes."
        if approve
        else "Reject a pending call: it is never sent, and the run resumes without it."
    )

    @click.command(decision, help=summary, short_help=summary)
    @click.argument("approval_id")
    @_common_options
    @click.option(
        "--thread-id",
        default=None,
        help=(
            "The approval's thread (printed with the approval). Without it, every approval "
            "you may see is searched."
        ),
    )
    @click.option("--comment", default=None, help="Why; recorded with the decision.")
    @click.option(
        "--verbose", "-v", is_flag=True, default=False, help="Print each event of the resumed run."
    )
    def command(
        approval_id: str,
        url: str | None,
        header: tuple[str, ...],
        cookie: tuple[str, ...],
        thread_id: str | None,
        comment: str | None,
        verbose: bool,
    ) -> None:
        _decide(
            decision,
            approval_id,
            url=url,
            header=header,
            cookie=cookie,
            thread_id=thread_id,
            comment=comment,
            verbose=verbose,
        )

    return command


approvals_group.add_command(_decision_command(DECISION_APPROVE))
approvals_group.add_command(_decision_command(DECISION_REJECT))
