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

"""Merge engine: preservation categories, three_way_compare, and the authentic-baseline stop."""

from __future__ import annotations

import pathlib
import subprocess

import pytest
from click.testing import CliRunner

from graph_agents_cli.scaffold.commands.upgrade import upgrade
from graph_agents_cli.scaffold.utils import merge
from graph_agents_cli.scaffold.utils import upgrade as upgrade_utils
from graph_agents_cli.scaffold.utils.upgrade import (
    FILE_CATEGORIES,
    categorize_file,
    three_way_compare,
)

from .conftest import CreateRunner, read_manifest


@pytest.mark.parametrize(
    ("path", "category"),
    [
        ("app/agent.py", "agent_code"),
        ("app/tools/search.py", "agent_code"),
        ("app/tools/nested/deep.py", "agent_code"),
        ("app/policies/custom.py", "agent_code"),
        ("app/prompts/system.md", "agent_code"),
        ("app/graph/nodes.py", "agent_code"),
        (".env", "config_files"),
        (".env.dev", "config_files"),
        ("api-policy.yaml", "config_files"),
        ("deployment/helm/my-agent/values-dev.yaml", "config_files"),
        ("deployment/helm/my-agent/values-prod.yaml", "config_files"),
        ("deployment/argocd/application-prod.yaml", "config_files"),
        ("tests/eval/datasets/basic-dataset.json", "config_files"),
        ("tests/eval/eval_config.yaml", "config_files"),
        ("pyproject.toml", "dependencies"),
        ("graph-agents-cli-manifest.yaml", "dependencies"),
        # scaffolding: everything else
        ("deployment/helm/my-agent/values.yaml", "scaffolding"),
        ("deployment/helm/my-agent/templates/deployment.yaml", "scaffolding"),
        ("app/app_utils/auth.py", "scaffolding"),
        ("app/fast_api_app.py", "scaffolding"),
        ("Dockerfile", "scaffolding"),
        (".github/workflows/pr_checks.yaml", "scaffolding"),
        ("README.md", "scaffolding"),
    ],
)
def test_categorize_file_d26(path: str, category: str) -> None:
    assert categorize_file(path, "app") == category


def test_categorize_uses_agent_directory() -> None:
    assert categorize_file("bot/agent.py", "bot") == "agent_code"
    assert categorize_file("app/agent.py", "bot") == "scaffolding"
    assert set(FILE_CATEGORIES) == {"agent_code", "config_files", "dependencies"}


