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

"""``system check``: what would keep the agents of a system from calling each other.

Every check reads the projects' files (their manifests, policies, generated peers
modules and chart values); ``--live`` adds what only the cluster knows (SC14,
SC15). Per environment, the values checks read each chart's values merged as
``deploy --env`` merges them; a local environment (``port_base``) has no chart
values, so its settings live in ``.env``, which is never read here.

| Id | Check | Severity |
|---|---|---|
| SC01 | the projects exist (else exit 3), run a 0.3 runtime, have a chart and values file per charted environment | error |
| SC02 | every edge is a peer in the caller, as the file says; no peer of an agent the file no longer lets it call; tools/a2a_peers.py in step | error |
| SC03 | the caller's a2a.path is the callee's A2A mount | error |
| SC04 | auth compatibility (issuer, audience, the callee's policy) | error / warning |
| SC05 | per environment, the callee's appUrl is the URL the caller dials | error |
| SC06 | a callee with several replicas keeps A2A tasks in memory | error |
| SC07 | a relay edge's callee gates are decided directly | warning |
| SC08 | the callee's AUTH_ALLOWED_ACTORS lacks the caller | error |
| SC09 | cycles; a delegation chain longer than AUTH_MAX_DELEGATION_DEPTH | warning / error |
| SC10 | a shared database's connection budget | error |
| SC11 | the caller's secrets.keys holds the edges' secrets; PRINCIPAL_HASH_SALT | error / warning |
| SC12 | exchange or forward in a langgraph-server caller | error |
| SC13 | a callee's route still publishes its A2A path | warning |
| SC14 | (--live) the Services have a ready endpoint (EndpointSlices); the URLs and the token URL answer | error |
| SC15 | (--live) the Secrets hold the edges' keys (names only) | error / warning |
"""

from __future__ import annotations

import json
import socket
import urllib.parse
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import click

from graph_agents_cli._api_policy import (
    TOKEN_EXCHANGE_SECRET_ENV,
    forward_runtime_problem,
    summarize,
)
from graph_agents_cli.deploy import _image, _kube
from graph_agents_cli.deploy._kube import Target
from graph_agents_cli.peer import _generate as gen
from graph_agents_cli.system._system import (
    CLUSTER,
    DEFAULT_DB_POOL_MAX_SIZE,
    DEFAULT_LANGGRAPH_POOL_MAX_SIZE,
    LOCAL,
    Link,
    Node,
    System,
)

ERROR = "error"
WARNING = "warning"
SALT = "PRINCIPAL_HASH_SALT"
DB_HEADROOM = 0.10  # SC10 keeps 10% of max_connections for everything else
# How many paths SC09 explores before giving up (a dense graph of many agents).
MAX_PATH_STEPS = 200_000
CARD_TIMEOUT_S = 5.0


@dataclass
class Finding:
    """One problem ``system check`` found."""

    check: str
    severity: str
    message: str
    agent: str | None = None
    env: str | None = None
    fix: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


def _where(system: System, node: Node) -> str:
    try:
        return node.root.relative_to(system.root).as_posix() or "."
    except ValueError:
        return str(node.root)


# ---------------------------------------------------------------------------
# Static checks (the projects' files)
# ---------------------------------------------------------------------------


def _sc01(system: System, envs: list[str]) -> list[Finding]:
    found = []
    for node in system.nodes.values():
        problem = node.runtime_problem
        if problem:
            found.append(
                Finding(
                    "SC01",
                    ERROR,
                    f"{node.name}: {problem}",
                    agent=node.name,
                    fix=f"cd {_where(system, node)} && graph-agents-cli scaffold upgrade",
                )
            )
        for env in envs:
            if not system.envs[env].charted:
                continue
            if not node.has_chart:
                found.append(
                    Finding(
                        "SC01",
                        ERROR,
                        f"{node.name} has no Helm chart (deployment_target "
                        f"{node.config.deployment_target}): it cannot run in {env}",
                        agent=node.name,
                        env=env,
                        fix="graph-agents-cli scaffold enhance --deployment-target kubernetes",
                    )
                )
            elif node.values_file(env) is None:
                found.append(
                    Finding(
                        "SC01",
                        ERROR,
                        f"{node.name}'s chart has no values-{env}.yaml",
                        agent=node.name,
                        env=env,
                    )
                )
    return found


