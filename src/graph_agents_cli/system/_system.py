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

"""A system file resolved against its projects: agents, edges, environments and URLs.

``resolve`` reads every project the file names (its manifest, ``api-policy.yaml``,
the generated peers module and the chart's values) and turns each ``calls`` entry
into a ``Link`` with what ``peer add`` would be given for it. What makes the file
unusable is a ``SystemFileError`` (exit 3): a project that does not exist or that
two agents name, an edge to an unknown agent or to the agent itself, the same agent called twice by
one agent, two agents with one client id, an ``auth: exchange`` edge without
``identity``, an environment a project's manifest does not know. Everything else
is a finding of ``system check``.

The URL an agent is called at in an environment:

- ``port_base`` (local processes): ``http://127.0.0.1:<port_base + i>``, i its
  position in ``agents``;
- ``url``: the template with ``{agent}``, ``{project}`` and ``{env}`` filled;
- otherwise in the cluster: ``http://<chart fullname>.<namespace>.svc.cluster.local``
  (``:<service.port>`` unless 80), the namespace from the called project's
  manifest (``environments.<env>.namespace``, default ``<project>-<env>``) and the
  fullname the release name, which ``deploy`` sets to the project's name.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

import click

from graph_agents_cli._api_policy import (
    DECIDE_DIRECT,
    DECIDE_RELAYED,
    REQUESTER_APPROVER,
    approval_rules,
)
from graph_agents_cli._defaults import normalize_auth_policy
from graph_agents_cli._project import MANIFEST_FILENAME, ProjectConfig
from graph_agents_cli.api._files import chart_values_files, read_text
from graph_agents_cli.api.cmd_api import _Project, load_project_at
from graph_agents_cli.deploy._values import deep_merge, load_yaml
from graph_agents_cli.peer import _generate as gen
from graph_agents_cli.peer.cmd_peer import AUTH_BY_POLICY, PeerOptions, runtime_problem
from graph_agents_cli.system._model import (
    DENY,
    RELAY,
    Edge,
    SystemFile,
    SystemFileError,
    load,
)

LOCAL = "local"
URL = "url"
CLUSTER = "cluster"
DEFAULT_SERVICE_PORT = 80
DEFAULT_TARGET_PORT = 8000
# `AUTH_MAX_DELEGATION_DEPTH` when a project does not set it (the template's default).
DEFAULT_MAX_DELEGATION_DEPTH = 3
# `DB_POOL_MAX_SIZE` when unset (the template's default), and LangGraph Server's own pool
# (`LANGGRAPH_POSTGRES_POOL_MAX_SIZE`, 150 in langgraph-api 0.14), which a langgraph-server
# replica opens beside the app's.
DEFAULT_DB_POOL_MAX_SIZE = 10
DEFAULT_LANGGRAPH_POOL_MAX_SIZE = 150
CLIENT_ACTOR_PREFIX = "client:"


@dataclass(frozen=True)
class Env:
    """An environment of the file: where the agents run, so how they are called."""

    name: str
    kind: str  # LOCAL, URL or CLUSTER
    port_base: int | None = None
    url: str | None = None

    @property
    def charted(self) -> bool:
        """Whether the agents run from their charts here (values-<env>.yaml)."""
        return self.kind != LOCAL


@dataclass
class Node:
    """One agent of the system and what its project says."""

    name: str
    index: int
    root: Path
    project: _Project
    client_id: str
    # The actor id the agents it calls see (act.sub): the file's actor_id, else client_id.
    actor_id: str = ""
    _values: dict[str | None, dict[str, Any]] = field(default_factory=dict, repr=False)

    @property
    def config(self) -> ProjectConfig:
        return self.project.config

    @property
    def document(self) -> dict[str, Any] | None:
        return self.project.document

    @property
    def release(self) -> str:
        """The Helm release (and chart fullname): the project's name, as ``deploy`` sets it."""
        return self.config.project_name[:63].rstrip("-")

    @property
    def chart_name(self) -> str:
        """The chart's ``agent.name``: ``nameOverride``, else the chart's name (the project's)."""
        override = self.values(None).get("nameOverride")
        name = str(override) if override else self.config.project_name
        return name[:63].rstrip("-")

    @property
    def pod_labels(self) -> dict[str, str]:
        """The agent pods' selector labels (the chart's ``agent.selectorLabels``): the name and
        the release. The bundled database's pods share the release label, not the name."""
        return {
            "app.kubernetes.io/name": self.chart_name,
            "app.kubernetes.io/instance": self.release,
        }

    @property
    def runtime_problem(self) -> str | None:
        return runtime_problem(self.root, self.config.agent_directory)

    @cached_property
    def chart_files(self) -> list[Path]:
        return chart_values_files(self.root, self.config)

    @property
    def has_chart(self) -> bool:
        return bool(self.chart_files)

    def values_file(self, env: str) -> Path | None:
        """``values-<env>.yaml`` when the chart has it."""
        for path in self.chart_files[1:]:
            if path.stem == f"values-{env}":
                return path
        return None

    def values(self, env: str | None = None) -> dict[str, Any]:
        """The chart's values merged as ``deploy --env`` merges them (``values.yaml`` alone
        for None); empty without a chart. Unreadable values read as empty (``deploy``
        reports them)."""
        if env not in self._values:
            merged: dict[str, Any] = {}
            if self.chart_files:
                try:
                    merged = load_yaml(self.chart_files[0])
                    extra = self.values_file(env) if env else None
                    if extra is not None:
                        merged = deep_merge(merged, load_yaml(extra))
                except click.ClickException:
                    merged = {}
            self._values[env] = merged
        return copy.deepcopy(self._values[env])

    def chart_env(self, env: str | None = None) -> dict[str, Any]:
        found = self.values(env).get("env")
        return dict(found) if isinstance(found, dict) else {}

    def setting(self, env: str | None, key: str) -> str:
        """A chart ``env`` setting as a string ('' when unset or null)."""
        value = self.chart_env(env).get(key)
        return "" if value is None else str(value).strip()

    def auth_policy(self, env: str | None = None) -> str:
        """``env.AUTH_POLICY`` of the chart values, else the manifest's policy."""
        raw = self.setting(env, "AUTH_POLICY")
        return normalize_auth_policy(raw or self.config.auth_policy, warn=False)

    def a2a_name(self, env: str | None = None) -> str:
        return self.setting(env, "A2A_NAME") or self.config.agent_directory

    def mount(self, env: str | None = None) -> str:
        """Where the agent serves A2A: ``/a2a/<A2A_NAME>``."""
        return f"/a2a/{self.a2a_name(env)}"

    def namespace(self, env: str) -> str:
        return self.config.environment(env)[1]

    def context(self, env: str) -> str | None:
        return self.config.environment(env)[0] or None

    def _port(self, env: str, key: str, default: int) -> int | str:
        service = self.values(env).get("service")
        value = service.get(key) if isinstance(service, dict) else None
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip():
            return int(value) if value.strip().isdigit() else value.strip()
        return default

    def service_port(self, env: str) -> int | str:
        return self._port(env, "port", DEFAULT_SERVICE_PORT)

    def target_port(self, env: str) -> int | str:
        """The pods' http port (``service.targetPort``): what a NetworkPolicy matches."""
        return self._port(env, "targetPort", DEFAULT_TARGET_PORT)

    def replicas(self, env: str) -> tuple[int, bool]:
        """``(replicas, autoscaled)``: ``hpa.maxReplicas`` while the HPA is on."""
        values = self.values(env)
        hpa = values.get("hpa") if isinstance(values.get("hpa"), dict) else {}
        if _true(hpa.get("enabled")):
            return _int(hpa.get("maxReplicas"), 1), True
        return _int(values.get("replicaCount"), 1), False

    def task_store(self, env: str | None = None) -> str:
        """Where the agent keeps A2A tasks: ``postgres`` (shared by replicas) or ``memory``."""
        if self.config.runtime == "langgraph-server":
            return "postgres"
        if self.has_chart:
            checkpointer = self.setting(env, "CHECKPOINTER") or "postgres"
        else:
            checkpointer = self.config.checkpointer
        return "memory" if checkpointer.lower() == "memory" else "postgres"

    def app_url(self, env: str) -> str:
        """The base URL the agent's card advertises (the chart's ``agent.appUrl``)."""
        values = self.values(env)
        if values.get("appUrl"):
            return str(values["appUrl"])
        if self.setting(env, "APP_URL"):
            return self.setting(env, "APP_URL")
        gateway = values.get("gateway") if isinstance(values.get("gateway"), dict) else {}
        if _true(gateway.get("enabled")) and gateway.get("hostname"):
            return f"https://{gateway['hostname']}"
        ingress = values.get("ingress") if isinstance(values.get("ingress"), dict) else {}
        if _true(ingress.get("enabled")) and ingress.get("hostname"):
            tls = values.get("tls") if isinstance(values.get("tls"), dict) else {}
            manager = tls.get("certManager") if isinstance(tls.get("certManager"), dict) else {}
            secure = bool(tls.get("existingSecret")) or _true(manager.get("enabled"))
            return f"{'https' if secure else 'http'}://{ingress['hostname']}"
        return ""

    def route_enabled(self, env: str) -> bool:
        values = self.values(env)
        return any(
            _true((values.get(key) or {}).get("enabled"))
            for key in ("gateway", "ingress")
            if isinstance(values.get(key), dict)
        )

    def public_paths(self, env: str) -> list[dict[str, Any]]:
        """What the route publishes (``route.publicPaths``; empty with no route)."""
        if not self.route_enabled(env):
            return []
        route = self.values(env).get("route")
        paths = route.get("publicPaths") if isinstance(route, dict) else None
        return [p for p in paths or [] if isinstance(p, dict)]

    def max_delegation_depth(self, env: str | None = None) -> int:
        raw = self.setting(env, "AUTH_MAX_DELEGATION_DEPTH")
        return int(raw) if raw.isdigit() else DEFAULT_MAX_DELEGATION_DEPTH

    def allowed_actors(self, env: str | None = None) -> list[str]:
        return [a.strip() for a in self.setting(env, "AUTH_ALLOWED_ACTORS").split(",") if a.strip()]

    @cached_property
    def module_text(self) -> str | None:
        return read_text(gen.module_path(self.root, self.config.agent_directory))

    @property
    def module_names(self) -> dict[str, str]:
        return gen.names_in(self.module_text)

    @cached_property
    def peers(self) -> dict[str, gen.Peer]:
        """The peers the project declares, by peer name."""
        return {p.name: p for p in gen.peers_of(self.document, self.module_names)}

    def entry(self, api: str) -> dict[str, Any] | None:
        found = ((self.document or {}).get("apis") or {}).get(api)
        return found if isinstance(found, dict) else None

    def requester_gates(self, document: dict[str, Any] | None = None) -> list[Gate]:
        """Every approval rule a requester decides, in the policy (what a relay reaches);
        ``document``: the policy as a plan will leave it (default: as it is)."""
        gates: list[Gate] = []
        policy = self.document if document is None else document
        for api_name, api in ((policy or {}).get("apis") or {}).items():
            rules = approval_rules(api) if isinstance(api, dict) else []
            listed = isinstance(api.get("approval"), list)
            for index, rule in enumerate(rules):
                if REQUESTER_APPROVER not in [str(a) for a in rule.get("approvers") or []]:
                    continue
                gates.append(
                    Gate(
                        api=str(api_name),
                        rule=index if listed else None,
                        decide_with=str(rule.get("decide_with") or DECIDE_DIRECT),
                        relayers=[str(r) for r in rule.get("relayers") or []],
                    )
                )
        return gates


