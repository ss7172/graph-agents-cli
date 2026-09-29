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

"""``system graph`` and ``system delegations``: the system drawn, and what the issuer grants.

The graph's edges carry the auth mode, whether the caller relays the person's
approvals or never sends them, and how the called agent's requester gates are
decided for that caller (``direct``: the person approves there; ``relayed``: the
caller may relay; ``mixed``; ``no gate``); its nodes carry the replicas and the
A2A task store per environment. The delegation matrix lists, for the issuer's
admin, each client and the audiences (and scopes) it must be allowed to exchange
users' tokens for, and what the issuer must guarantee (section 0.6 of the
design): the CLI cannot enforce those.
"""

from __future__ import annotations

from typing import Any

from graph_agents_cli.system._system import Link, Node, System

NO_GATE = "no gate"
ISSUER_DEFAULT = "(issuer default)"
REQUIREMENTS = (
    "each client may exchange only for the audiences listed here (RFC 8693 may_act, section "
    "4.4, or the issuer's client policy), and no other audiences",
    "an exchanged token carries act naming the calling client, earlier agents nested inside "
    "(without act, a callee reads the call as the person's own unless AUTH_JWT_DIRECT_CLIENTS "
    "excludes the client)",
    "no exchange of service tokens, and no exchange of a token issued to another client",
    "expires_in of 300 s or less for exchanged tokens, and the narrowed scope where the issuer "
    "supports it",
)


def decides(link: Link) -> str:
    """How the callee's requester gates are decided for ``link``'s caller."""
    gates = link.callee.requester_gates()
    if not gates:
        return NO_GATE
    relayed = [gate.relays(link.actor) for gate in gates]
    if all(relayed):
        return "relayed"
    return "mixed" if any(relayed) else "direct"


def _envs(system: System) -> list[str]:
    return [name for name, env in system.envs.items() if env.charted]


def _replicas(node: Node, env: str) -> int | str:
    count, autoscaled = node.replicas(env)
    return f"hpa<={count}" if autoscaled else count


def graph_data(system: System) -> dict[str, Any]:
    envs = _envs(system)
    nodes = []
    for node in system.nodes.values():
        entry: dict[str, Any] = {
            "name": node.name,
            "project": node.config.project_name,
            "client_id": node.client_id,
            "auth_policy": node.auth_policy(),
            "runtime": node.config.runtime,
        }
        if node.has_chart and envs:
            entry["replicas"] = {env: _replicas(node, env) for env in envs}
            entry["task_store"] = {env: node.task_store(env) for env in envs}
        else:
            entry["task_store"] = {"(local)": node.task_store()}
        nodes.append(entry)
    edges = []
    for link in system.links:
        edges.append(
            {
                "from": link.caller.name,
                "to": link.callee.name,
                "auth": link.auth,
                "approvals": link.edge.approvals,
                "callee_decides": decides(link) if link.edge.approvals == "relay" else None,
                "declared": link.existing is not None,
            }
        )
    return {"name": system.file.name, "nodes": nodes, "edges": edges}


def _node_lines(node: dict[str, Any]) -> list[str]:
    lines = [str(node["name"])]
    replicas = node.get("replicas") or {}
    if replicas:
        lines.append("replicas " + ", ".join(f"{e} {n}" for e, n in replicas.items()))
    stores = sorted(set((node.get("task_store") or {}).values()))
    if stores:
        lines.append("tasks " + "/".join(stores))
    return lines


def _edge_label(edge: dict[str, Any]) -> str:
    """``exchange, relay (orders: direct)``: the auth mode, relay or deny, and for a relay how
    the called agent's requester gates are decided for this caller."""
    label = f"{edge['auth']}, {edge['approvals']}"
    if edge.get("callee_decides"):
        label += f" ({edge['to']}: {edge['callee_decides']})"
    if not edge["declared"]:
        label += " [not applied]"
    return label


def mermaid(data: dict[str, Any]) -> str:
    lines = ["flowchart LR"]
    for node in data["nodes"]:
        label = "<br/>".join(_node_lines(node)).replace('"', "'")
        lines.append(f'  a_{node["name"]}["{label}"]')
    for edge in data["edges"]:
        label = _edge_label(edge).replace('"', "'")
        lines.append(f'  a_{edge["from"]} -->|"{label}"| a_{edge["to"]}')
    return "\n".join(lines) + "\n"


def _dot_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def dot(data: dict[str, Any]) -> str:
    lines = [f"digraph {_dot_quote(data['name'])} {{", "  rankdir=LR;", "  node [shape=box];"]
    for node in data["nodes"]:
        label = "\\n".join(
            line.replace("\\", "\\\\").replace('"', '\\"') for line in _node_lines(node)
        )
        lines.append(f'  {_dot_quote(node["name"])} [label="{label}"];')
    for edge in data["edges"]:
        style = "" if edge["declared"] else ", style=dashed"
        lines.append(
            f"  {_dot_quote(edge['from'])} -> {_dot_quote(edge['to'])} "
            f"[label={_dot_quote(_edge_label(edge))}{style}];"
        )
    lines.append("}")
    return "\n".join(lines) + "\n"


def delegations(system: System) -> dict[str, Any]:
    """The issuer's delegation matrix: one row per (client, audience, scope)."""
    rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    for link in system.links:
        if link.auth != "exchange":
            continue
        scope = link.edge.scope or ISSUER_DEFAULT
        key = (link.caller.client_id, link.audience, scope)
        because = link.describe() + (" (relay)" if link.edge.approvals == "relay" else "")
        row = rows.setdefault(
            key,
            {
                "client": link.caller.client_id,
                "audience": link.audience,
                "scope": scope,
                "because": [],
                "actorless": False,
            },
        )
        row["because"].append(because)
        row["actorless"] = row["actorless"] or link.edge.allow_actorless
    identity = system.file.identity
    return {
        "issuer": identity.issuer if identity else None,
        "rows": list(rows.values()),
        "requirements": list(REQUIREMENTS),
    }


def delegations_table(data: dict[str, Any]) -> str:
    headers = ("client", "may exchange for audience", "scope", "because")
    rows = [
        (
            row["client"],
            row["audience"],
            row["scope"],
            "; ".join(row["because"]) + (" [names no actor]" if row["actorless"] else ""),
        )
        for row in data["rows"]
    ]
    widths = [max(len(str(r[i])) for r in [headers, *rows]) for i in range(3)]
    lines = []
    for row in [headers, *rows]:
        cells = [str(row[i]).ljust(widths[i]) for i in range(3)]
        lines.append("   ".join([*cells, str(row[3])]).rstrip())
    return "\n".join(lines) + "\n"