def _differs(have: dict[str, Any], want: dict[str, Any]) -> list[str]:
    return sorted(k for k in set(have) | set(want) if have.get(k) != want.get(k))


def _sc02(system: System, expected: dict[str, dict[str, Any] | None]) -> list[Finding]:
    """``expected``: each caller's policy as ``system apply`` would leave it (None: no plan,
    the reason is reported by the caller)."""
    found = []
    for node in system.nodes.values():
        links = system.calls(node)
        fix = "graph-agents-cli system apply"
        want_doc = expected.get(node.name)
        for link in links:
            peer = link.existing
            if peer is None:
                found.append(
                    Finding(
                        "SC02",
                        ERROR,
                        f"{link.describe()}: {node.name} has no protocol: a2a API for "
                        f"{link.callee.name} (no peer {link.callee.name} in its api-policy.yaml)",
                        agent=node.name,
                        fix=fix,
                    )
                )
                continue
            want = ((want_doc or {}).get("apis") or {}).get(peer.api)
            have = link.entry or {}
            if want_doc is not None and isinstance(want, dict) and have != want:
                keys = ", ".join(_differs(have, want))
                found.append(
                    Finding(
                        "SC02",
                        ERROR,
                        f"{link.describe()}: {node.name}'s {peer.api} differs from the file "
                        f"({keys})",
                        agent=node.name,
                        fix=fix,
                    )
                )
        wanted = {link.callee.name for link in links}
        for name, peer in node.peers.items():
            if name in system.nodes and name not in wanted:
                found.append(
                    Finding(
                        "SC02",
                        ERROR,
                        f"{node.name} still has the peer {name} ({peer.api}), which the file no "
                        "longer lets it call",
                        agent=node.name,
                        fix=fix,
                    )
                )
        if node.document is not None or node.module_text is not None:
            peers = gen.peers_of(node.document, node.module_names)
            text = gen.module_text(peers, node.config.agent_directory)
            if text != node.module_text:
                rel = f"{node.config.agent_directory}/tools/{gen.MODULE_NAME}"
                found.append(
                    Finding(
                        "SC02",
                        ERROR,
                        f"{node.name}: {rel} is out of step with its api-policy.yaml",
                        agent=node.name,
                        fix=f"cd {_where(system, node)} && graph-agents-cli peer sync (or {fix})",
                    )
                )
    return found


def _sc03(system: System, envs: list[str]) -> list[Finding]:
    found = []
    for link in system.links:
        entry = link.entry
        if entry is None:
            continue  # SC02
        path = str((entry.get("a2a") or {}).get("path") or "")
        places: list[str | None] = [None] + [e for e in envs if system.envs[e].charted]
        for env in places:
            mount = link.callee.mount(env)
            if path != mount:
                where = f" in {env}" if env else ""
                found.append(
                    Finding(
                        "SC03",
                        ERROR,
                        f"{link.describe()}: {link.caller.name} calls {path}, but "
                        f"{link.callee.name} serves A2A at {mount}{where} (A2A_NAME "
                        f"{link.callee.a2a_name(env)})",
                        agent=link.caller.name,
                        env=env,
                        fix="graph-agents-cli system apply",
                    )
                )
                break
    return found


def _csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _sc04(system: System, envs: list[str]) -> list[Finding]:
    found = []
    issuer = system.file.identity.issuer if system.file.identity else None
    charted = [e for e in envs if system.envs[e].charted]
    for link in system.links:
        caller, callee = link.caller, link.callee
        if caller.config.auth_policy == "shared-bearer" and link.carries_user:
            found.append(
                Finding(
                    "SC04",
                    ERROR,
                    f"{link.describe()}: auth {link.auth} needs a user token, and {caller.name} "
                    "uses shared-bearer (every caller is one principal)",
                    agent=caller.name,
                    fix=f"give the edge auth: bearer (with {callee.name}'s agent key)",
                )
            )
        # The callee's policy: the manifest's, and each environment's where its chart sets
        # another (env.AUTH_POLICY).
        base = callee.auth_policy()
        finding = _policy_finding(link, base, None)
        if finding is not None:
            found.append(finding)
        for env in charted:
            if not callee.has_chart:
                break
            policy = callee.auth_policy(env)
            if policy != base:
                finding = _policy_finding(link, policy, env)
                if finding is not None:
                    found.append(finding)
            if policy == "jwt" and link.carries_user:
                found.extend(_jwt_findings(link, issuer, env))
    return found


