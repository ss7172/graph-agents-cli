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

"""Where things are: the checkout, the benchmark data, and a run's scratch layout."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parents[1]  # tools/skillopt
REPO = TOOL_DIR.parents[1]
SKILLS_DIR = REPO / "skills"
TASKS_DIR = TOOL_DIR / "tasks"
SPLITS_DIR = TOOL_DIR / "splits"
CONFIGS_DIR = TOOL_DIR / "configs"

SKILLS = (
    "graph-agents-cli-workflow",
    "graph-agents-cli-scaffold",
    "graph-agents-cli-langgraph-code",
    "graph-agents-cli-eval",
    "graph-agents-cli-deploy",
    "graph-agents-cli-observability",
)
# Task directories are named by the skill's short name (tasks/eval/..., tasks/deploy/...).
SHORT = {name: name.removeprefix("graph-agents-cli-") for name in SKILLS}
LONG = {short: name for name, short in SHORT.items()}

# Rollout workspaces. Claude Code's Bash sandbox always lets commands write under its own temp
# root (/private/tmp/claude-<uid>), which may hold the caller's scratch (ledger, other runs), so
# workspaces live outside it and the scratch root is write-denied (DESIGN section 3.1).
DEFAULT_WORKSPACE_ROOT = Path("/private/tmp/gac-x-skillopt")


@dataclass
class Bench:
    """A benchmark installation: scratch directories, the CLI build and the rollout ports.

    ``scratch`` holds everything that outlives one rollout (the CLI build, the warm uv cache,
    vendored chart dependencies, run outputs); it must be outside the checkout.
    """

    scratch: Path
    workspace_root: Path = DEFAULT_WORKSPACE_ROOT
    port_base: int = 22050
    # Extra paths rollouts may not write (on top of ``scratch``), e.g. the session scratchpad
    # that contains it. GAC_SKILLOPT_DENY_WRITE: os.pathsep-separated.
    deny_write: tuple[Path, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        self.scratch = Path(self.scratch).resolve()
        if self.scratch == REPO or REPO in self.scratch.parents:
            raise SystemExit(f"scratch {self.scratch} is inside the checkout; use a scratch dir")
        extra = os.environ.get("GAC_SKILLOPT_DENY_WRITE", "")
        paths = [Path(p).resolve() for p in extra.split(os.pathsep) if p.strip()]
        # A scratch inside a Claude Code session's temp root (/private/tmp/claude-<uid>/<project>/
        # <session>/scratchpad/...) sits beside that session's other files (a spend ledger,
        # other runs): rollouts may not write anywhere under the project's temp directory. The
        # rollouts' own Claude Code keeps its temp files under another project directory there.
        parts = self.scratch.parts
        if len(parts) > 4 and parts[1:3] == ("private", "tmp") and parts[3].startswith("claude-"):
            paths.append(Path(*parts[:5]))
        self.deny_write = tuple(dict.fromkeys([*self.deny_write, *paths, self.scratch]))
        if self.workspace_root in self.deny_write or any(
            p in self.workspace_root.parents for p in self.deny_write
        ):
            raise SystemExit(f"workspace root {self.workspace_root} is inside a write-denied path")

    @classmethod
    def from_env(cls, scratch: str | Path | None = None, **kw: object) -> Bench:
        """``GAC_SKILLOPT_PORT_BASE`` sets the port base when ``port_base`` is not given: slot
        ``i`` uses ``base+i`` and ``base+25+i``, so concurrent runs need disjoint ranges."""
        value = scratch or os.environ.get("GAC_SKILLOPT_SCRATCH")
        if not value:
            raise SystemExit("set --scratch or GAC_SKILLOPT_SCRATCH to a scratch directory")
        if kw.get("port_base") is None:
            kw.pop("port_base", None)
            if os.environ.get("GAC_SKILLOPT_PORT_BASE"):
                kw["port_base"] = int(os.environ["GAC_SKILLOPT_PORT_BASE"])
        return cls(Path(value), **kw)  # type: ignore[arg-type]

    # Layout under ``scratch``.
    @property
    def bin(self) -> Path:
        return self.scratch / "bin"

    @property
    def uv_tools(self) -> Path:
        return self.scratch / "uv-tools"

    @property
    def uv_venv(self) -> Path:
        return self.scratch / "uv-new"

    @property
    def uv_cache(self) -> Path:
        return self.scratch / "uv-cache"

    @property
    def chart_cache(self) -> Path:
        return self.scratch / "chart-deps"

    @property
    def runs(self) -> Path:
        return self.scratch / "runs"

    @property
    def kubeconfig(self) -> Path:
        return self.scratch / "kubeconfig-empty"

    @property
    def cli_real(self) -> Path:
        return self.uv_tools / "graph-agents-cli" / "bin" / "graph-agents-cli"

    @property
    def cli_python(self) -> Path:
        return self.uv_tools / "graph-agents-cli" / "bin" / "python"

    def agent_port(self, slot: int) -> int:
        return self.port_base + slot

    def verifier_port(self, slot: int) -> int:
        return self.port_base + 25 + slot
