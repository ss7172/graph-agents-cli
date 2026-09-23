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
"""Load a locally built image into a single-node dev cluster (kind, k3d, k3s, minikube, ...).

A context name is only a label: a renamed kind context would otherwise make
``deploy`` push a dev build to the real registry, and a production context that
happens to be called ``kind-...`` would be treated as a laptop cluster. So the
cluster is identified from the cluster itself: its nodes' ``providerID``, name
and labels (kind ``kind://<provider>/<cluster>/<node>``, k3d ``k3d-<cluster>-server-N``,
k3s ``k3s://``, minikube's ``minikube.k8s.io/name`` label, Docker Desktop and
OrbStack node names). A kind cluster is also confirmed with ``kind get
clusters``, since ``kind load`` only reaches clusters on this machine. Only
when the nodes cannot be read (no permission to list nodes, cluster down)
does the context name decide, and a ``kind-*`` name is still confirmed with
the kind CLI.

Only the ``docker`` CLI is supported for builds; Docker Desktop, Rancher
Desktop and OrbStack share the daemon with the cluster, so nothing needs
loading there.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from graph_agents_cli.deploy import _kube

KIND = "kind"
K3D = "k3d"
K3S = "k3s"
MINIKUBE = "minikube"
SHARED_DAEMON = "shared-daemon"

_K3D_NODE = re.compile(r"^k3d-(?P<cluster>.+)-(?:server|agent)-\d+$")
_SHARED_DAEMON_NODES = ("docker-desktop", "orbstack")
NODE_READ_TIMEOUT = "10s"


@dataclass(frozen=True)
class LocalCluster:
    """A single-node dev cluster the image can be loaded into."""

    kind: str
    name: str = ""
    evidence: str = ""

    def describe(self) -> str:
        label = {
            KIND: f"kind cluster {self.name!r}",
            K3D: f"k3d cluster {self.name!r}",
            K3S: "k3s",
            MINIKUBE: f"minikube profile {self.name!r}",
            SHARED_DAEMON: f"{self.name or 'dev cluster'} (shares the docker daemon)",
        }.get(self.kind, self.kind)
        return f"{label}{f', {self.evidence}' if self.evidence else ''}"


def from_context_name(context: str | None) -> LocalCluster | None:
    """The dev cluster a context name suggests (unverified), or ``None``."""
    name = (context or "").strip()
    if name.startswith("kind-"):
        return LocalCluster(KIND, name[len("kind-") :], "from the context name")
    if name.startswith("k3d-"):
        return LocalCluster(K3D, name[len("k3d-") :], "from the context name")
    if name.startswith("minikube"):
        return LocalCluster(MINIKUBE, name, "from the context name")
    if name.startswith("k3s"):
        return LocalCluster(K3S, "", "from the context name")
    if name in ("docker-desktop", "rancher-desktop", "orbstack"):
        return LocalCluster(SHARED_DAEMON, name, "from the context name")
    return None


def from_nodes(nodes: list[dict[str, Any]]) -> LocalCluster | None:
    """The dev cluster the node objects identify, or ``None`` for any other cluster."""
    for node in nodes:
        meta = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        spec = node.get("spec") if isinstance(node.get("spec"), dict) else {}
        labels = meta.get("labels") if isinstance(meta.get("labels"), dict) else {}
        node_name = str(meta.get("name") or "")
        provider_id = str(spec.get("providerID") or "")
        if provider_id.startswith("kind://"):
            # kind://<provider>/<cluster>/<node>
            parts = provider_id[len("kind://") :].split("/")
            if len(parts) >= 3 and parts[1]:
                return LocalCluster(KIND, parts[1], f"node providerID {provider_id}")
        if labels.get("minikube.k8s.io/name"):
            profile = str(labels["minikube.k8s.io/name"])
            return LocalCluster(MINIKUBE, profile, f"node label minikube.k8s.io/name={profile}")
        if node_name in _SHARED_DAEMON_NODES or "rancher-desktop" in node_name:
            return LocalCluster(SHARED_DAEMON, node_name, f"node {node_name}")
        if provider_id.startswith("k3s://"):
            match = _K3D_NODE.match(node_name)
            if match:
                return LocalCluster(K3D, match.group("cluster"), f"node {node_name}")
            return LocalCluster(K3S, "", f"node providerID {provider_id}")
    return None


def read_nodes(context: str | None) -> list[dict[str, Any]] | None:
    """The cluster's Node objects, ``None`` when they cannot be read (never raises)."""
    target = _kube.Target(context=context, namespace="")
    try:
        result = _kube.kubectl(
            ["get", "nodes", "-o", "json", f"--request-timeout={NODE_READ_TIMEOUT}"],
            target,
            namespaced=False,
            check=False,
            quiet=True,
        )
    except _kube.ToolFailed:
        return None
    if result.returncode != 0 or not (result.stdout or "").strip():
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        return None
    return [item for item in items if isinstance(item, dict)]