def _policy_finding(link: Link, policy: str, env: str | None) -> Finding | None:
    """Whether ``link``'s auth mode can work with the callee's auth policy at all."""
    callee, what = link.callee, link.describe()
    where = f" in {env}" if env else ""
    if link.auth == "bearer":
        if policy == "jwt":
            return Finding(
                "SC04",
                ERROR,
                f"{what}: auth bearer sends a static key, and {callee.name} verifies JWTs "
                f"(jwt{where})",
                agent=callee.name,
                env=env,
                fix="give the edge auth: exchange",
            )
        if policy == "shared-bearer" and "API_KEY" not in callee.config.secret_keys:
            return Finding(
                "SC04",
                ERROR,
                f"{what}: {callee.name}'s secrets.keys has no API_KEY, the key "
                f"{link.caller.name} sends",
                agent=callee.name,
                fix="add API_KEY to secrets.keys in its manifest",
            )
        if policy == "custom":
            return Finding(
                "SC04",
                WARNING,
                f"{what}: {callee.name}'s custom policy{where} must accept {link.caller.name}'s "
                "key",
                agent=callee.name,
                env=env,
            )
        return None
    if policy == "shared-bearer":
        return Finding(
            "SC04",
            ERROR,
            f"{what}: auth {link.auth} carries the user's token, and {callee.name} uses "
            f"shared-bearer{where}: it cannot verify it",
            agent=callee.name,
            env=env,
            fix=f"give the edge auth: bearer, or give {callee.name} the jwt auth policy",
        )
    if policy == "custom":
        return Finding(
            "SC04",
            WARNING,
            f"{what}: {callee.name}'s custom policy{where} must accept the token "
            f"{link.caller.name} sends (audience {link.audience}) and set its actor",
            agent=callee.name,
            env=env,
        )
    return None


def _jwt_findings(link: Link, issuer: str | None, env: str) -> list[Finding]:
    """A jwt callee's issuer and audience in ``env``, against the tokens the caller sends."""
    callee, what = link.callee, link.describe()
    found = []
    if link.auth == "exchange" and issuer is not None:
        have = callee.setting(env, "AUTH_JWT_ISSUER")
        if have != issuer:
            found.append(
                Finding(
                    "SC04",
                    ERROR,
                    f"{what}: {callee.name}'s AUTH_JWT_ISSUER in {env} is {have or '(empty)'}, "
                    f"not the file's issuer {issuer}",
                    agent=callee.name,
                    env=env,
                    fix=f"set env.AUTH_JWT_ISSUER: {issuer} in its values-{env}.yaml",
                )
            )
    audiences = _csv(callee.setting(env, "AUTH_JWT_AUDIENCE"))
    if link.audience not in audiences:
        found.append(
            Finding(
                "SC04",
                ERROR,
                f"{what}: {callee.name}'s AUTH_JWT_AUDIENCE in {env} "
                f"({','.join(audiences) or 'empty'}) does not include {link.audience}, the "
                f"audience {link.caller.name}'s tokens are minted for",
                agent=callee.name,
                env=env,
                fix=(
                    "graph-agents-cli system apply (sets it when empty), or add "
                    f"{link.audience} to it"
                ),
            )
        )
    return found


def _placeholder(value: str) -> bool:
    return not value or _image.has_placeholder(value)