@dataclass(frozen=True)
class Gate:
    """A requester gate of a called agent, as `api approval` names it."""

    api: str
    rule: int | None  # --rule N for a list of rules
    decide_with: str
    relayers: list[str]

    def relays(self, actor: str) -> bool:
        return self.decide_with == DECIDE_RELAYED and actor in self.relayers

    def command(self, actor: str) -> str:
        """The `api approval` line that lets ``actor`` relay the requester's decision here."""
        relayers = [*self.relayers, actor] if actor not in self.relayers else self.relayers
        rule = f" --rule {self.rule}" if self.rule is not None else ""
        return (
            f"graph-agents-cli api approval {self.api}{rule} --decide-with relayed "
            f"--relayers {','.join(relayers)}"
        )


@dataclass
class Link:
    """One edge: ``caller`` calls ``callee``, as the file says."""

    caller: Node
    callee: Node
    edge: Edge

    @property
    def auth(self) -> str:
        return self.edge.auth or AUTH_BY_POLICY.get(self.caller.config.auth_policy, "bearer")

    @property
    def peer(self) -> str:
        return self.callee.name

    @property
    def existing(self) -> gen.Peer | None:
        """The caller's peer of the callee's name, when it has one (its API and variables are
        kept: a peer added with --api-name or --url-env keeps them)."""
        return self.caller.peers.get(self.callee.name)

    @property
    def entry(self) -> dict[str, Any] | None:
        peer = self.existing
        return self.caller.entry(peer.api) if peer else None

    @property
    def api_name(self) -> str:
        peer = self.existing
        return peer.api if peer else f"{self.callee.name}{gen.AGENT_SUFFIX}"

    @property
    def url_env(self) -> str:
        entry = self.entry
        if entry and entry.get("base_url_env"):
            return str(entry["base_url_env"])
        return f"{self.callee.name.upper()}_AGENT_URL"

    @property
    def token_env(self) -> str | None:
        if self.auth != "bearer":
            return None
        entry = self.entry
        if entry and entry.get("auth") == "bearer" and entry.get("token_env"):
            return str(entry["token_env"])
        return f"{self.callee.name.upper()}_AGENT_KEY"

    @property
    def audience(self) -> str:
        """What the callee's AUTH_JWT_AUDIENCE must hold: the callee's name."""
        return self.callee.name

    @property
    def actor(self) -> str:
        """The actor id the callee sees for the caller (what AUTH_ALLOWED_ACTORS lists).

        The act.sub of the caller's exchanged tokens (the file's ``actor_id``, else its
        client id); ``client:<client id>`` for tokens that name no actor.
        """
        if self.edge.allow_actorless:
            return f"{CLIENT_ACTOR_PREFIX}{self.caller.client_id}"
        return self.caller.actor_id or self.caller.client_id

    @property
    def carries_user(self) -> bool:
        """Whether the call carries the user's identity (exchange, forward)."""
        return self.auth in ("exchange", "forward")

    def describe(self) -> str:
        return f"{self.caller.name} -> {self.callee.name}"

    def options(self, *, local_url: str | None = None) -> PeerOptions:
        """What `peer add` is given for this edge."""
        description = self.edge.description or self.callee.setting(None, "A2A_DESCRIPTION")
        options = PeerOptions(
            name=self.peer,
            api_name=self.api_name,
            url_env=self.url_env,
            token_env=self.token_env,
            path=self.callee.mount(),
            auth=self.auth,
            audience=self.audience if self.auth in ("exchange", "forward") else None,
            scope=self.edge.scope,
            allow_actorless=self.edge.allow_actorless,
            description=description or None,
            approvals=self.edge.approvals,
        )
        if self.edge.calls is not None:
            options.calls = tuple(self.edge.calls)
        if local_url:
            options.local_url = local_url
        return options


