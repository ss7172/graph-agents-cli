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

"""graph-agents-cli peer commands: the other agents this agent asks, over A2A.

A peer is an API in ``api-policy.yaml`` with ``protocol: a2a``. ``peer add``
writes one, with everything the template's A2A client needs: the peer's
endpoint and card, the JSON-RPC methods it may send, the approve gate (or the
denial), the credential, limits for a model-bound peer, and the project's
files that follow (the manifest's ``secrets.keys``, ``.env.example``, the
chart's values). It then regenerates ``<agent_dir>/tools/a2a_peers.py`` from
the policy (``_generate.py``), the module that hands the model ``ask_agent``
and ``approve_agent_action``. ``peer remove``, ``peer sync`` keep that module in
step; ``peer list`` and ``peer show [--check]`` report. Every change is
validated with the runtime's rules, shown as one diff and written atomically
(``--dry-run`` stops after the diff). ``.env`` is never touched.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
from rich.markup import escape
from rich.table import Table

from graph_agents_cli._api_policy import (
    A2A_APPROVE,
    A2A_KEY,
    A2A_OPERATION_KEY,
    ALLOW_ACTORLESS_KEY,
    DESCRIPTION_KEY,
    DESCRIPTION_MAX_CHARS,
    EXCHANGE_KEY,
    MAX_APPROVAL_TIMEOUT_S,
    MAX_RESPONSE_BYTES_LIMIT,
    MIN_APPROVAL_TIMEOUT_S,
    POLICY_FILENAME,
    PROTOCOL_A2A,
    PROTOCOL_KEY,
    REQUESTER_APPROVER,
    RPC_METHOD_KEY,
    TOKEN_EXCHANGE_CLIENT_ID_ENV,
    api_protocol,
    auth_policy_findings,
    summarize,
    uses_exchange,
)
from graph_agents_cli._click import LazyGroup
from graph_agents_cli._output import Console, print_table
from graph_agents_cli.api import _changes as ch
from graph_agents_cli.api._files import (
    Plan,
    chart_values_files,
    env_example_add,
    env_example_remove,
    print_diff,
    read_text,
    sync_manifest,
    values_add,
    values_env_set,
    values_exchange_add,
    values_exchange_remove,
    values_remove,
)
from graph_agents_cli.api.cmd_api import (
    _API_KEY_ORDER,
    _edit,
    _load_project,
    _policy_header,
    _Project,
    _validate,
)
from graph_agents_cli.peer import _generate as gen
from graph_agents_cli.scaffold.utils.keyedit import YamlText, block_lines

PEER_LOCAL_URL = "http://localhost:8001"
DEFAULT_CALLS = ("ask", "status")
CALL_CHOICES = ("ask", "status", "cancel")
RELAY, DENY = gen.RELAY, gen.DENY
AUTH_BY_POLICY = {"jwt": "exchange", "custom": "forward", "shared-bearer": "bearer"}
DEFAULT_MAX_CALLS = 12
DEFAULT_READ_TIMEOUT_MS = 120_000
DEFAULT_CONNECT_TIMEOUT_MS = 2000
DEFAULT_MAX_RESPONSE_BYTES = 1_048_576
DEFAULT_APPROVAL_TIMEOUT_S = 900
CARD_TIMEOUT_S = 5.0
RUNTIME_MODULE = "app_utils/a2a_client.py"
NOTHING = "Nothing to change."
SHARED_BEARER_NOTE = (
    "under shared-bearer, any holder of API_KEY (another agent included) can decide requester "
    "gates: the agent behind {api} cannot tell this agent from the person"
)


class PeerCommandError(click.ClickException):
    """A check failed or the project cannot take the change (exit 3)."""

    exit_code = 3


@click.group("peer", cls=LazyGroup)
def peer_group() -> None:
    """Declare the other agents this agent asks, over A2A (its peers).

    A peer is an api-policy.yaml API with `protocol: a2a`. `peer add NAME`
    writes it (the endpoint, the JSON-RPC methods it may send, the approve gate
    or its denial, the credential, the limits) with the manifest, .env.example
    and chart values that follow, and regenerates <agent_dir>/tools/a2a_peers.py,
    which gives the model `ask_agent` and, for peers it relays approvals to,
    `approve_agent_action`. It then prints what is left to set, here and on the
    peer. Every change is validated, shown as one diff and written atomically;
    --dry-run prints the diff only. .env is never touched.

    \b
    Exit codes:
      0  changed, or nothing to change
      1  show --check: the peer is unreachable, or its card names another endpoint
      2  usage error
      3  not in a project, a 0.2 runtime, an invalid policy, or a change it cannot make
    """


def _dry_run_option(function: Callable[..., Any]) -> Callable[..., Any]:
    return click.option(
        "--dry-run", "dry_run", is_flag=True, default=False, help="Print the diff only."
    )(function)


# ---------------------------------------------------------------------------
# The project
# ---------------------------------------------------------------------------


def _project() -> _Project:
    project = _load_project()
    problem = runtime_problem(project.root, project.config.agent_directory)
    if problem:
        raise PeerCommandError(problem)
    return project


def runtime_problem(root: Path, agent_directory: str) -> str | None:
    """Why the project's runtime cannot have peers (a 0.2 runtime), or None."""
    if (root / agent_directory / RUNTIME_MODULE).is_file():
        return None
    return (
        f"this project's runtime predates A2A peers (no {agent_directory}/{RUNTIME_MODULE}): run "
        "`graph-agents-cli scaffold upgrade` first. A 0.2 runtime refuses every call once "
        "api-policy.yaml uses protocol: a2a (as KI-007)."
    )