def _sc05(system: System, envs: list[str]) -> list[Finding]:
    found = []
    for env in envs:
        if not system.envs[env].charted:
            continue
        for link in system.links:
            if not (link.caller.has_chart and link.callee.has_chart):
                continue
            dialled = link.caller.setting(env, link.url_env)
            served = link.callee.app_url(env)
            if _placeholder(dialled):
                message = (
                    f"{link.describe()}: {link.caller.name}'s {link.url_env} in {env} is "
                    f"{dialled or 'unset'}"
                )
            elif not served:
                message = (
                    f"{link.describe()}: {link.callee.name} has no appUrl in {env}: its card "
                    f"names its bind address, not {dialled}"
                )
            elif dialled.rstrip("/") != served.rstrip("/"):
                message = (
                    f"{link.describe()}: {link.caller.name} dials {dialled} in {env}, but "
                    f"{link.callee.name}'s card names {served} (appUrl)"
                )
            else:
                continue
            found.append(
                Finding(
                    "SC05",
                    ERROR,
                    message,
                    agent=link.caller.name,
                    env=env,
                    fix="graph-agents-cli system apply",
                )
            )
    return found


def _sc06(system: System, envs: list[str]) -> list[Finding]:
    found = []
    for env in envs:
        if not system.envs[env].charted:
            continue
        for node in system.nodes.values():
            if not system.callers(node) or not node.has_chart:
                continue
            replicas, autoscaled = node.replicas(env)
            if node.task_store(env) == "memory" and (replicas > 1 or autoscaled):
                many = "an HPA" if autoscaled else f"{replicas} replicas"
                found.append(
                    Finding(
                        "SC06",
                        ERROR,
                        f"{node.name} runs {many} in {env} with A2A tasks in memory "
                        "(CHECKPOINTER=memory): a caller's GetTask or approval that reaches "
                        "another replica finds no task",
                        agent=node.name,
                        env=env,
                        fix="set CHECKPOINTER: postgres (the tasks then live in the database), "
                        "or one replica",
                    )
                )
    return found


def _sc07(system: System) -> list[Finding]:
    found = []
    for link in system.links:
        for gate in system.relay_gates(link):
            found.append(
                Finding(
                    "SC07",
                    WARNING,
                    f"relays to {link.callee.name} need the person to approve at "
                    f"{link.callee.name} ({link.describe()}: its gate {gate.api}"
                    + (f" rule {gate.rule}" if gate.rule is not None else "")
                    + f" is decided {gate.decide_with})",
                    agent=link.callee.name,
                    fix=f"cd {_where(system, link.callee)} && {gate.command(link.actor)}",
                )
            )
    return found


def _sc08(system: System, envs: list[str]) -> list[Finding]:
    found = []
    for env in envs:
        if not system.envs[env].charted:
            continue
        for link in system.links:
            if not link.carries_user or not link.callee.has_chart:
                continue
            actors = link.callee.allowed_actors(env)
            if "*" in actors or link.actor in actors:
                continue
            found.append(
                Finding(
                    "SC08",
                    ERROR,
                    f"{link.describe()}: {link.callee.name}'s AUTH_ALLOWED_ACTORS in {env} "
                    f"({','.join(actors) or 'empty'}) lacks {link.actor}: it refuses "
                    f"{link.caller.name}'s calls (403)",
                    agent=link.callee.name,
                    env=env,
                    fix="graph-agents-cli system apply",
                )
            )
    return found


def cycles(system: System) -> list[list[str]]:
    """The cycles of the call graph, each once (its agents in call order)."""
    graph = {
        name: [lk.callee.name for lk in system.links if lk.caller.name == name]
        for name in system.nodes
    }
    found: list[list[str]] = []
    seen: set[frozenset[str]] = set()

    def walk(start: str, here: str, path: list[str]) -> None:
        for nxt in graph[here]:
            if nxt == start:
                key = frozenset(path)
                if key not in seen:
                    seen.add(key)
                    found.append([*path, start])
            elif nxt not in path and list(system.nodes).index(nxt) > list(system.nodes).index(
                start
            ):
                walk(start, nxt, [*path, nxt])

    for name in system.nodes:
        walk(name, name, [name])
    return found


