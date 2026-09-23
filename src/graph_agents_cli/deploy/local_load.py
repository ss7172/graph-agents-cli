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
"""Load a locally built image into a single-node dev cluster (ASSUMPTIONS item 10).

The mechanism is chosen from the kube context name. Only the ``docker`` CLI is
supported for builds; Docker Desktop, Rancher Desktop and OrbStack share the
daemon with the cluster, so nothing needs loading there.
"""

from __future__ import annotations

from pathlib import Path

from graph_agents_cli.deploy._kube import ConfigError


def local_load_commands(
    context: str, image: str, *, tar_path: Path | None = None
) -> list[list[str]]:
    """The commands that make ``image`` visible to the cluster behind ``context``.

    An empty list means the cluster already sees the docker daemon's images.
    """
    name = context.strip()
    if name.startswith("kind-"):
        return [["kind", "load", "docker-image", image, "--name", name[len("kind-") :]]]
    if name.startswith("k3d-"):
        return [["k3d", "image", "import", image, "-c", name[len("k3d-") :]]]
    if name == "minikube":
        return [["minikube", "image", "load", image]]
    if name.startswith("minikube"):
        return [["minikube", "image", "load", image, "-p", name]]
    if name.startswith("k3s"):
        tar = str(tar_path or Path(".graph-agents-cli") / "image.tar")
        return [["docker", "save", "-o", tar, image], ["k3s", "ctr", "images", "import", tar]]
    if name in ("docker-desktop", "rancher-desktop", "orbstack"):
        return []
    raise ConfigError(
        f"Cannot load images into kube context {context!r}: it is not a recognised dev cluster.\n"
        "  Supported: kind-*, k3d-*, minikube, k3s, docker-desktop, rancher-desktop, orbstack."
    )