def _module(project: _Project) -> tuple[str, str | None]:
    """The generated module's path (from the root) and its text (None: absent)."""
    path = gen.module_path(project.root, project.config.agent_directory)
    rel = path.relative_to(project.root).as_posix()
    text = read_text(path)
    if text is not None and not gen.is_generated(text):
        raise PeerCommandError(
            f"{rel} exists and was not generated by graph-agents-cli (no marker line): move "
            f"it; graph-agents-cli owns tools/{gen.MODULE_NAME}"
        )
    return rel, text


def _names(project: _Project, extra: dict[str, str] | None = None) -> dict[str, str]:
    """The peer name of each API: the generated module's, plus `extra` (API -> name)."""
    _rel, text = _module(project)
    return {**gen.names_in(text), **(extra or {})}


def _sync_module(
    plan: Plan,
    project: _Project,
    document: dict[str, Any] | None,
    extra: dict[str, str] | None = None,
) -> None:
    rel, before = _module(project)
    peers = gen.peers_of(document, _names(project, extra))
    plan.set_text(rel, before, gen.module_text(peers, project.config.agent_directory))


def _peers(project: _Project) -> dict[str, gen.Peer]:
    return {peer.name: peer for peer in gen.peers_of(project.document, _names(project))}


def _peer(project: _Project, name: str) -> gen.Peer:
    peers = _peers(project)
    if name not in peers:
        known = ", ".join(peers) or "none"
        raise PeerCommandError(f"no peer {name!r} in {POLICY_FILENAME} (peers: {known})")
    return peers[name]


def _client_id(project: _Project) -> str:
    """This agent's client id at the issuer, as the chart sets it (else the project's name)."""
    files = chart_values_files(project.root, project.config)
    if files:
        try:
            env = YamlText(read_text(files[0]) or "").get(("env",))
        except Exception:
            env = None
        value = env.get(TOKEN_EXCHANGE_CLIENT_ID_ENV) if isinstance(env, dict) else None
        if isinstance(value, str) and value and "CHANGE-ME" not in value:
            return value
    return project.config.project_name or "<this agent's client id>"


# ---------------------------------------------------------------------------
# The card (--card on add, --check on show)
# ---------------------------------------------------------------------------