def _tool_output(cmd: list[str]) -> str | None:
    """stdout of a local CLI, ``None`` when it is missing or fails (never raises)."""
    try:
        result = _kube.run_cmd(cmd, check=False, quiet=True)
    except _kube.ToolFailed:
        return None
    if result.returncode != 0:
        return None
    return result.stdout or ""


def kind_clusters() -> set[str] | None:
    """Names ``kind get clusters`` reports on this machine, ``None`` when kind is unavailable."""
    out = _tool_output(["kind", "get", "clusters"])
    if out is None:
        return None
    return {line.strip() for line in out.splitlines() if line.strip()}


def _json_or_none(text: str | None) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text or "null")
    except json.JSONDecodeError:
        return None


def k3d_clusters() -> set[str] | None:
    """Names ``k3d cluster list`` reports on this machine, ``None`` when k3d is unavailable."""
    data = _json_or_none(_tool_output(["k3d", "cluster", "list", "-o", "json"]))
    if not isinstance(data, list):
        return None
    return {str(c.get("name")) for c in data if isinstance(c, dict) and c.get("name")}


def minikube_profiles() -> set[str] | None:
    """Valid profiles ``minikube profile list`` reports, ``None`` when minikube is unavailable."""
    data = _json_or_none(_tool_output(["minikube", "profile", "list", "-o", "json"]))
    valid = data.get("valid") if isinstance(data, dict) else None
    if not isinstance(valid, list):
        return None
    return {str(p.get("Name")) for p in valid if isinstance(p, dict) and p.get("Name")}


def _local_hostnames() -> set[str]:
    import socket

    name = socket.gethostname().lower()
    return {name, name.split(".")[0]}


_LISTINGS = {
    KIND: ("kind get clusters", kind_clusters),
    K3D: ("k3d cluster list", k3d_clusters),
    MINIKUBE: ("minikube profile list", minikube_profiles),
}


def _verify(
    cluster: LocalCluster, nodes: list[dict[str, Any]] | None
) -> tuple[LocalCluster | None, str]:
    """Confirm that ``cluster`` runs on this machine, where its load command can reach it."""
    if cluster.kind in _LISTINGS:
        command, listing = _LISTINGS[cluster.kind]
        names = listing()
        if names is None:
            return None, (
                f"{cluster.describe()}, but `{command}` is not available on this machine to "
                "load images into it"
            )
        if cluster.name not in names:
            return None, (
                f"{cluster.describe()}, but `{command}` on this machine does not list "
                f"{cluster.name!r}"
            )
        evidence = f"{cluster.evidence}; `{command}` lists it"
        return LocalCluster(cluster.kind, cluster.name, evidence), ""
    if cluster.kind == K3S and nodes is not None:
        # k3s also runs production clusters: only this machine's own node can import images.
        node_names = {
            str(n["metadata"].get("name") or "").lower()
            for n in nodes
            if isinstance(n.get("metadata"), dict)
        }
        if not node_names & _local_hostnames():
            return None, f"{cluster.describe()}, but none of its nodes is this machine"
    return cluster, ""


def detect(context: str | None) -> tuple[LocalCluster | None, str]:
    """Identify a local dev cluster behind ``context``: ``(cluster or None, reason)``.

    ``reason`` explains, in one line, why the cluster is not treated as local.
    """
    nodes = read_nodes(context)
    if nodes is not None:
        cluster = from_nodes(nodes)
        if cluster is None:
            return None, "its nodes are not a local dev cluster"
    else:
        cluster = from_context_name(context)
        if cluster is None:
            return None, "its nodes cannot be read and the context name is not a dev cluster"
    return _verify(cluster, nodes)


def local_load_commands(
    cluster: LocalCluster, image: str, *, tar_path: Path | None = None
) -> list[list[str]]:
    """The commands that make ``image`` visible to ``cluster``.

    An empty list means the cluster already sees the docker daemon's images.
    """
    if cluster.kind == KIND:
        return [["kind", "load", "docker-image", image, "--name", cluster.name]]
    if cluster.kind == K3D:
        return [["k3d", "image", "import", image, "-c", cluster.name]]
    if cluster.kind == MINIKUBE:
        if cluster.name and cluster.name != "minikube":
            return [["minikube", "image", "load", image, "-p", cluster.name]]
        return [["minikube", "image", "load", image]]
    if cluster.kind == K3S:
        tar = str(tar_path or Path(".graph-agents-cli") / "image.tar")
        return [["docker", "save", "-o", tar, image], ["k3s", "ctr", "images", "import", tar]]
    return []
