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

"""``system apply``: make every project of a system agree with the file.

One ``Plan`` per project, built from the project's files as they are (so a
second run finds nothing to do):

- **as a caller**, for each edge, what ``peer add`` writes (``plan_add``, with the
  path from the called agent's A2A name and the audience from its name), and per
  environment the called agent's URL (``<PEER>_AGENT_URL``), the issuer's token
  endpoint (``TOKEN_EXCHANGE_URL``) and ``networkPolicy.egressTo`` to the called
  agent's pods; ``TOKEN_EXCHANGE_CLIENT_ID`` is the file's ``client_id``. A peer
  that is an agent of the file and has left ``calls`` is removed
  (``plan_remove``); other peers are never touched.
- **as a called agent**: ``appUrl`` per environment (the URL callers dial, so
  their card check passes), ``AUTH_JWT_AUDIENCE`` when it is empty, the callers'
  client ids added to ``AUTH_ALLOWED_ACTORS``, and per environment
  ``networkPolicy.ingressFrom`` from the callers' pods, plus the Gateway's
  namespace while the route publishes anything (the Gateway must still reach
  ``/chat`` and whatever else it routes once the policy is on).

Never written: approval gates (a called agent's ``decide_with: relayed`` stays a
reviewed ``api approval`` command, printed), secrets, ``.env``, and anything for
local environments (their lines are printed). An existing peer's entry is
rewritten only where it differs from the file; its limits, timeouts, approval
timeout, resource indicator and description stay (``plan_add(keep_tuning=True)``).
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click

from graph_agents_cli._api_policy import (
    TOKEN_EXCHANGE_CLIENT_ID_ENV,
    TOKEN_EXCHANGE_URL_ENV,
)
from graph_agents_cli.api._files import Plan, read_text, values_env_set
from graph_agents_cli.peer import _generate as gen
from graph_agents_cli.peer.cmd_peer import plan_add, plan_remove
from graph_agents_cli.scaffold.utils.keyedit import EditError, YamlText
from graph_agents_cli.system._system import CLUSTER, LOCAL, Link, Node, System

NS_LABEL = "kubernetes.io/metadata.name"
INGRESS_PATH = ("networkPolicy", "ingressFrom")
EGRESS_PATH = ("networkPolicy", "egressTo")
ALLOWED_ACTORS_ENV = "AUTH_ALLOWED_ACTORS"
AUDIENCE_ENV = "AUTH_JWT_AUDIENCE"
ALLOWED_ACTORS_COMMENT = (
    "Agents that may call this agent for a user: their actor ids (graph-agents-cli system apply "
    "adds its callers)."
)
APP_URL_COMMENT = "The URL other agents call this agent at (graph-agents-cli system apply)."


class SystemApplyError(click.ClickException):
    """A project cannot take the file's edges (exit 3); nothing was written."""

    exit_code = 3


@dataclass
class ProjectPlan:
    """What ``system apply`` changes in one project, and what it leaves to the user."""

    node: Node
    plan: Plan
    notes: list[str] = field(default_factory=list)
    todos: list[str] = field(default_factory=list)
    document: dict[str, Any] | None = None  # the policy as it will be

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def todo(self, text: str) -> None:
        if text not in self.todos:
            self.todos.append(text)


def pod_peer(namespace: str, labels: dict[str, str]) -> dict[str, Any]:
    """A NetworkPolicy peer: the agent pods (``labels``, the chart's selector labels) in
    ``namespace``; both must match."""
    return {
        "namespaceSelector": {"matchLabels": {NS_LABEL: namespace}},
        "podSelector": {"matchLabels": dict(labels)},
    }


def egress_rule(namespace: str, labels: dict[str, str], port: int | str) -> dict[str, Any]:
    """An egress rule to the agent pods, on their http port (the pod's port, not the
    Service's: a NetworkPolicy sees the connection after the Service has translated it)."""
    return {"to": [pod_peer(namespace, labels)], "ports": [{"port": port, "protocol": "TCP"}]}


