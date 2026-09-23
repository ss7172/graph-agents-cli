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

"""Tests for graph_agents_cli._tools: hints, caching, Windows PATH handling."""

from __future__ import annotations

import os
import shutil

import pytest

from graph_agents_cli import _tools
from graph_agents_cli._tools import (
    DEFAULT_INSTALL_HINTS,
    DEFAULT_SKILLS_SOURCE,
    ToolNotFoundError,
    install_hint,
    require_tool,
    run_npx_skills,
    tool_available,
)
from graph_agents_cli.extension._refs import FIRST_PARTY_REPO


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_tools, "_tool_paths", {})
    _tools._get_cleaned_path.cache_clear()
    yield
    _tools._get_cleaned_path.cache_clear()


EXPECTED_TOOLS = (
    "helm",
    "kubectl",
    "docker",
    "argocd",
    "gh",
    "uv",
    "uvx",
    "npx",
    "node",
    "git",
    "langgraph",
)


@pytest.mark.parametrize("name", EXPECTED_TOOLS)
def test_hint_exists_for_every_tool(name: str):
    assert DEFAULT_INSTALL_HINTS[name]
    assert install_hint(name) == DEFAULT_INSTALL_HINTS[name]


def test_no_gcp_hints_remain():
    assert "gcloud" not in DEFAULT_INSTALL_HINTS
    assert "terraform" not in DEFAULT_INSTALL_HINTS
    assert not hasattr(_tools, "_get_gcloud_fallback")
    assert "gcloud" not in open(_tools.__file__, encoding="utf-8").read()


def test_langgraph_hint_points_at_uv_run():
    assert "uv run langgraph" in DEFAULT_INSTALL_HINTS["langgraph"]


def test_skills_source_placeholder_is_single_constant():
    assert DEFAULT_SKILLS_SOURCE == "https://github.com/ss7172/graph-agents-cli"
    assert FIRST_PARTY_REPO == "ss7172/graph-agents-cli"
    assert DEFAULT_SKILLS_SOURCE.endswith(FIRST_PARTY_REPO)


def test_require_tool_missing_uses_default_hint(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(shutil, "which", lambda name, path=None: None)
    with pytest.raises(ToolNotFoundError) as exc:
        require_tool("helm")
    assert "'helm' is not installed" in str(exc.value)
    assert "https://helm.sh" in str(exc.value)


def test_require_tool_explicit_hint_wins(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(shutil, "which", lambda name, path=None: None)
    with pytest.raises(ToolNotFoundError) as exc:
        require_tool("kubectl", "use the cluster bundle")
    assert "use the cluster bundle" in str(exc.value)
    assert "kubernetes.io" not in str(exc.value)


def test_require_tool_unknown_tool_has_no_hint(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(shutil, "which", lambda name, path=None: None)
    with pytest.raises(ToolNotFoundError) as exc:
        require_tool("frobnicate")
    assert str(exc.value) == "'frobnicate' is not installed or not on PATH."


def test_require_tool_caches(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []

    def fake_which(name, path=None):
        calls.append(name)
        return f"/usr/bin/{name}"

    monkeypatch.setattr(shutil, "which", fake_which)
    assert require_tool("docker") == "/usr/bin/docker"
    assert require_tool("docker") == "/usr/bin/docker"
    assert calls == ["docker"]
    assert tool_available("docker")


def test_tool_available_false_when_missing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(shutil, "which", lambda name, path=None: None)
    assert tool_available("argocd") is False


def test_windows_retries_with_cleaned_path(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_tools, "is_windows", lambda: True)
    monkeypatch.setenv("TOOLHOME", "/opt/tools")
    monkeypatch.setenv("PATH", os.pathsep.join(['"/quoted/bin"', "", "$TOOLHOME/bin"]))
    seen: list[str | None] = []

    def fake_which(name, path=None):
        seen.append(path)
        return "/opt/tools/bin/gh" if path else None

    monkeypatch.setattr(shutil, "which", fake_which)
    assert require_tool("gh") == "/opt/tools/bin/gh"
    assert seen[0] is None
    assert seen[1] == os.pathsep.join(["/quoted/bin", "/opt/tools/bin"])


def test_run_npx_skills_wraps_failure(monkeypatch: pytest.MonkeyPatch):
    import click

    def boom(args, **kwargs):
        raise ToolNotFoundError("'npx' is not installed or not on PATH.")

    monkeypatch.setattr("graph_agents_cli._runner.run_resolved", boom)
    with pytest.raises(click.ClickException, match="Error running npx skills"):
        run_npx_skills(["list"], "Listing")


def test_run_npx_skills_pins_package(monkeypatch: pytest.MonkeyPatch):
    from graph_agents_cli._skills_check import SKILLS_NPX_PACKAGE

    seen: list[list[str]] = []
    monkeypatch.setattr(
        "graph_agents_cli._runner.run_resolved", lambda args, **kw: seen.append(list(args))
    )
    run_npx_skills(["add", "x", "-y"], "Installing")
    assert seen == [["npx", "-y", SKILLS_NPX_PACKAGE, "add", "x", "-y"]]