@dataclass
class System:
    """A system file resolved against its projects."""

    path: Path
    file: SystemFile
    nodes: dict[str, Node]
    links: list[Link]
    envs: dict[str, Env]

    @property
    def root(self) -> Path:
        return self.path.parent

    def calls(self, node: Node) -> list[Link]:
        return [link for link in self.links if link.caller is node]

    def callers(self, node: Node) -> list[Link]:
        return [link for link in self.links if link.callee is node]

    def url(self, node: Node, env: str) -> str:
        """The base URL ``node`` is called at in ``env``."""
        spec = self.envs[env]
        if spec.kind == LOCAL:
            return f"http://127.0.0.1:{(spec.port_base or 0) + node.index}"
        if spec.kind == URL:
            return (
                (spec.url or "")
                .replace("{agent}", node.name)
                .replace("{project}", node.config.project_name)
                .replace("{env}", env)
                .rstrip("/")
            )
        port = node.service_port(env)
        suffix = "" if port == DEFAULT_SERVICE_PORT else f":{port}"
        return f"http://{node.release}.{node.namespace(env)}.svc.cluster.local{suffix}"

    def token_url(self, env: str) -> str | None:
        identity = self.file.identity
        if identity is None or identity.token_url is None:
            return None
        if isinstance(identity.token_url, str):
            return identity.token_url
        return identity.token_url.get(env)

    def select(self, names: list[str] | tuple[str, ...] | None) -> list[str]:
        """The environments to look at: ``names`` (each must be in the file), else all."""
        if not names:
            return list(self.envs)
        unknown = [n for n in names if n not in self.envs]
        if unknown:
            known = ", ".join(self.envs) or "none"
            raise click.UsageError(
                f"environment {', '.join(unknown)} is not in {self.path.name} (environments: "
                f"{known})"
            )
        return list(dict.fromkeys(names))

    def local_url(self, node: Node) -> str | None:
        """The URL of ``node`` in the first local environment (for .env.example)."""
        for env in self.envs.values():
            if env.kind == LOCAL:
                return self.url(node, env.name)
        return None

    def relay_gates(
        self, link: Link, documents: dict[str, dict[str, Any] | None] | None = None
    ) -> list[Gate]:
        """The callee's requester gates that ``link``'s caller cannot relay to (SC07);
        ``documents``: each agent's policy as a plan will leave it."""
        if link.edge.approvals != RELAY:
            return []
        document = (documents or {}).get(link.callee.name)
        gates = link.callee.requester_gates(document)
        return [gate for gate in gates if not gate.relays(link.actor)]