def gateway_peer(namespace: str) -> dict[str, Any]:
    return {"namespaceSelector": {"matchLabels": {NS_LABEL: namespace}}}


def _rel(node: Node, path: Path) -> str:
    return path.relative_to(node.root).as_posix()


def _edit(result: ProjectPlan, rel: str, action: Callable[[YamlText], None], what: str) -> None:
    """Apply ``action`` to the planned text of the values file ``rel``; a todo when it cannot."""
    plan = result.plan
    before = plan.current(rel)
    if before is None:
        result.todo(f"{rel}: {what} (the file does not exist)")
        return
    try:
        values = YamlText(before)
        action(values)
    except EditError as exc:
        result.todo(f"{rel}: {what} (not edited: {exc})")
        return
    plan.set_text(rel, read_text(plan.root / rel), values.text)


def _set_env(values: YamlText, key: str, value: str, *, comment: str | None = None) -> None:
    env = values.get(("env",))
    if env is None:
        values.set(("env",), {key: value})
    elif not isinstance(env, dict):
        raise EditError("env: is not a mapping")
    elif key not in env:
        values.set(("env", key), value, after="AUTH_ADMIN_ROLES", comment=comment)
    elif env.get(key) != value:
        values.set(("env", key), value)


def _sync_list(
    values: YamlText,
    path: tuple[str, ...],
    base: list[Any],
    desired: list[dict[str, Any]],
    managed: list[dict[str, Any]],
) -> None:
    """Make the list at ``path`` hold every ``desired`` entry and no ``managed`` one that is
    not desired; other entries stay. A values-<env>.yaml without the list starts from the
    base values' list (Helm replaces a list whole, so the base entries must be repeated)."""
    current = values.get(path)
    if current is None:
        start = [e for e in base if not (e in managed and e not in desired)]
        new = start + [d for d in desired if d not in start]
        if new != base:
            values.set(path, new, block=True)
        return
    if not isinstance(current, list):
        raise EditError(f"{'.'.join(path)} is not a list")
    for entry in desired:
        if entry not in values.get(path):
            values.append(path, entry)
    stale = [i for i, e in enumerate(values.get(path)) if e in managed and e not in desired]
    for index in reversed(stale):
        values.remove_item(path, index)


def _base_list(node: Node, path: tuple[str, ...]) -> list[Any]:
    found: Any = node.values(None)
    for part in path:
        found = found.get(part) if isinstance(found, dict) else None
    return copy.deepcopy(found) if isinstance(found, list) else []


def _network_enabled(node: Node, env: str) -> tuple[bool, bool]:
    policy = node.values(env).get("networkPolicy")
    policy = policy if isinstance(policy, dict) else {}
    return policy.get("enabled") is True, policy.get("restrictEgress") is True


def plan_system(system: System, envs: list[str]) -> list[ProjectPlan]:
    """A plan per project (file order); ``SystemApplyError`` before anything is written."""
    results = []
    for node in system.nodes.values():
        result = ProjectPlan(node, Plan(node.root), document=node.document)
        _plan_caller(system, node, envs, result)
        _plan_callee(system, node, envs, result)
        for item in result.plan.left_for_you:
            result.todo(item)
        results.append(result)
    return results


def _first_local(system: System, envs: list[str]) -> str | None:
    return next((e for e in envs if system.envs[e].kind == LOCAL), None)