def _write(root: pathlib.Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_three_way_compare_actions(tmp_path: pathlib.Path) -> None:
    project, old, new = tmp_path / "project", tmp_path / "old", tmp_path / "new"
    for root in (project, old, new):
        root.mkdir()

    # unchanged by the user, changed by the template -> auto_update
    _write(project, "Dockerfile", "v1")
    _write(old, "Dockerfile", "v1")
    _write(new, "Dockerfile", "v2")
    # changed by the user, unchanged by the template -> preserve
    _write(project, "README.md", "mine")
    _write(old, "README.md", "tpl")
    _write(new, "README.md", "tpl")
    # both changed -> conflict
    _write(project, "app/fast_api_app.py", "mine")
    _write(old, "app/fast_api_app.py", "v1")
    _write(new, "app/fast_api_app.py", "v2")
    # new in template
    _write(new, "app/app_utils/telemetry.py", "new")
    # removed in template, untouched by user
    _write(project, "old_script.py", "x")
    _write(old, "old_script.py", "x")
    # removed in template but modified by user -> conflict
    _write(project, "changed_then_removed.py", "mine")
    _write(old, "changed_then_removed.py", "x")
    # user-added
    _write(project, "notes.md", "n")
    # already current
    _write(project, "current.py", "v2")
    _write(old, "current.py", "v1")
    _write(new, "current.py", "v2")
    # agent code and config are never touched, whatever the hashes say
    _write(project, "app/agent.py", "mine")
    _write(old, "app/agent.py", "v1")
    _write(new, "app/agent.py", "v2")
    _write(project, ".env", "SECRET=1")
    _write(new, ".env", "SECRET=")
    _write(project, "pyproject.toml", "a")
    _write(old, "pyproject.toml", "b")
    _write(new, "pyproject.toml", "c")

    def cmp(rel: str):
        return three_way_compare(rel, project, old, new, "app")

    assert cmp("Dockerfile").action == "auto_update"
    r = cmp("README.md")
    assert (r.action, r.preserve_type) == ("preserve", "gacli_unchanged")
    assert cmp("app/fast_api_app.py").action == "conflict"
    assert cmp("app/app_utils/telemetry.py").action == "new"
    assert cmp("old_script.py").action == "removed"
    assert cmp("changed_then_removed.py").action == "conflict"
    assert cmp("notes.md").action == "skip"
    r = cmp("current.py")
    assert (r.action, r.preserve_type) == ("preserve", "already_current")
    r = cmp("app/agent.py")
    assert (r.action, r.category) == ("skip", "agent_code")
    r = cmp(".env")
    assert (r.action, r.category) == ("skip", "config_files")
    r = cmp("pyproject.toml")
    assert (r.action, r.category) == ("preserve", "dependencies")
    assert cmp("missing.py").action == "skip"


def test_three_way_compare_never_resurrects_deleted_agent_code_or_config(
    tmp_path: pathlib.Path,
) -> None:
    project, old, new = tmp_path / "project", tmp_path / "old", tmp_path / "new"
    for root in (project, old, new):
        root.mkdir()
    # Shipped by both snapshots, deleted by the developer -> never re-added.
    for rel in ("app/tools/weather.py", "tests/eval/datasets/basic.json"):
        _write(old, rel, "shipped")
        _write(new, rel, "shipped")
    # Genuinely new in the template (absent from the old snapshot) -> added once.
    for rel in ("deployment/argocd/application-dev.yaml", "app/policies/new_policy.py"):
        _write(new, rel, "new")

    def cmp(rel: str):
        return three_way_compare(rel, project, old, new, "app")

    for rel in ("app/tools/weather.py", "tests/eval/datasets/basic.json"):
        r = cmp(rel)
        assert r.action == "skip", rel
        assert "Removed by you" in r.reason
    for rel in ("deployment/argocd/application-dev.yaml", "app/policies/new_policy.py"):
        assert cmp(rel).action == "new", rel


def test_enhance_does_not_restore_a_deleted_sample_tool(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from graph_agents_cli.scaffold.commands.enhance import enhance

    result, project = run_create("--cd", "skip")
    assert result.exit_code == 0, result.output
    (project / "app" / "agent.py").unlink()
    monkeypatch.chdir(project)
    result = CliRunner().invoke(
        enhance, ["--cd", "helm-push", "-y", "--skip-checks"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    assert not (project / "app" / "agent.py").exists()
    assert (project / ".github" / "workflows" / "staging.yaml").is_file()


def test_python_dependency_merge(tmp_path: pathlib.Path) -> None:
    project, old, new = tmp_path / "project", tmp_path / "old", tmp_path / "new"
    for root in (project, old, new):
        root.mkdir()
    _write(
        project,
        "pyproject.toml",
        '[project]\ndependencies = ["langgraph>=1.0", "mine==1", "removed>=1"]\n',
    )
    _write(old, "pyproject.toml", '[project]\ndependencies = ["langgraph>=1.0", "removed>=1"]\n')
    _write(new, "pyproject.toml", '[project]\ndependencies = ["langgraph>=1.1", "added[x]>=2"]\n')
    by_name = {r.name: r for r in upgrade_utils.merge_python_dependencies(project, old, new)}
    assert by_name["langgraph"].status == "updated"
    assert by_name["langgraph"].new_version == ">=1.1"
    assert by_name["added"].status == "added"
    assert by_name["added"].full_name() == "added[x]>=2"
    assert by_name["mine"].status == "kept"
    assert by_name["removed"].status == "removed"


# --- run_create_command: authentic baseline or stop ------------------------


@pytest.fixture
def vendored_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_vendored(args, output_dir, project_name):
        calls.append(list(args))
        return True

    monkeypatch.setattr(merge, "_run_vendored_create", fake_vendored)
    return calls


@pytest.fixture
def uvx_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """uvx fails (index unreachable / version absent); records every attempt."""
    import graph_agents_cli._runner as runner

    calls: list[list[str]] = []

    def fake_run_resolved(args, *, resolve_executable=True, **kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="No solution found")

    monkeypatch.setattr(runner, "run_resolved", fake_run_resolved)
    return calls


def test_current_version_renders_vendored(vendored_calls, uvx_calls, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(merge, "get_current_version", lambda: "0.1.0", raising=False)
    from graph_agents_cli.scaffold.utils import version as version_module

    monkeypatch.setattr(version_module, "get_current_version", lambda: "0.1.0")
    assert merge.run_create_command(["--cd", "skip"], tmp_path, "p", None) is True
    assert merge.run_create_command(["--cd", "skip"], tmp_path, "p", "0.1.0") is True
    assert len(vendored_calls) == 2
    assert uvx_calls == []


def test_authentic_baseline_stops_when_uvx_fails(
    vendored_calls, uvx_calls, monkeypatch, tmp_path, caplog
) -> None:
    from graph_agents_cli.scaffold.utils import version as version_module

    monkeypatch.setattr(version_module, "get_current_version", lambda: "0.2.0")
    monkeypatch.delenv(version_module.INSTALL_SPEC_ENV, raising=False)
    ok = merge.run_create_command(["--cd", "skip"], tmp_path, "p", "0.1.0")
    assert ok is False
    assert vendored_calls == []  # no silent fallback to the current templates
    assert len(uvx_calls) == 1
    assert uvx_calls[0][:5] == [
        "uvx",
        "--from",
        "git+https://github.com/ss7172/graph-agents-cli@v0.1.0",
        "graph-agents-cli",
        "scaffold",
    ]
    assert "--baseline current" in caplog.text


def test_authentic_baseline_is_not_rendered_by_a_fixed_override(
    vendored_calls, uvx_calls, monkeypatch, tmp_path, caplog
) -> None:
    """An override without {version} would render some other build as the old snapshot."""
    from graph_agents_cli.scaffold.utils import version as version_module

    monkeypatch.setattr(version_module, "get_current_version", lambda: "0.2.0")
    monkeypatch.setenv(version_module.INSTALL_SPEC_ENV, "/srv/mirror/graph_agents_cli.whl")
    assert merge.run_create_command(["--cd", "skip"], tmp_path, "p", "0.1.0") is False
    assert vendored_calls == [] and uvx_calls == []
    assert "{version}" in caplog.text and "--baseline current" in caplog.text

    # With the placeholder the override is used, pinned to the old version.
    monkeypatch.setenv(version_module.INSTALL_SPEC_ENV, "git+https://git.example/gac@v{version}")
    assert merge.run_create_command(["--cd", "skip"], tmp_path, "p", "0.1.0") is False
    assert uvx_calls[0][:3] == ["uvx", "--from", "git+https://git.example/gac@v0.1.0"]


def test_authentic_baseline_stops_on_unfetchable_version(
    vendored_calls, uvx_calls, monkeypatch, tmp_path, caplog
) -> None:
    from graph_agents_cli.scaffold.utils import version as version_module

    monkeypatch.setattr(version_module, "get_current_version", lambda: "0.2.0")
    assert merge.run_create_command([], tmp_path, "p", "0.0.0") is False
    assert merge.run_create_command([], tmp_path, "p", "not-a-version") is False
    assert vendored_calls == []
    assert uvx_calls == []
    assert "--baseline current" in caplog.text


def test_baseline_current_is_explicit_opt_in(
    vendored_calls, uvx_calls, monkeypatch, tmp_path, caplog
) -> None:
    from graph_agents_cli.scaffold.utils import version as version_module

    monkeypatch.setattr(version_module, "get_current_version", lambda: "0.2.0")
    ok = merge.run_create_command(["--cd", "skip"], tmp_path, "p", "0.1.0", baseline="current")
    assert ok is True
    assert vendored_calls == [["--cd", "skip"]]
    assert uvx_calls == []
    assert "Baseline override" in caplog.text


@pytest.mark.parametrize("args", [["--dry-run"], ["-y"]])
def test_upgrade_outside_a_project_is_a_configuration_error(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    """Exit 3, like every other command run outside a project (it used to be 1)."""
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(upgrade, args)
    assert result.exit_code == 3, result.output
    assert "No graph-agents-cli-manifest.yaml found" in result.output


def test_upgrade_command_stops_without_authentic_baseline(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch, uvx_calls
) -> None:
    result, project = run_create()
    assert result.exit_code == 0, result.output
    manifest_path = project / "graph-agents-cli-manifest.yaml"
    manifest_path.write_text(
        manifest_path.read_text().replace("cli_version: ", "cli_version: '0.0.1' # was ")
    )
    assert read_manifest(project)["cli_version"] == "0.0.1"

    from graph_agents_cli import _tools

    monkeypatch.setattr(_tools, "require_tool", lambda name, install_hint="": "/usr/bin/uvx")
    monkeypatch.setattr(
        "graph_agents_cli.scaffold.commands.upgrade.get_current_version", lambda: "9.9.9"
    )
    from graph_agents_cli.scaffold.utils import version as version_module

    monkeypatch.setattr(version_module, "get_current_version", lambda: "9.9.9")

    before = sorted(str(p.relative_to(project)) for p in project.rglob("*"))
    result = CliRunner().invoke(upgrade, [str(project), "-y"], catch_exceptions=False)
    assert result.exit_code == 2  # uvx could not fetch and run the prior release: a tool failure
    assert "--baseline current" in result.output
    assert "not modified" in result.output
    after = sorted(str(p.relative_to(project)) for p in project.rglob("*"))
    assert before == after
    assert read_manifest(project)["cli_version"] == "0.0.1"


def _set_cli_version(project: pathlib.Path, value: str | None) -> None:
    """Replace (or, with None, drop) the manifest's cli_version line."""
    manifest_path = project / "graph-agents-cli-manifest.yaml"
    lines = manifest_path.read_text().splitlines(keepends=True)
    out = []
    for line in lines:
        if line.startswith("cli_version:"):
            if value is None:
                continue
            line = f"cli_version: '{value}'\n"
        out.append(line)
    manifest_path.write_text("".join(out))


@pytest.fixture
def newer_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """The running CLI is 9.9.9, newer than any project the tests create."""
    from graph_agents_cli.scaffold.utils import version as version_module

    monkeypatch.setattr(
        "graph_agents_cli.scaffold.commands.upgrade.get_current_version", lambda: "9.9.9"
    )
    monkeypatch.setattr(version_module, "get_current_version", lambda: "9.9.9")


def _tree(project: pathlib.Path) -> list[str]:
    return sorted(str(p.relative_to(project)) for p in project.rglob("*"))


def _uvx(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if c[0] == "uvx"]


def test_upgrade_without_cli_version_is_a_configuration_error(
    run_create: CreateRunner, newer_cli, uvx_calls
) -> None:
    result, project = run_create()
    assert result.exit_code == 0, result.output
    _set_cli_version(project, None)
    before = _tree(project)
    result = CliRunner().invoke(upgrade, [str(project), "-y"])
    assert result.exit_code == 3, result.output
    assert "No cli_version found in graph-agents-cli-manifest.yaml" in result.output
    assert _uvx(uvx_calls) == [] and _tree(project) == before


@pytest.mark.parametrize("version", ["0.0.0", "not-a-version"])
def test_upgrade_from_an_unreleased_cli_version_is_a_configuration_error(
    run_create: CreateRunner, newer_cli, uvx_calls, version: str
) -> None:
    result, project = run_create()
    assert result.exit_code == 0, result.output
    _set_cli_version(project, version)
    result = CliRunner().invoke(upgrade, [str(project), "-y"])
    assert result.exit_code == 3, result.output
    assert "is not a released graph-agents-cli version" in result.output
    assert "--baseline current" in result.output
    assert _uvx(uvx_calls) == []


def test_upgrade_with_an_install_spec_override_lacking_version_is_a_configuration_error(
    run_create: CreateRunner, newer_cli, uvx_calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, project = run_create()
    assert result.exit_code == 0, result.output
    _set_cli_version(project, "0.0.1")
    monkeypatch.setenv("GRAPH_AGENTS_CLI_INSTALL_SPEC", "git+https://git.example/gac@main")
    result = CliRunner().invoke(upgrade, [str(project), "-y"])
    assert result.exit_code == 3, result.output
    assert "{version}" in result.output and "--baseline current" in result.output
    assert _uvx(uvx_calls) == []


def test_upgrade_without_uvx_is_a_tool_failure(
    run_create: CreateRunner, newer_cli, uvx_calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graph_agents_cli import _tools

    result, project = run_create()
    assert result.exit_code == 0, result.output
    _set_cli_version(project, "0.0.1")

    def missing(name, install_hint=""):
        raise _tools.ToolNotFoundError(f"{name} not found")

    monkeypatch.setattr(_tools, "require_tool", missing)
    before = _tree(project)
    result = CliRunner().invoke(upgrade, [str(project), "-y"])
    assert result.exit_code == 2, result.output
    assert "needs uvx" in result.output
    assert _uvx(uvx_calls) == [] and _tree(project) == before


def test_baseline_current_says_what_the_preserve_list_holds(
    run_create: CreateRunner, newer_cli, uvx_calls
) -> None:
    """Under --baseline current a file that differs from the current template may be
    one the template changed since the old version, not an edit: the list says so."""
    result, project = run_create()
    assert result.exit_code == 0, result.output
    _set_cli_version(project, "0.0.1")
    readme = project / "README.md"
    readme.write_text(readme.read_text() + "\nAs rendered by an older release.\n")
    result = CliRunner().invoke(
        upgrade, [str(project), "--dry-run", "--baseline", "current"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    output = " ".join(result.output.split())
    assert "Will preserve (differs from the current template):" in output
    assert "cannot tell your edits from template changes since 0.0.1" in output
    assert "keeps its 0.0.1 content" in output
    assert "dependency changes since then are not merged" in output
    assert "you modified, template unchanged" not in output
    assert "README.md" in output


def test_upgrade_command_baseline_current_is_labelled(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch, uvx_calls
) -> None:
    result, project = run_create()
    assert result.exit_code == 0, result.output
    manifest_path = project / "graph-agents-cli-manifest.yaml"
    manifest_path.write_text(
        manifest_path.read_text().replace("cli_version: ", "cli_version: '0.0.1' # was ")
    )

    monkeypatch.setattr(
        "graph_agents_cli.scaffold.commands.upgrade.get_current_version", lambda: "9.9.9"
    )
    from graph_agents_cli.scaffold.utils import version as version_module

    monkeypatch.setattr(version_module, "get_current_version", lambda: "9.9.9")

    result = CliRunner().invoke(
        upgrade, [str(project), "-y", "--baseline", "current"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    assert "--baseline current" in result.output
    assert [c for c in uvx_calls if c[0] == "uvx"] == []  # never tried to fetch the prior release
    assert "baseline: current templates" in result.output
    # Both snapshots render identically from the current templates: nothing to do,
    # so the version stamp is not rewritten.
    assert "No changes needed" in result.output