def _true(value: Any) -> bool:
    return value is True or (
        isinstance(value, str) and value.strip().lower() in ("true", "yes", "1", "on")
    )


def _int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return default


def resolve(path: Path) -> System:
    """Read ``path`` and every project it names; ``SystemFileError`` when it cannot be used."""
    data = load(path)
    problems: list[str] = []
    nodes: dict[str, Node] = {}
    roots: dict[Path, str] = {}
    for index, (name, agent) in enumerate(data.agents.items()):
        root = (path.parent / agent.project).resolve()
        if root in roots:
            problems.append(
                f"agents.{name}.project: {agent.project} is also agents.{roots[root]}'s project "
                "(one project is one agent: its release, namespace and card)"
            )
            continue
        roots[root] = name
        if not (root / MANIFEST_FILENAME).is_file():
            problems.append(
                f"agents.{name}.project: {agent.project} is not a graph-agents-cli project (no "
                f"{MANIFEST_FILENAME} in {root})"
            )
            continue
        try:
            project = load_project_at(root)
        except click.ClickException as exc:  # an invalid manifest or policy, a legacy file
            problems.append(f"agents.{name} ({agent.project}): {exc.format_message()}")
            continue
        client_id = agent.client_id or name
        nodes[name] = Node(
            name, index, root, project, client_id, actor_id=agent.actor_id or client_id
        )
    clients: dict[str, str] = {}
    actors: dict[str, str] = {}
    for node in nodes.values():
        if node.client_id in clients:
            problems.append(
                f"agents.{node.name}.client_id: {node.client_id} is also agents."
                f"{clients[node.client_id]}'s: each agent needs its own client id"
            )
        clients.setdefault(node.client_id, node.name)
        if node.actor_id in actors and actors[node.actor_id] != node.name:
            problems.append(
                f"agents.{node.name}.actor_id: {node.actor_id} is also agents."
                f"{actors[node.actor_id]}'s actor id: the agents they call could not tell them "
                "apart"
            )
        actors.setdefault(node.actor_id, node.name)
    links: list[Link] = []
    for name in data.agents:
        seen: set[str] = set()
        for position, edge in enumerate(data.edges(name)):
            where = f"agents.{name}.calls[{position}]"
            if edge.agent == name:
                problems.append(f"{where}: an agent cannot call itself ({name})")
            elif edge.agent not in data.agents:
                problems.append(f"{where}: {edge.agent} is not an agent of this file")
            elif edge.agent in seen:
                problems.append(f"{where}: {name} already calls {edge.agent} (one edge per pair)")
            elif name in nodes and edge.agent in nodes:
                links.append(Link(nodes[name], nodes[edge.agent], edge))
            seen.add(edge.agent)
    if data.identity is None:
        exchanges = [link.describe() for link in links if link.auth == "exchange"]
        if exchanges:
            problems.append(
                f"identity: required, since these edges use auth: exchange: {', '.join(exchanges)}"
            )
    envs: dict[str, Env] = {}
    for env_name in data.environments:
        spec = data.environment(env_name)
        kind = LOCAL if spec.port_base is not None else URL if spec.url is not None else CLUSTER
        envs[env_name] = Env(env_name, kind, spec.port_base, spec.url)
        if kind == LOCAL:
            continue
        for node in nodes.values():
            try:
                node.config.environment(env_name)
            except click.UsageError:
                problems.append(
                    f"environments.{env_name}: {node.name}'s manifest does not know this "
                    f"environment (add it under environments: in {node.root / MANIFEST_FILENAME})"
                )
    for label, keys in (
        (
            "identity.token_url",
            list(data.identity.token_url)
            if data.identity and isinstance(data.identity.token_url, dict)
            else [],
        ),
        ("database.max_connections", list(data.database.max_connections) if data.database else []),
    ):
        for key in keys:
            if key not in envs:
                problems.append(f"{label}.{key}: not an environment of this file")
    for env in envs.values():
        if env.kind == URL:
            leftover = (env.url or "").replace("{agent}", "").replace("{project}", "")
            leftover = leftover.replace("{env}", "")
            if "{" in leftover or "}" in leftover:
                problems.append(
                    f"environments.{env.name}.url: only {{agent}}, {{project}} and {{env}} may be "
                    "filled in"
                )
    if problems:
        lines = "\n".join(f"  - {problem}" for problem in problems)
        raise SystemFileError(f"{path} cannot be used:\n{lines}")
    return System(path, data, nodes, links, envs)


def locate(file: str | None) -> Path:
    """The system file: ``--file``, else ``graph-agents-system.yaml`` here or above."""
    from graph_agents_cli.system._model import SYSTEM_FILENAME, find

    if file:
        path = Path(file)
        if not path.is_file():
            raise SystemFileError(f"{file}: no such file")
        return path.resolve()
    found = find()
    if found is None:
        raise SystemFileError(f"no {SYSTEM_FILENAME} in this directory or its parents; pass --file")
    return found


__all__ = [
    "CLUSTER",
    "DENY",
    "LOCAL",
    "RELAY",
    "URL",
    "Env",
    "Gate",
    "Link",
    "Node",
    "System",
    "locate",
    "resolve",
]