def _plan_caller(system: System, node: Node, envs: list[str], result: ProjectPlan) -> None:
    links = system.calls(node)
    project = node.project
    names: dict[str, str] = {}
    local = _first_local(system, envs)
    for link in links:
        options = link.options(local_url=system.url(link.callee, local) if local else None)
        try:
            added = plan_add(project, result.plan, options, names=names, keep_tuning=True)
        except click.ClickException as exc:
            raise SystemApplyError(
                f"{link.describe()}: {node.name} ({node.root}) cannot take this edge; nothing was "
                f"written:\n  {exc.format_message()}"
            ) from None
        project, names = added.project, added.names
        if added.api != link.entry:  # peer add's notes, for an entry this run writes
            for note in added.notes:
                result.note(note)
    wanted = {link.callee.name for link in links}
    current = gen.peers_of(project.document, {**node.module_names, **names})
    for peer in current:
        if peer.name in system.nodes and peer.name not in wanted:
            try:
                removed = plan_remove(project, result.plan, peer.name, names=names)
            except click.ClickException as exc:
                raise SystemApplyError(
                    f"{node.name}: the peer {peer.name} left its calls but cannot be removed; "
                    f"nothing was written:\n  {exc.format_message()}"
                ) from None
            project = removed.project
            result.note(f"{peer.name} is no longer in {node.name}'s calls: its peer entry goes")
            for note in removed.notes:
                result.note(note)
    result.document = project.document
    charted = [e for e in envs if system.envs[e].charted]
    if not links:
        # No edge left: only the egress rules to agents of the file go.
        for env in charted:
            if node.has_chart and system.envs[env].kind == CLUSTER:
                _plan_egress(system, node, env, [], result)
        return
    exchange = any(link.auth == "exchange" for link in links)
    if node.has_chart:
        for link in links:
            urls = {env: system.url(link.callee, env) for env in charted}
            values_env_set(result.plan, node.config, link.url_env, urls)
        if exchange:
            tokens: dict[str, str] = {}
            for env in charted:
                token = system.token_url(env)
                if token:
                    tokens[env] = token
            values_env_set(result.plan, node.config, TOKEN_EXCHANGE_URL_ENV, tokens)
            missing = [e for e in charted if e not in tokens]
            if missing:
                result.todo(
                    f"set {TOKEN_EXCHANGE_URL_ENV} for {', '.join(missing)} in "
                    "values-<env>.yaml (identity.token_url names none)"
                )
            _set_client_id(node, result)
        for env in charted:
            if system.envs[env].kind == CLUSTER:
                _plan_egress(system, node, env, links, result)
    for env in envs:
        if system.envs[env].kind != LOCAL:
            continue
        lines = [f"{link.url_env}={system.url(link.callee, env)}" for link in links]
        if exchange:
            token = system.token_url(env)
            lines.append(f"{TOKEN_EXCHANGE_URL_ENV}={token or '<the issuer token endpoint>'}")
            lines.append(f"{TOKEN_EXCHANGE_CLIENT_ID_ENV}={node.client_id}")
        result.todo(f"in {node.name}'s .env for {env} (never written): {', '.join(lines)}")


def _set_client_id(node: Node, result: ProjectPlan) -> None:
    """``TOKEN_EXCHANGE_CLIENT_ID`` is the file's client id: in values.yaml (where `peer add`
    put the project's name), and in each values-<env>.yaml that sets its own."""
    files = node.chart_files
    for index, path in enumerate(files):
        rel = _rel(node, path)
        text = result.plan.current(rel) or ""
        try:
            env = YamlText(text).get(("env",))
        except EditError:
            env = None
        if index > 0 and not (isinstance(env, dict) and TOKEN_EXCHANGE_CLIENT_ID_ENV in env):
            continue
        _edit(
            result,
            rel,
            lambda v: _set_env(v, TOKEN_EXCHANGE_CLIENT_ID_ENV, node.client_id),
            f"set env.{TOKEN_EXCHANGE_CLIENT_ID_ENV}: {node.client_id}",
        )


def _plan_egress(
    system: System, node: Node, env: str, links: list[Link], result: ProjectPlan
) -> None:
    values_file = node.values_file(env)
    if values_file is None:
        if links:
            result.todo(f"values-{env}.yaml does not exist: no egress rules written for {env}")
        return
    desired = [
        egress_rule(
            link.callee.namespace(env), link.callee.pod_labels, link.callee.target_port(env)
        )
        for link in links
    ]
    managed = [
        egress_rule(other.namespace(env), other.pod_labels, other.target_port(env))
        for other in system.nodes.values()
    ]
    base = _base_list(node, EGRESS_PATH)
    _edit(
        result,
        _rel(node, values_file),
        lambda v: _sync_list(v, EGRESS_PATH, base, desired, managed),
        f"networkPolicy.egressTo: allow the pods of {', '.join(lk.callee.name for lk in links)}",
    )
    enabled, restricted = _network_enabled(node, env)
    if not links:
        return
    if not (enabled and restricted):
        result.note(
            f"values-{env}.yaml: the egress rules take effect with networkPolicy.enabled and "
            "restrictEgress (both off now)"
        )
    elif any(link.auth == "exchange" for link in links):
        result.todo(
            f"values-{env}.yaml: with restrictEgress, also allow the token issuer "
            f"({TOKEN_EXCHANGE_URL_ENV}) in networkPolicy.egressTo"
        )


