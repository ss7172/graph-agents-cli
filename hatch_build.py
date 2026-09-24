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

"""Hatch build hook: record which source a wheel or sdist was built from.

Builds between two releases share the version in ``pyproject.toml``. This hook
writes ``graph_agents_cli/_build_info.json`` (the git commit, uncommitted
changes, the release tag) into every wheel built from a git checkout, so
``graph-agents-cli --version`` and the project manifest can tell those builds
apart (see ``src/graph_agents_cli/_build.py``, which computes the facts).

An sdist built from a checkout carries the file under ``src/``; a wheel built
from that sdist (no git there) keeps it. An editable install writes nothing:
it reads git at run time. A tree without git builds a wheel without the file,
which identifies itself by its version only.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_BUILD_MODULE = Path("src") / "graph_agents_cli" / "_build.py"
_WHEEL_PATH = "graph_agents_cli/_build_info.json"
_SDIST_PATH = "src/graph_agents_cli/_build_info.json"


def _load_build_module(root: Path) -> Any:
    spec = importlib.util.spec_from_file_location("_graph_agents_cli_build", root / _BUILD_MODULE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {_BUILD_MODULE}")
    module = importlib.util.module_from_spec(spec)
    # Registered first: dataclasses look their module up in sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class CustomBuildHook(BuildHookInterface):
    """Adds ``_build_info.json`` to the wheel and the sdist (see the module docstring)."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if version == "editable":
            return
        root = Path(self.root)
        facts = _load_build_module(root).git_facts(root, self.metadata.version)
        if facts is None:
            return  # no git: an sdist's own file (if any) is packaged as it is
        out_dir = Path(tempfile.mkdtemp(prefix="gacli-build-info-"))
        self._out_dir = out_dir
        info = out_dir / "_build_info.json"
        info.write_text(json.dumps(facts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        target = _SDIST_PATH if self.target_name == "sdist" else _WHEEL_PATH
        build_data["force_include"][str(info)] = target

    def finalize(self, version: str, build_data: dict[str, Any], artifact_path: str) -> None:
        out_dir = getattr(self, "_out_dir", None)
        if out_dir is None:
            return
        for child in out_dir.iterdir():
            child.unlink()
        os.rmdir(out_dir)