def chain_depths(system: System) -> dict[str, tuple[int, list[str]]]:
    """For each agent, the longest chain of exchange edges ending at it: the agents between
    the user and it, which its AUTH_MAX_DELEGATION_DEPTH caps (forward and bearer edges add
    no actor). ``(length, path)``."""
    into: dict[str, list[str]] = {name: [] for name in system.nodes}
    for link in system.links:
        if link.auth == "exchange":
            into[link.callee.name].append(link.caller.name)
    best: dict[str, tuple[int, list[str]]] = {}
    steps = 0

    def longest(node: str, seen: frozenset[str]) -> tuple[int, list[str]]:
        nonlocal steps
        steps += 1
        if steps > MAX_PATH_STEPS:
            return 0, [node]
        result = (0, [node])
        for caller in into[node]:
            if caller in seen:
                continue
            length, path = longest(caller, seen | {caller})
            if length + 1 > result[0]:
                result = (length + 1, [*path, node])
        return result

    for name in system.nodes:
        best[name] = longest(name, frozenset({name}))
    return best


def _sc09(system: System, envs: list[str]) -> list[Finding]:
    found = []
    for cycle in cycles(system):
        found.append(
            Finding(
                "SC09",
                WARNING,
                f"a cycle: {' -> '.join(cycle)} (a request can loop back; the client refuses a "
                "peer already in the delegation chain)",
            )
        )
    places: list[str | None] = [None] + [e for e in envs if system.envs[e].charted]
    for name, (length, path) in chain_depths(system).items():
        node = system.nodes[name]
        for env in places:
            limit = node.max_delegation_depth(env)
            if length > limit:
                where = f" in {env}" if env else ""
                found.append(
                    Finding(
                        "SC09",
                        ERROR,
                        f"{' -> '.join(path)}: {length} agents stand between the user and {name}, "
                        f"more than its AUTH_MAX_DELEGATION_DEPTH ({limit}){where}: it refuses "
                        "that chain (401)",
                        agent=name,
                        env=env,
                        fix=f"raise AUTH_MAX_DELEGATION_DEPTH on {name} (at most 8), or shorten "
                        "the chain",
                    )
                )
                break
    return found


def _pool(node: Node, env: str) -> tuple[int, str]:
    """Connections one replica of ``node`` opens at most, and how that was counted."""
    raw = node.setting(env, "DB_POOL_MAX_SIZE")
    app = int(raw) if raw.isdigit() else DEFAULT_DB_POOL_MAX_SIZE
    if node.config.runtime != "langgraph-server":
        return app, f"DB_POOL_MAX_SIZE {app}"
    raw = node.setting(env, "LANGGRAPH_POSTGRES_POOL_MAX_SIZE")
    server = int(raw) if raw.isdigit() else DEFAULT_LANGGRAPH_POOL_MAX_SIZE
    return app + server, f"DB_POOL_MAX_SIZE {app} + LANGGRAPH_POSTGRES_POOL_MAX_SIZE {server}"


def _sc10(system: System, envs: list[str]) -> list[Finding]:
    database = system.file.database
    if database is None:
        return []
    found = []
    for env in envs:
        limit = database.max_connections.get(env)
        if limit is None:
            continue
        total, parts = 0, []
        for node in system.nodes.values():
            values = node.values(env)
            bundled = isinstance(values.get("postgresql"), dict) and (
                values["postgresql"].get("enabled") is True
            )
            if not node.has_chart or bundled or node.task_store(env) == "memory":
                continue  # its own database, or none
            replicas, _autoscaled = node.replicas(env)
            per, how = _pool(node, env)
            total += replicas * per
            parts.append(f"{node.name} {replicas} x {per} ({how})")
        budget = int(limit * (1 - DB_HEADROOM))
        if total > budget:
            found.append(
                Finding(
                    "SC10",
                    ERROR,
                    f"the agents may open {total} connections to the shared database in {env}, "
                    f"more than {budget} (max_connections {limit} less 10%): " + "; ".join(parts),
                    env=env,
                    fix="lower DB_POOL_MAX_SIZE or the replicas, raise max_connections, or put "
                    "PgBouncer in front (above about 120 connections)",
                )
            )
    return found