def _stale_actors(system: System, node: Node, actors: list[str]) -> list[str]:
    """Agents of the file ``node``'s AUTH_ALLOWED_ACTORS still lists that no longer call it."""
    clients = {n.client_id for n in system.nodes.values()}
    known = clients | {n.actor_id for n in system.nodes.values()}
    known |= {f"client:{client}" for client in clients}
    listed = node.allowed_actors() if node.has_chart else []
    return [a for a in listed if a in known and a not in actors]


def _plan_callee(system: System, node: Node, envs: list[str], result: ProjectPlan) -> None:
    links = system.callers(node)
    charted = [e for e in envs if system.envs[e].charted]
    stale = _stale_actors(system, node, [link.actor for link in links if link.carries_user])
    if stale:
        result.note(
            f"AUTH_ALLOWED_ACTORS still lists {', '.join(stale)}, which no longer call "
            f"{node.name} for a user: remove them if nothing else does (apply never takes access "
            "away)"
        )
    if not links:
        # Nobody calls it any more: only the ingress rules for agents of the file go (its
        # appUrl, audience and allowed actors stay: apply never takes access away).
        for env in charted:
            if node.has_chart and system.envs[env].kind == CLUSTER:
                _plan_ingress(system, node, env, [], result)
        return
    identity = [link for link in links if link.carries_user]
    actors = list(dict.fromkeys(link.actor for link in identity))
    jwt = node.auth_policy() == "jwt"
    if node.has_chart:
        for env in charted:
            values_file = node.values_file(env)
            if values_file is None:
                result.todo(f"values-{env}.yaml does not exist: set appUrl for {env} there")
                continue
            url = system.url(node, env)
            _edit(
                result,
                _rel(node, values_file),
                lambda v, url=url: v.set(("appUrl",), url, comment=APP_URL_COMMENT),
                f"set appUrl: {url}",
            )
        if identity and jwt:
            _set_audience(node, result)
        if actors:
            _add_actors(node, actors, result)
        for env in charted:
            if system.envs[env].kind == CLUSTER:
                _plan_ingress(system, node, env, links, result)
    for env in envs:
        if system.envs[env].kind != LOCAL:
            continue
        port = system.url(node, env).rsplit(":", 1)[-1]
        lines = [f"port {port} (APP_URL=http://127.0.0.1:{port})"]
        if identity and jwt:
            lines.append(f"{AUDIENCE_ENV} including {node.name}")
        if actors:
            lines.append(f"{ALLOWED_ACTORS_ENV} including {','.join(actors)}")
        result.todo(f"run {node.name} for {env} on {', '.join(lines)} (.env is never written)")
    if not jwt and identity:
        result.note(
            f"{node.name} uses the {node.auth_policy()} auth policy: its policy must accept the "
            f"callers' credentials and set their actor ({', '.join(actors)})"
        )


def _set_audience(node: Node, result: ProjectPlan) -> None:
    """``AUTH_JWT_AUDIENCE`` becomes the agent's name where it is empty (values.yaml, and each
    values-<env>.yaml that sets it empty); a set audience is never changed (SC04 checks it)."""
    for index, path in enumerate(node.chart_files):
        rel = _rel(node, path)
        try:
            env = YamlText(result.plan.current(rel) or "").get(("env",))
        except EditError:
            continue
        env = env if isinstance(env, dict) else {}
        if index > 0 and AUDIENCE_ENV not in env:
            continue
        if str(env.get(AUDIENCE_ENV) or "").strip():
            continue
        _edit(
            result,
            rel,
            lambda v: _set_env(v, AUDIENCE_ENV, node.name),
            f"set env.{AUDIENCE_ENV}: {node.name}",
        )