def _read_card(source: str) -> dict[str, Any]:
    """A card from a file, or fetched (no credential) from a URL; ValueError when unreadable."""
    if source.startswith(("http://", "https://")):
        import httpx

        response = httpx.get(
            source,
            headers={"A2A-Version": "1.0"},
            timeout=CARD_TIMEOUT_S,
            follow_redirects=False,
        )
        if response.status_code == 401:
            raise ValueError("401: the card needs a credential (KI-120)")
        response.raise_for_status()
        data = response.json()
    else:
        data = json.loads(Path(source).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("not an agent card (a JSON object)")
    return data


def _card_interface(card: dict[str, Any]) -> dict[str, Any] | None:
    for interface in card.get("supportedInterfaces") or []:
        if (
            isinstance(interface, dict)
            and str(interface.get("protocolBinding", "")).upper() == "JSONRPC"
            and str(interface.get("protocolVersion", "")).startswith("1.")
        ):
            return interface
    return None


def _card_reads_origin(card: dict[str, Any]) -> bool:
    capabilities = card.get("capabilities") if isinstance(card.get("capabilities"), dict) else {}
    return any(
        isinstance(e, dict) and e.get("uri") == ORIGIN_EXTENSION
        for e in capabilities.get("extensions") or []
    )


ORIGIN_EXTENSION = "https://ss7172.github.io/graph-agents-cli/a2a/ext/origin/v1"


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


def _parse_calls(value: str) -> list[str]:
    calls = [part.strip() for part in value.split(",") if part.strip()]
    unknown = [c for c in calls if c not in CALL_CHOICES]
    if unknown or "ask" not in calls:
        raise click.UsageError(
            f"--calls takes a comma list of {', '.join(CALL_CHOICES)}, with ask (got {value!r})"
        )
    return calls


def _entry(
    *,
    description: str | None,
    path: str,
    url_env: str,
    auth: str,
    token_env: str | None,
    audience: str | None,
    scope: str | None,
    resource: str | None,
    allow_actorless: bool,
    forward_audience: bool,
    calls: list[str],
    approvals: str,
    approval_timeout_s: int,
    max_calls_per_run: int,
    read_timeout_ms: int,
    max_response_bytes: int,
) -> dict[str, Any]:
    """The api-policy entry of a peer, its keys in the schema's order."""
    api: dict[str, Any] = {}
    if description:
        api[DESCRIPTION_KEY] = description
    api[PROTOCOL_KEY] = PROTOCOL_A2A
    api[A2A_KEY] = {"path": path}
    api["base_url_env"] = url_env
    api["auth"] = auth
    if auth == "bearer":
        api["token_env"] = token_env
    if auth == "forward" and forward_audience and audience:
        api["forward_audience"] = audience
    if auth == "exchange":
        exchange: dict[str, Any] = {"audience": audience}
        if scope:
            exchange["scope"] = scope
        if resource:
            exchange["resource"] = resource
        if allow_actorless:
            exchange[ALLOW_ACTORLESS_KEY] = True
        api[EXCHANGE_KEY] = exchange
    api["allowed_methods"] = ["GET", "POST"]
    operations: list[dict[str, Any]] = [
        {
            "operationId": gen.CARD_OPERATION,
            "methods": ["GET"],
            "path": f"{path}{gen.CARD_SUFFIX}",
        },
        {RPC_METHOD_KEY: "SendMessage", "methods": ["POST"], "path": path},
    ]
    if "status" in calls or approvals == RELAY:  # the relay reads the task first
        operations.append({RPC_METHOD_KEY: "GetTask", "methods": ["POST"], "path": path})
    if "cancel" in calls:
        operations.append({RPC_METHOD_KEY: "CancelTask", "methods": ["POST"], "path": path})
    if approvals == RELAY:
        operations.append(
            {"operationId": gen.LEDGER_OPERATION, "methods": ["GET"], "path": gen.LEDGER_PATH}
        )
    api[ch.ALLOWED] = operations
    if approvals == DENY:
        api[ch.DENIED] = [{A2A_OPERATION_KEY: A2A_APPROVE}]
    api["timeouts_ms"] = {"connect": DEFAULT_CONNECT_TIMEOUT_MS, "read": read_timeout_ms}
    api["limits"] = {
        "max_calls_per_run": max_calls_per_run,
        "max_response_bytes": max_response_bytes,
    }
    if approvals == RELAY:
        api["approval"] = [
            {
                "required_for": {"operations": [{A2A_OPERATION_KEY: A2A_APPROVE}]},
                "approvers": [REQUESTER_APPROVER],
                "timeout_s": approval_timeout_s,
            }
        ]
    return api


def _same_peer(existing: dict[str, Any], path: str) -> bool:
    return (
        api_protocol(existing) == PROTOCOL_A2A and (existing.get(A2A_KEY) or {}).get("path") == path
    )


def left_for_you(
    project: _Project,
    name: str,
    api_name: str,
    api: dict[str, Any],
    *,
    first_exchange: bool,
    cluster_envs: list[str],
) -> list[str]:
    """What `peer add` cannot do: settings here, at the issuer, and on the peer (printed)."""
    config = project.config
    client = _client_id(project)
    url_env = api["base_url_env"]
    items = []
    where = "in .env (local runs)"
    if config.deployment_target == "kubernetes":
        missing = [e for e in _environments(project) if e not in cluster_envs]
        if missing:
            where += (
                f" and in values-{{{','.join(missing)}}}.yaml (values.yaml holds a placeholder)"
            )
    items.append(f"set {url_env} (the base URL of {name}) {where}")
    auth = api["auth"]
    audience = (api.get(EXCHANGE_KEY) or {}).get("audience") or api.get("forward_audience")
    if auth == "exchange":
        if first_exchange:
            items.append(
                "set TOKEN_EXCHANGE_URL (the issuer's token endpoint) and TOKEN_EXCHANGE_CLIENT_ID "
                "(this agent's client there) in .env and in the chart's values-<env>.yaml; put "
                "TOKEN_EXCHANGE_CLIENT_SECRET in .env (and .env.<env>, then `graph-agents-cli "
                "secrets apply --env <env>`)"
            )
        items.append(
            f"at the issuer: let this agent's client ({client}) exchange users' tokens for "
            f"audience {audience}, naming it in the exchanged token's act claim"
        )
        items.append(
            f"on {name}: AUTH_JWT_AUDIENCE includes {audience}, and AUTH_ALLOWED_ACTORS "
            f"includes {client}"
        )
        actorless = (api.get(EXCHANGE_KEY) or {}).get(ALLOW_ACTORLESS_KEY) is True
        if actorless:
            items.append(
                f"on {name} (required: this agent sends tokens that name no actor, "
                f"exchange.allow_actorless): AUTH_JWT_DIRECT_CLIENTS=<the clients people sign in "
                f"with>, and client:{client} in AUTH_ALLOWED_ACTORS; otherwise {name} reads this "
                "agent's calls as the person's own"
            )
        else:
            items.append(
                "if the issuer's exchanged tokens name no actor (no act claim; Keycloak adds "
                "none: decode one to see), this agent refuses them: then add --allow-actorless "
                f"(exchange.allow_actorless) and, on {name}, set AUTH_JWT_DIRECT_CLIENTS=<the "
                f"clients people sign in with> and list client:{client} in AUTH_ALLOWED_ACTORS"
            )
        relayer = f"client:{client}" if actorless else client
    elif auth == "bearer":
        items.append(
            f"put {api['token_env']} (the key {name} accepts, its API_KEY) in .env"
            + (
                " and .env.<env>, then `graph-agents-cli secrets apply --env <env>`"
                if config.deployment_target == "kubernetes"
                else ""
            )
        )
        items.append(SHARED_BEARER_NOTE.format(api=name))
        relayer = client
    else:
        items.append(
            f"make the auth policy give callers attributes['credentials']['{api_name}'] (the "
            f"credential forwarded to {name}), and have {name}'s policy set the actor for it "
            "(a custom policy that forwards users' credentials must, or the callee treats this "
            "agent as the person)"
        )
        relayer = client
    if api.get("approval"):
        items.append(
            f"on {name}, to let this agent relay the person's decisions: `graph-agents-cli api "
            f"approval <its gated API> --decide-with relayed --relayers {relayer}` for each gate "
            f"a relay should reach (a reviewed loosening there; without it the person approves "
            f"at {name} directly)"
        )
    items.append(
        f"set PRINCIPAL_HASH_SALT (a secret): the conversation ids sent to {name} are keyed with it"
    )
    items.append(
        f"then `graph-agents-cli lint`, `graph-agents-cli peer show {name} --check`, and an eval "
        "case at the agent people talk to"
    )
    return items


def _environments(project: _Project) -> list[str]:
    return [
        path.stem.removeprefix("values-")
        for path in chart_values_files(project.root, project.config)[1:]
    ]


def _finish(
    project: _Project,
    plan: Plan,
    *,
    dry_run: bool,
    notes: list[str],
    todos: list[str],
) -> None:
    console = Console()
    if not plan.effective:
        console.print(NOTHING)
        return
    print_diff(plan.diff())
    click.echo()
    for note in notes:
        console.print(f"Note: {escape(note)}", style="yellow", highlight=False)
    if dry_run:
        console.print("Dry run: nothing was written.", style="yellow")
    else:
        plan.write()
        written = ", ".join(change.path for change in plan.effective)
        console.print(f"Wrote {escape(written)}.", style="green")
    left = [*plan.left_for_you, *todos]
    if left:
        console.print("Left for you:", style="bold")
        for item in left:
            console.print(f"  - {escape(item)}", highlight=False)


@peer_group.command("add")
@click.argument("name")
@click.option("--api-name", "api_name", default=None, help="The API's name (default <NAME>_agent).")
@click.option(
    "--url-env", "url_env", default=None, help="Base URL variable (default <NAME>_AGENT_URL)."
)
@click.option(
    "--path",
    "path",
    default=None,
    help="The peer's A2A endpoint (default /a2a/<NAME>: its agent directory or A2A_NAME).",
)
@click.option(
    "--auth",
    "auth",
    type=click.Choice(["exchange", "forward", "bearer"]),
    default=None,
    help="Default by the auth policy: jwt exchange, custom forward, shared-bearer bearer.",
)
@click.option(
    "--audience",
    "audience",
    default=None,
    help="exchange: the audience the issuer mints for (default NAME); forward (jwt): forward_audience.",
)
@click.option("--scope", "scope", default=None, help="exchange: the scopes to ask for.")
@click.option("--resource", "resource", default=None, help="exchange: the resource indicator.")
@click.option(
    "--allow-actorless",
    "allow_actorless",
    is_flag=True,
    default=False,
    help=(
        "exchange: accept exchanged tokens that name no actor; the peer must then set "
        "AUTH_JWT_DIRECT_CLIENTS and list client:<this agent's client id>."
    ),
)
@click.option(
    "--token-env",
    "token_env",
    default=None,
    help="bearer: the key's variable (default <NAME>_AGENT_KEY).",
)
@click.option(
    "--description",
    "description",
    default=None,
    help=f"What the peer does, for the model's roster (at most {DESCRIPTION_MAX_CHARS} characters).",
)
@click.option(
    "--card",
    "card",
    default=None,
    metavar="URL|FILE",
    help="Read the description, the path and origin support from the peer's agent card.",
)
@click.option(
    "--calls",
    "calls",
    default=",".join(DEFAULT_CALLS),
    show_default=True,
    help="What the agent may send: ask (SendMessage), status (GetTask), cancel (CancelTask).",
)
@click.option(
    "--approvals",
    "approvals",
    type=click.Choice([RELAY, DENY]),
    default=RELAY,
    show_default=True,
    help=(
        "relay: messages that approve the peer's pending approvals wait for the person here "
        "(gated: requester); deny: this agent never sends them."
    ),
)
@click.option(
    "--approval-timeout-s",
    type=click.IntRange(MIN_APPROVAL_TIMEOUT_S, MAX_APPROVAL_TIMEOUT_S),
    default=DEFAULT_APPROVAL_TIMEOUT_S,
    show_default=True,
)
@click.option(
    "--max-calls-per-run", type=click.IntRange(min=1), default=DEFAULT_MAX_CALLS, show_default=True
)
@click.option(
    "--read-timeout-ms",
    type=click.IntRange(min=1),
    default=DEFAULT_READ_TIMEOUT_MS,
    show_default=True,
    help="A peer runs a model: its answers take time.",
)
@click.option(
    "--max-response-bytes",
    type=click.IntRange(1, MAX_RESPONSE_BYTES_LIMIT),
    default=DEFAULT_MAX_RESPONSE_BYTES,
    show_default=True,
)
@click.option(
    "--cluster-url",
    "cluster_url",
    default=None,
    metavar="TEMPLATE",
    help=(
        "The peer's URL per environment, with {env}, for values-<env>.yaml "
        "(e.g. http://orders-agent.orders-agent-{env}.svc.cluster.local)."
    ),
)
@_dry_run_option
def cmd_add(
    name: str,
    api_name: str | None,
    url_env: str | None,
    path: str | None,
    auth: str | None,
    audience: str | None,
    scope: str | None,
    resource: str | None,
    allow_actorless: bool,
    token_env: str | None,
    description: str | None,
    card: str | None,
    calls: str,
    approvals: str,
    approval_timeout_s: int,
    max_calls_per_run: int,
    read_timeout_ms: int,
    max_response_bytes: int,
    cluster_url: str | None,
    dry_run: bool,
) -> None:
    """Add another agent this agent asks (a protocol: a2a API), and regenerate tools/a2a_peers.py.

    Defaults fit a peer made with graph-agents-cli: its endpoint /a2a/NAME, the credential
    by this project's auth policy (jwt: a token exchanged for the user's, audience NAME),
    SendMessage and GetTask, the person's approval relayed through a gate here, 12 calls per
    run, a 120 s read timeout and a 1 MiB answer cap. It prints what is left to set: here,
    at the issuer, and on the peer (AUTH_ALLOWED_ACTORS, and the `api approval
    --decide-with relayed --relayers` line that lets this agent relay there).
    """
    if not gen.PEER_NAME_RE.fullmatch(name):
        raise click.UsageError(
            f"peer name {name!r}: lowercase letters, digits and underscores, starting with a "
            "letter, at most 26 characters"
        )
    parsed_calls = _parse_calls(calls)
    project = _project()
    plan = Plan(project.root)
    added = plan_add(
        project,
        plan,
        PeerOptions(
            name=name,
            api_name=api_name,
            url_env=url_env,
            path=path,
            auth=auth,
            audience=audience,
            scope=scope,
            resource=resource,
            allow_actorless=allow_actorless,
            token_env=token_env,
            description=description,
            card=card,
            calls=tuple(parsed_calls),
            approvals=approvals,
            approval_timeout_s=approval_timeout_s,
            max_calls_per_run=max_calls_per_run,
            read_timeout_ms=read_timeout_ms,
            max_response_bytes=max_response_bytes,
            cluster_url=cluster_url,
        ),
    )
    _finish(project, plan, dry_run=dry_run, notes=added.notes, todos=added.todos)


@dataclass
class PeerOptions:
    """What `peer add` is asked for (its options), for `plan_add`."""

    name: str
    api_name: str | None = None
    url_env: str | None = None
    path: str | None = None
    auth: str | None = None
    audience: str | None = None
    scope: str | None = None
    resource: str | None = None
    allow_actorless: bool = False
    token_env: str | None = None
    description: str | None = None
    card: str | None = None
    calls: tuple[str, ...] = DEFAULT_CALLS
    approvals: str = RELAY
    approval_timeout_s: int = DEFAULT_APPROVAL_TIMEOUT_S
    max_calls_per_run: int = DEFAULT_MAX_CALLS
    read_timeout_ms: int = DEFAULT_READ_TIMEOUT_MS
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    cluster_url: str | None = None
    # The peer's URL in .env.example (a local run).
    local_url: str = PEER_LOCAL_URL


@dataclass
class PeerAdded:
    """What `plan_add` planned: the project as it will be, and what to tell the user."""

    project: _Project  # its policy text and document with the peer in
    api_name: str
    api: dict[str, Any]
    notes: list[str]
    todos: list[str]
    names: dict[str, str]  # API -> peer name for the generated module, this peer's included


# An existing peer's settings `plan_add(keep_tuning=True)` keeps when it rewrites the entry
# (`system apply`): what the system file does not say. The rest follows the file.
TUNED_KEYS = ("timeouts_ms", "limits", "forward_header")


def _retuned(api: dict[str, Any], existing: dict[str, Any]) -> dict[str, Any]:
    """``api`` with the tuning of the ``existing`` entry it replaces carried over."""
    new = copy.deepcopy(api)
    for key in TUNED_KEYS:
        if key in existing:
            new[key] = copy.deepcopy(existing[key])
    old_exchange = existing.get(EXCHANGE_KEY) or {}
    if EXCHANGE_KEY in new and "resource" in old_exchange and "resource" not in new[EXCHANGE_KEY]:
        new[EXCHANGE_KEY]["resource"] = old_exchange["resource"]
    old_rules, new_rules = existing.get("approval"), new.get("approval")
    if isinstance(old_rules, list) and isinstance(new_rules, list):
        for old_rule, new_rule in zip(old_rules, new_rules, strict=False):
            if isinstance(old_rule, dict) and "timeout_s" in old_rule:
                new_rule["timeout_s"] = old_rule["timeout_s"]
    if DESCRIPTION_KEY not in new and existing.get(DESCRIPTION_KEY):
        new[DESCRIPTION_KEY] = existing[DESCRIPTION_KEY]
    # The schema's key order, as every command writes an entry.
    rank = {key: index for index, key in enumerate(_API_KEY_ORDER)}
    return {key: new[key] for key in sorted(new, key=lambda k: rank.get(k, len(rank)))}


def plan_add(
    project: _Project,
    plan: Plan,
    options: PeerOptions,
    *,
    names: dict[str, str] | None = None,
    keep_tuning: bool = False,
) -> PeerAdded:
    """Plan a peer's entry and every file that follows it into ``plan``.

    ``project`` holds the policy as earlier plans left it, so several peers can be
    planned into one ``plan`` (``names``: the peer names planned so far, API -> name).
    An entry of the same name that differs is refused, unless ``keep_tuning``
    (``system apply``): a peer's entry is then rewritten to the options, keeping the
    settings the options do not decide (``TUNED_KEYS``, the approval timeout, the
    exchange resource and a description the options do not give).
    """
    config = project.config
    name = options.name
    if name in (config.agent_directory, config.project_name):
        raise click.UsageError(f"an agent cannot be its own peer ({name})")
    auth = options.auth or AUTH_BY_POLICY.get(config.auth_policy, "bearer")
    if auth != "exchange" and (options.scope or options.resource or options.allow_actorless):
        raise click.UsageError("--scope, --resource and --allow-actorless go with --auth exchange")
    if auth != "bearer" and options.token_env:
        raise click.UsageError("--token-env goes with --auth bearer")
    notes: list[str] = []
    path, description = options.path, options.description
    if options.card is not None:
        card = options.card
        try:
            data = _read_card(card)
        except Exception as exc:
            notes.append(f"the card {card} could not be read ({exc}); continuing without it")
            data = None
        if data is not None:
            interface = _card_interface(data)
            if path is None and interface is not None:
                import urllib.parse

                path = urllib.parse.urlsplit(str(interface.get("url"))).path.rstrip("/") or None
            if description is None and isinstance(data.get("description"), str):
                description = data["description"]
            notes.append(
                f"{name}'s card "
                + (
                    "declares the origin extension: the user's own words go with each request"
                    if _card_reads_origin(data)
                    else "does not declare the origin extension: the user's words stay here"
                )
            )
    api_name = options.api_name or f"{name}{gen.AGENT_SUFFIX}"
    url_env = options.url_env or f"{name.upper()}_AGENT_URL"
    path = path or f"/a2a/{name}"
    token_env = options.token_env or (f"{name.upper()}_AGENT_KEY" if auth == "bearer" else None)
    if description is not None:
        description = " ".join(description.split())
        if len(description) > DESCRIPTION_MAX_CHARS:
            description = description[: DESCRIPTION_MAX_CHARS - 3].rstrip() + "..."
            notes.append(f"the description was cut to {DESCRIPTION_MAX_CHARS} characters")
    api = _entry(
        description=description,
        path=path,
        url_env=url_env,
        auth=auth,
        token_env=token_env,
        audience=options.audience or name,
        scope=options.scope,
        resource=options.resource,
        allow_actorless=options.allow_actorless,
        forward_audience=auth == "forward"
        and (options.audience is not None or config.auth_policy == "jwt"),
        calls=list(options.calls),
        approvals=options.approvals,
        approval_timeout_s=options.approval_timeout_s,
        max_calls_per_run=options.max_calls_per_run,
        read_timeout_ms=options.read_timeout_ms,
        max_response_bytes=options.max_response_bytes,
    )
    existing = (project.document or {}).get("apis", {}).get(api_name)
    if existing is not None and existing != api:
        if keep_tuning and api_protocol(existing) == PROTOCOL_A2A:
            api = _retuned(api, existing)
        elif _same_peer(existing, path):
            raise PeerCommandError(
                f"peer {name} exists with other settings; change it with `graph-agents-cli api "
                f"...` ({api_name}), or `peer remove {name}` then `peer add {name}`"
            )
        else:
            raise PeerCommandError(f"API {api_name} exists; pick --api-name")
    if DESCRIPTION_KEY not in api:
        notes.append(
            f"no --description: the model's roster says {name} has none (lint warns); give one "
            "with --description or --card"
        )
    document = ch.with_api(project.document, api_name, api)
    _validate(project, document)
    errors, matrix_notes = auth_policy_findings({"apis": {api_name: api}}, config.auth_policy)
    if errors:
        lines = "\n".join(f"  - {error}" for error in errors)
        raise PeerCommandError(f"{name} cannot work in this project; nothing was written:\n{lines}")
    extra = {**(names or {}), api_name: name}
    named = {p.api: p.name for p in gen.peers_of(document, _names(project, extra))}
    if named.get(api_name) != name:
        clash = next((api for api, n in named.items() if n == name and api != api_name), "?")
        raise PeerCommandError(f"another peer is already called {name} (API {clash})")
    text = project.text
    if existing != api:
        if project.text is None:
            lines = [*_policy_header(config), "apis:", *block_lines({api_name: api}, 2)]
            text = "\n".join(lines) + "\n"
        else:
            editor = project.editor()
            verb = "add" if existing is None else "rewrite"
            _edit(lambda: editor.set(("apis", api_name), api), f"{verb} apis.{api_name}")
            text = editor.text
        if YamlText(text).data != document:
            raise PeerCommandError(f"could not write {POLICY_FILENAME} (internal check)")
        plan.set_text(POLICY_FILENAME, read_text(project.root / POLICY_FILENAME), text)
    # The same peer again: nothing in the policy changes, and the files that follow it are
    # brought back in step (each edit below finds its work done when it is).
    sync_manifest(plan, config, document=document, previous=project.document)
    env_example_add(plan, api_name, api, peer=name, base_url=options.local_url)
    values_add(plan, config, document, api_name)
    values_exchange_add(plan, config, document, api_name)
    cluster_envs = (
        values_env_set(plan, config, url_env, options.cluster_url) if options.cluster_url else []
    )
    _sync_module(plan, project, document, extra)
    previous = copy.deepcopy(project.document)
    if previous is not None and existing is not None:
        del previous["apis"][api_name]
    first_exchange = auth == "exchange" and not (
        previous is not None and uses_exchange(summarize(previous))
    )
    todos = left_for_you(
        project, name, api_name, api, first_exchange=first_exchange, cluster_envs=cluster_envs
    )
    notes.extend(matrix_notes)
    if options.approvals == RELAY:
        notes.append(
            f"messages that approve {name}'s pending approvals wait for the person here (gated: "
            "requester): this agent relays their decision, never its own"
        )
    return PeerAdded(
        project=_Project(project.root, config, text, document),
        api_name=api_name,
        api=api,
        notes=notes,
        todos=todos,
        names=extra,
    )


# ---------------------------------------------------------------------------
# remove, sync
# ---------------------------------------------------------------------------


@peer_group.command("remove")
@click.argument("name")
@_dry_run_option
def cmd_remove(name: str, dry_run: bool) -> None:
    """Remove a peer (its API and variables), and regenerate or delete tools/a2a_peers.py."""
    project = _project()
    plan = Plan(project.root)
    removed = plan_remove(project, plan, name)
    _finish(project, plan, dry_run=dry_run, notes=removed.notes, todos=[])


@dataclass
class PeerRemoved:
    """What `plan_remove` planned: the project as it will be, and what to tell the user."""

    project: _Project
    notes: list[str]


def plan_remove(
    project: _Project, plan: Plan, name: str, *, names: dict[str, str] | None = None
) -> PeerRemoved:
    """Plan removing peer ``name`` and the files that follow it into ``plan`` (``project``
    as earlier plans left it; ``names``: the peer names planned so far, API -> name)."""
    peers = {p.name: p for p in gen.peers_of(project.document, _names(project, names))}
    if name not in peers:
        known = ", ".join(peers) or "none"
        raise PeerCommandError(f"no peer {name!r} in {POLICY_FILENAME} (peers: {known})")
    peer = peers[name]
    assert project.document is not None
    api = project.document["apis"][peer.api]
    document = copy.deepcopy(project.document)
    del document["apis"][peer.api]
    after: dict[str, Any] | None = document if document["apis"] else None
    stored = read_text(project.root / POLICY_FILENAME)
    text: str | None
    if after is None:
        text = None
    else:
        editor = project.editor()
        _edit(lambda: editor.delete(("apis", peer.api)), f"delete apis.{peer.api}")
        text = editor.text
    plan.set_text(POLICY_FILENAME, stored, text)
    sync_manifest(plan, project.config, document=after, previous=project.document)
    others = list(document["apis"].values())
    keep = {str(o["base_url_env"]) for o in others} | {
        str(o["token_env"]) for o in others if o.get("token_env")
    }
    env_example_remove(plan, peer.api, api, keep, document["apis"])
    if api["base_url_env"] not in keep:
        values_remove(
            plan, project.config, api["base_url_env"], {str(o["base_url_env"]) for o in others}
        )
    if api["auth"] == EXCHANGE_KEY and not (after and uses_exchange(summarize(after))):
        values_exchange_remove(plan, project.config)
    _sync_module(plan, project, after, names)
    notes = []
    if peer.approvals == RELAY:
        notes.append(
            f"approvals this agent is relaying to {name} and still has pending will fail when "
            "decided (the API they call is gone)"
        )
    if after is None:
        notes.append(f"{name} was the only API: {POLICY_FILENAME} goes")
    return PeerRemoved(_Project(project.root, project.config, text, after), notes)


@peer_group.command("sync")
@_dry_run_option
def cmd_sync(dry_run: bool) -> None:
    """Regenerate tools/a2a_peers.py from api-policy.yaml (after `scaffold upgrade`, or `api` edits)."""
    project = _project()
    plan = Plan(project.root)
    _sync_module(plan, project, project.document)
    _finish(project, plan, dry_run=dry_run, notes=[], todos=[])


# ---------------------------------------------------------------------------
# list, show
# ---------------------------------------------------------------------------


def _url_of(project: _Project, variable: str) -> str | None:
    """The peer's URL from the environment or .env (a URL only; nothing else is read)."""
    import os

    value = os.environ.get(variable)
    if value:
        return value
    env_file = project.root / ".env"
    if env_file.is_file():
        from dotenv import dotenv_values

        try:
            found = dotenv_values(env_file).get(variable)
        except Exception:
            return None
        return found or None
    return None


def _row(project: _Project, peer: gen.Peer) -> dict[str, Any]:
    assert project.document is not None
    api = project.document["apis"][peer.api]
    exchange = api.get(EXCHANGE_KEY) or {}
    return {
        "name": peer.name,
        "api": peer.api,
        "url_env": api["base_url_env"],
        "url": _url_of(project, api["base_url_env"]),
        "path": peer.path,
        "auth": api["auth"],
        "audience": exchange.get("audience") or api.get("forward_audience"),
        "approvals": peer.approvals,
        "calls": [
            " ".join(
                str(part)
                for part in (
                    c.get(RPC_METHOD_KEY) or c.get("operation_id"),
                    c.get(A2A_OPERATION_KEY),
                )
                if part
            )
            for c in peer.calls
        ],
        "limits": api.get("limits") or {},
        "description": peer.description,
    }


@peer_group.command("list")
@click.option("--json", "as_json", is_flag=True, default=False, help="Print JSON.")
def cmd_list(as_json: bool) -> None:
    """List the peers: name, API, URL, auth, audience, approvals and limits."""
    project = _project()
    rows = [_row(project, peer) for peer in _peers(project).values()]
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    console = Console()
    if not rows:
        console.print("No peers: add one with `graph-agents-cli peer add NAME`.")
        return
    table = Table(title="A2A peers")
    for header in ("Peer", "API", "URL", "Auth", "Audience", "Approvals", "Limits"):
        table.add_column(header, overflow="fold")
    for row in rows:
        limits = ", ".join(f"{k} {v}" for k, v in row["limits"].items())
        table.add_row(
            escape(row["name"]),
            escape(row["api"]),
            escape(f"{row['url_env']}={row['url'] or '(unset)'}"),
            row["auth"],
            escape(str(row["audience"] or "-")),
            row["approvals"],
            escape(limits or "-"),
        )
    print_table(console, table)
    if project.config.auth_policy == "shared-bearer":
        console.print(
            "Note: under shared-bearer, any holder of API_KEY (another agent included) can "
            "decide requester gates at a peer.",
            style="yellow",
        )


def check_card(url: str, path: str, *, transport: Any = None) -> tuple[bool, list[str]]:
    """GET a peer's card (no credential) and check it; `(ok, lines to print)`."""
    import httpx

    card_url = f"{url.rstrip('/')}{path}{gen.CARD_SUFFIX}"
    try:
        with httpx.Client(transport=transport, timeout=CARD_TIMEOUT_S) as client:
            response = client.get(card_url, headers={"A2A-Version": "1.0"})
    except httpx.HTTPError as exc:
        return False, [f"unreachable ({type(exc).__name__}): {card_url}"]
    if response.status_code == 401:
        return True, ["reachable (401: the card needs a credential; KI-120)"]
    if response.status_code != 200:
        return False, [f"unreachable (HTTP {response.status_code}): {card_url}"]
    try:
        card = response.json()
    except ValueError:
        return False, ["reachable, but its card is not JSON"]
    lines = ["reachable"]
    interface = _card_interface(card) if isinstance(card, dict) else None
    if interface is None:
        return False, [*lines, "its card offers no A2A 1.x JSON-RPC interface"]
    lines.append(f"protocol {interface.get('protocolVersion')}")
    expected = f"{url.rstrip('/')}{path}".rstrip("/")
    if str(interface.get("url", "")).rstrip("/") != expected:
        return False, [
            *lines,
            f"foreign endpoint: the card names {interface.get('url')}, not {expected} (set the "
            "peer's APP_URL, appUrl in its chart values)",
        ]
    name = path.rstrip("/").rsplit("/", 1)[-1]
    if card.get("name") != name:
        return False, [*lines, f"the card is agent {card.get('name')!r}, not {name!r}"]
    lines.append(
        "declares the origin extension"
        if _card_reads_origin(card)
        else "does not declare the origin extension (the user's words stay here)"
    )
    return True, lines


@peer_group.command("show")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, default=False, help="Print JSON.")
@click.option(
    "--check",
    "check",
    is_flag=True,
    default=False,
    help="Read the peer's card (no credential) and check its endpoint; exit 1 when it fails.",
)
def cmd_show(name: str, as_json: bool, check: bool) -> None:
    """Show a peer's entry and what is left to set; --check reads its card."""
    project = _project()
    peer = _peer(project, name)
    assert project.document is not None
    api = project.document["apis"][peer.api]
    todos = left_for_you(project, name, peer.api, api, first_exchange=False, cluster_envs=[])
    result: dict[str, Any] = {"peer": _row(project, peer), "entry": api, "left_for_you": todos}
    ok = True
    if check:
        url = _url_of(project, api["base_url_env"])
        if not url:
            ok, lines = False, [f"{api['base_url_env']} is not set (in the environment or .env)"]
        else:
            ok, lines = check_card(url, peer.path)
        result["check"] = {"ok": ok, "lines": lines}
    if as_json:
        click.echo(json.dumps(result, indent=2))
    else:
        console = Console()
        console.print(f"{escape(name)} ({escape(peer.api)}):", style="bold")
        click.echo(yaml_text({peer.api: api}))
        console.print("Left for you:", style="bold")
        for item in todos:
            console.print(f"  - {escape(item)}", highlight=False)
        if check:
            console.print("Check:", style="bold")
            for line in result["check"]["lines"]:
                console.print(f"  {escape(line)}", style="green" if ok else "red", highlight=False)
    if not ok:
        raise SystemExit(1)


def yaml_text(value: dict[str, Any]) -> str:
    return "\n".join(block_lines(value, 2))