def _sc11(system: System) -> list[Finding]:
    found = []
    for node in system.nodes.values():
        links = system.calls(node)
        if not links:
            continue
        keys = set(node.config.secret_keys)
        needed = []
        if any(link.auth == "exchange" for link in links):
            needed.append(TOKEN_EXCHANGE_SECRET_ENV)
        needed += [link.token_env for link in links if link.token_env]
        for key in dict.fromkeys(needed):
            if key and key not in keys:
                found.append(
                    Finding(
                        "SC11",
                        ERROR,
                        f"{node.name}'s secrets.keys lacks {key}: its pods never get it",
                        agent=node.name,
                        fix="graph-agents-cli system apply (or add it to secrets.keys)",
                    )
                )
        if SALT not in keys:
            found.append(
                Finding(
                    "SC11",
                    WARNING,
                    f"{node.name}: {SALT} is not in secrets.keys, so the conversation ids it "
                    "sends are keyed without a secret (anyone who knows a thread id can compute "
                    "them)",
                    agent=node.name,
                    fix=f"add {SALT} to secrets.keys and set it in .env.<env>",
                )
            )
    return found


def _sc12(system: System) -> list[Finding]:
    found = []
    for node in system.nodes.values():
        links = [link for link in system.calls(node) if link.carries_user]
        if not links or node.config.runtime != "langgraph-server":
            continue
        document = {
            "apis": {
                link.api_name: {"base_url_env": link.url_env, "auth": link.auth} for link in links
            }
        }
        problem = forward_runtime_problem(summarize(document), node.config.runtime)
        if problem:
            found.append(Finding("SC12", ERROR, f"{node.name}: {problem}", agent=node.name))
    return found


def _publishes(path: dict[str, Any], mount: str) -> bool:
    value = str(path.get("path") or "")
    if str(path.get("type") or "") == "Exact":
        return value == mount
    value = value.rstrip("/") or "/"
    return value == "/" or mount == value or mount.startswith(value + "/")


def _sc13(system: System, envs: list[str]) -> list[Finding]:
    """Only where agents call each other inside the cluster: with a url environment they
    call the public URL, so the route must publish the A2A path there."""
    found = []
    for env in envs:
        if system.envs[env].kind != CLUSTER:
            continue
        for node in system.nodes.values():
            if not system.callers(node) or not node.has_chart:
                continue
            mount = node.mount(env)
            if any(_publishes(p, mount) for p in node.public_paths(env)):
                found.append(
                    Finding(
                        "SC13",
                        WARNING,
                        f"{node.name}'s route still publishes {mount} in {env} although agents "
                        "call it (inside the cluster)",
                        agent=node.name,
                        env=env,
                        fix=f"drop {mount} from route.publicPaths in values-{env}.yaml if only "
                        "agents call it",
                    )
                )
    return found


# ---------------------------------------------------------------------------
# Live checks (--live)
# ---------------------------------------------------------------------------


@dataclass
class Probe:
    """What ``--live`` asks the cluster and the network (replaced in tests)."""

    def ready_endpoints(self, target: Target, service: str) -> int:
        """Ready endpoints behind a Service (-1: no such Service), read from its
        EndpointSlices (the v1 Endpoints API is deprecated since Kubernetes 1.33);
        ``ToolFailed`` when kubectl cannot say."""
        found = _kube.run_cmd(
            _kube.kubectl_args(["get", "service", service, "-o", "name"], target),
            check=False,
            quiet=True,
        )
        if found.returncode != 0:
            if "(NotFound)" in (found.stderr or ""):
                return -1
            raise _kube.ToolFailed(_first_line(found))
        label = f"kubernetes.io/service-name={service}"
        result = _kube.run_cmd(
            _kube.kubectl_args(["get", "endpointslices", "-l", label, "-o", "json"], target),
            check=False,
            quiet=True,
        )
        if result.returncode != 0:
            raise _kube.ToolFailed(_first_line(result))
        body = json.loads(result.stdout or "{}")
        return sum(
            1
            for item in body.get("items") or []
            for endpoint in item.get("endpoints") or []
            # An unset `ready` means ready (the EndpointSlice API).
            if (endpoint.get("conditions") or {}).get("ready") is not False
        )

    def secret_keys(self, target: Target, name: str) -> set[str] | None:
        """The key names of a Secret (never its values); None when it does not exist."""
        result = _kube.run_cmd(
            _kube.kubectl_args(["get", "secret", name, "-o", "json"], target),
            check=False,
            quiet=True,
        )
        if result.returncode != 0:
            if "(NotFound)" in (result.stderr or ""):
                return None
            raise _kube.ToolFailed(_first_line(result))
        body = json.loads(result.stdout or "{}")
        return set(body.get("data") or {}) | set(body.get("stringData") or {})

    def resolves(self, host: str) -> str | None:
        """None when ``host`` resolves, else why not."""
        try:
            socket.getaddrinfo(host, None)
        except OSError as exc:
            return str(exc)
        return None

    def card(self, url: str) -> tuple[bool, str]:
        """GET a card with no credential: 200 or 401 is reachable (KI-120)."""
        import httpx

        try:
            response = httpx.get(
                url, headers={"A2A-Version": "1.0"}, timeout=CARD_TIMEOUT_S, follow_redirects=False
            )
        except httpx.HTTPError as exc:
            return False, f"unreachable ({type(exc).__name__})"
        if response.status_code in (200, 401):
            return True, f"HTTP {response.status_code}"
        return False, f"HTTP {response.status_code}"