def _add_actors(node: Node, actors: list[str], result: ProjectPlan) -> None:
    """The callers' actor ids join ``AUTH_ALLOWED_ACTORS`` (values.yaml, and each
    values-<env>.yaml that sets its own). Ids already there, and ``*``, stay; none goes."""
    for index, path in enumerate(node.chart_files):
        rel = _rel(node, path)
        try:
            env = YamlText(result.plan.current(rel) or "").get(("env",))
        except EditError:
            continue
        env = env if isinstance(env, dict) else {}
        if index > 0 and ALLOWED_ACTORS_ENV not in env:
            continue
        present = [a.strip() for a in str(env.get(ALLOWED_ACTORS_ENV) or "").split(",")]
        present = [a for a in present if a]
        if "*" in present:
            continue
        wanted = present + [a for a in actors if a not in present]
        if wanted == present:
            continue
        value = ",".join(wanted)
        _edit(
            result,
            rel,
            lambda v, value=value: _set_env(
                v, ALLOWED_ACTORS_ENV, value, comment=ALLOWED_ACTORS_COMMENT
            ),
            f"set env.{ALLOWED_ACTORS_ENV}: {value}",
        )


def _plan_ingress(
    system: System, node: Node, env: str, links: list[Link], result: ProjectPlan
) -> None:
    values_file = node.values_file(env)
    if values_file is None:
        return
    desired = [pod_peer(link.caller.namespace(env), link.caller.pod_labels) for link in links]
    managed = [pod_peer(o.namespace(env), o.pod_labels) for o in system.nodes.values()]
    if links and node.public_paths(env):
        values = node.values(env)
        gateway = values.get("gateway") if isinstance(values.get("gateway"), dict) else {}
        parent = gateway.get("parentRef") if isinstance(gateway.get("parentRef"), dict) else {}
        namespace = str(parent.get("namespace") or "").strip()
        if gateway.get("enabled") is True and namespace:
            desired.append(gateway_peer(namespace))
        else:
            result.todo(
                f"values-{env}.yaml: the route publishes paths, so networkPolicy.ingressFrom must "
                "also admit the namespace of the Gateway's (or ingress controller's) pods: add it"
            )
    base = _base_list(node, INGRESS_PATH)
    _edit(
        result,
        _rel(node, values_file),
        lambda v: _sync_list(v, INGRESS_PATH, base, desired, managed),
        f"networkPolicy.ingressFrom: admit {', '.join(lk.caller.name for lk in links)}",
    )
    enabled, _restricted = _network_enabled(node, env)
    if not links:
        return
    if not enabled:
        result.note(
            f"values-{env}.yaml: the ingress rules take effect with networkPolicy.enabled (off now)"
        )
    metrics = node.values(env).get("metrics")
    metrics = metrics if isinstance(metrics, dict) else {}
    monitor = (
        metrics.get("serviceMonitor") if isinstance(metrics.get("serviceMonitor"), dict) else {}
    )
    if metrics.get("scrapeAnnotations") is True or monitor.get("enabled") is True:
        result.todo(
            f"values-{env}.yaml: Prometheus scrapes /metrics: admit its namespace in "
            "networkPolicy.ingressFrom too"
        )


def relay_commands(
    system: System, results: list[ProjectPlan] | None = None
) -> list[tuple[Node, str, str]]:
    """``(called agent, why, command)`` for each relay edge a called agent's gates refuse, the
    policies as ``results`` leave them (a caller's own relay gates come with its peers)."""
    documents = {r.node.name: r.document for r in results or []}
    found = []
    for link in system.links:
        for gate in system.relay_gates(link, documents):
            why = (
                f"relays from {link.caller.name} to {link.callee.name} need the person to approve "
                f"at {link.callee.name}"
            )
            found.append((link.callee, why, gate.command(link.actor)))
    return found