def _first_line(result: Any) -> str:
    lines = (result.stderr or result.stdout or "").strip().splitlines()
    return lines[0] if lines else f"kubectl exit {result.returncode}"


def _cluster_service(host: str) -> tuple[str, str] | None:
    """``(service, namespace)`` of an in-cluster name, else None."""
    parts = host.lower().rstrip(".").split(".")
    if len(parts) >= 3 and parts[2] == "svc":
        return parts[0], parts[1]
    return None


def _target(node: Node, env: str) -> tuple[Target | None, str | None]:
    """Where kubectl looks for ``node`` in ``env``: its manifest's context; outside dev never
    the kubeconfig's current one (the `deploy` rule)."""
    context = node.context(env)
    if context is None and env != "dev":
        return None, (
            f"{node.name}'s manifest records no kube context for {env} "
            f"(environments.{env}.context): the current context is never used outside dev"
        )
    return Target(context=context, namespace=node.namespace(env)), None


def _live(system: System, envs: list[str], probe: Probe) -> list[Finding]:
    found: list[Finding] = []

    def add(check: str, severity: str, message: str, **kw: Any) -> None:
        found.append(Finding(check, severity, message, **kw))

    for env in envs:
        spec = system.envs[env]
        urls: dict[str, tuple[Link, str]] = {}
        for link in system.links:
            if spec.charted and link.caller.has_chart:
                url = link.caller.setting(env, link.url_env) or system.url(link.callee, env)
            else:
                url = system.url(link.callee, env)
            urls.setdefault(url, (link, url))
        for link, url in urls.values():
            host = urllib.parse.urlsplit(url).hostname or ""
            service = _cluster_service(host)
            if service is not None and spec.kind == CLUSTER:
                target, why = _target(link.caller, env)
                if target is None:
                    add("SC14", ERROR, why or "", agent=link.caller.name, env=env)
                    continue
                try:
                    ready = probe.ready_endpoints(
                        Target(context=target.context, namespace=service[1]), service[0]
                    )
                except _kube.ToolFailed as exc:
                    add("SC14", ERROR, f"{url}: kubectl failed: {exc.message}", env=env)
                    continue
                if ready <= 0:
                    what = "does not exist" if ready < 0 else "has no ready endpoint"
                    add(
                        "SC14",
                        ERROR,
                        f"{link.describe()}: the Service {service[0]} in {service[1]} {what}",
                        agent=link.callee.name,
                        env=env,
                        fix=f"graph-agents-cli system deploy --env {env} --only {link.callee.name}",
                    )
                continue
            problem = probe.resolves(host) if spec.kind != LOCAL else None
            if problem:
                add(
                    "SC14",
                    ERROR,
                    f"{link.describe()}: {host} does not resolve ({problem})",
                    env=env,
                )
                continue
            card = f"{url.rstrip('/')}{link.callee.mount(env if spec.charted else None)}"
            card += gen.CARD_SUFFIX
            ok, detail = probe.card(card)
            if not ok:
                add(
                    "SC14",
                    ERROR,
                    f"{link.describe()}: {card} is not reachable ({detail})",
                    agent=link.callee.name,
                    env=env,
                )
        token = system.token_url(env)
        if token and any(link.auth == "exchange" for link in system.links):
            host = urllib.parse.urlsplit(token).hostname or ""
            service = _cluster_service(host)
            if service is not None and spec.kind == CLUSTER:
                caller = next(lk.caller for lk in system.links if lk.auth == "exchange")
                target, why = _target(caller, env)
                if target is not None:
                    try:
                        ready = probe.ready_endpoints(
                            Target(context=target.context, namespace=service[1]), service[0]
                        )
                    except _kube.ToolFailed as exc:
                        ready = 0
                        why = str(exc.message)
                    if ready <= 0:
                        add(
                            "SC14",
                            ERROR,
                            f"the token URL {token}: no ready issuer behind {service[0]} in "
                            f"{service[1]}" + (f" ({why})" if why else ""),
                            env=env,
                        )
            elif spec.kind != LOCAL:
                problem = probe.resolves(host)
                if problem:
                    add(
                        "SC14",
                        ERROR,
                        f"the token URL {token} does not resolve ({problem})",
                        env=env,
                    )
        if not spec.charted:
            continue
        for node in system.nodes.values():
            links = system.calls(node)
            if not links or not node.has_chart:
                continue
            needed = []
            if any(link.auth == "exchange" for link in links):
                needed.append(TOKEN_EXCHANGE_SECRET_ENV)
            needed += [link.token_env for link in links if link.token_env]
            target, why = _target(node, env)
            if target is None:
                add("SC15", ERROR, why or "", agent=node.name, env=env)
                continue
            secret = f"{node.config.project_name}-app"
            try:
                keys = probe.secret_keys(target, secret)
            except _kube.ToolFailed as exc:
                add(
                    "SC15",
                    ERROR,
                    f"{node.name}: kubectl failed: {exc.message}",
                    agent=node.name,
                    env=env,
                )
                continue
            missing = [k for k in dict.fromkeys(needed) if k and k not in (keys or set())]
            if missing:
                add(
                    "SC15",
                    ERROR,
                    f"{node.name}'s Secret {secret} in {target.namespace} "
                    + ("does not exist" if keys is None else f"lacks {', '.join(missing)}"),
                    agent=node.name,
                    env=env,
                    fix=f"put them in .env.{env}, then graph-agents-cli secrets apply --env {env}",
                )
            if keys is not None and SALT not in keys:
                add(
                    "SC15",
                    WARNING,
                    f"{node.name}'s Secret {secret} has no {SALT}",
                    agent=node.name,
                    env=env,
                )
    return found


# ---------------------------------------------------------------------------


def run(
    system: System,
    envs: list[str],
    *,
    live: bool = False,
    probe: Probe | None = None,
    expected: dict[str, dict[str, Any] | None] | None = None,
) -> list[Finding]:
    """Every finding for ``envs`` (static checks always; SC14 and SC15 with ``live``)."""
    from graph_agents_cli.system import _apply

    if expected is None:
        expected = {}
        try:
            for result in _apply.plan_system(system, envs):
                expected[result.node.name] = result.document
        except click.ClickException as exc:
            return [
                *_static(system, envs, {}),
                Finding(
                    "SC02",
                    ERROR,
                    exc.format_message(),
                    fix="fix the project, then graph-agents-cli system apply",
                ),
            ]
    found = _static(system, envs, expected)
    if live:
        found += _live(system, envs, probe or Probe())
    return found


def _static(
    system: System, envs: list[str], expected: dict[str, dict[str, Any] | None]
) -> list[Finding]:
    checks: list[Callable[[], list[Finding]]] = [
        lambda: _sc01(system, envs),
        lambda: _sc02(system, expected),
        lambda: _sc03(system, envs),
        lambda: _sc04(system, envs),
        lambda: _sc05(system, envs),
        lambda: _sc06(system, envs),
        lambda: _sc07(system),
        lambda: _sc08(system, envs),
        lambda: _sc09(system, envs),
        lambda: _sc10(system, envs),
        lambda: _sc11(system),
        lambda: _sc12(system),
        lambda: _sc13(system, envs),
    ]
    found: list[Finding] = []
    for check in checks:
        found.extend(check())
    return found
