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

"""Builds between releases: the manifest records the build, upgrade compares it.

A release and the builds made before the next one share a version, so
``cli_version`` alone told ``scaffold upgrade`` that a project made by an
earlier build of the same version was up to date. The manifest now records the
build (``cli_build``: id, commit, a digest of the snapshot it renders) and
``--baseline-ref`` names the build of a project that predates the record.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
from collections.abc import Callable

import pytest
from click.testing import CliRunner

from graph_agents_cli import _build, _tools
from graph_agents_cli.scaffold.commands.enhance import enhance
from graph_agents_cli.scaffold.commands.upgrade import upgrade
from graph_agents_cli.scaffold.utils import build_record, merge
from graph_agents_cli.scaffold.utils import version as version_module
from graph_agents_cli.scaffold.utils.build_record import BuildRecord, template_digest
from graph_agents_cli.scaffold.utils.version import InstallSpecError, resolve_baseline_ref

from .conftest import TEST_BUILD_COMMIT, CreateRunner, read_manifest

MANIFEST = "graph-agents-cli-manifest.yaml"
REPO = "https://github.com/ss7172/graph-agents-cli"
OTHER_COMMIT = "d99c8160" + "1" * 32
OTHER_DIGEST = "sha256:" + "ab" * 32


@pytest.fixture(autouse=True)
def version_0_2_0(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every project and the running CLI are 0.2.0 unless a test says otherwise."""
    monkeypatch.setattr(version_module, "get_current_version", lambda: "0.2.0")
    monkeypatch.setattr(
        "graph_agents_cli.scaffold.commands.upgrade.get_current_version", lambda: "0.2.0"
    )
    monkeypatch.delenv(version_module.INSTALL_SPEC_ENV, raising=False)


RUNNING_ID = f"0.2.0+g{TEST_BUILD_COMMIT[:7]}"


class FakeUvx:
    """``uvx ... scaffold create`` rendered in process, as the named build would.

    ``render`` changes the snapshot after the render (what the old build's
    templates had); ``fail`` makes uvx fail like an unreachable ref.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.render: Callable[[pathlib.Path], None] | None = None
        self.fail = False

    def __call__(self, args, *, resolve_executable=True, **kwargs):
        if args[:3] == ["git", "remote", "get-url"]:
            return subprocess.CompletedProcess(args, 128, stdout="", stderr="fatal: No such remote")
        if args[:1] == ["uv"] and "--frozen" in args:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:1] != ["uvx"]:
            raise AssertionError(f"unexpected subprocess in test: {args}")
        self.calls.append(list(args))
        if self.fail:
            return subprocess.CompletedProcess(
                args, 1, stdout="", stderr="error: Git operation failed\n  commit not found"
            )
        name = args[args.index("create") + 1]
        out = pathlib.Path(args[args.index("--output-dir") + 1])
        rest = args[args.index("--skip-checks") + 1 :]
        assert merge._run_vendored_create(rest, out, name)
        if self.render is not None:
            self.render(out / name)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def specs(self) -> list[str]:
        return [c[c.index("--from") + 1] for c in self.calls]


@pytest.fixture
def uvx(monkeypatch: pytest.MonkeyPatch) -> FakeUvx:
    import graph_agents_cli._runner as runner

    fake = FakeUvx()
    monkeypatch.setattr(runner, "run_resolved", fake)
    monkeypatch.setattr(_tools, "require_tool", lambda name, install_hint="": f"/usr/bin/{name}")
    return fake


def _project(run_create: CreateRunner, *args: str, name: str = "my-agent") -> pathlib.Path:
    result, project = run_create(*args, name=name)
    assert result.exit_code == 0, result.output
    return project


def _upgrade(project: pathlib.Path, *args: str):
    return CliRunner().invoke(upgrade, [str(project), *args], catch_exceptions=False)


def _snapshot(project: pathlib.Path) -> dict[str, bytes]:
    return {
        p.relative_to(project).as_posix(): p.read_bytes()
        for p in sorted(project.rglob("*"))
        if p.is_file()
    }


def _old_build(project: pathlib.Path) -> None:
    """What an earlier build rendered: another README.md, no cli_build record."""
    (project / "README.md").write_text("# OLD_TEMPLATE\n")
    build_record.write_build_record(project, None)


# --- create records the build ---------------------------------------------------


def test_create_records_the_running_build_and_the_digest_of_its_render(
    run_create: CreateRunner,
) -> None:
    project = _project(run_create)
    manifest = read_manifest(project)
    assert manifest["cli_build"] == {
        "id": RUNNING_ID,
        "commit": TEST_BUILD_COMMIT,
        "template_digest": template_digest(project),
    }
    text = (project / MANIFEST).read_text()
    assert text.index("cli_version:") < text.index("cli_build:") < text.index("agent_directory:")


def test_the_digest_follows_the_files_and_nothing_else(tmp_path: pathlib.Path) -> None:
    tree = tmp_path / "tree"
    (tree / "app").mkdir(parents=True)
    (tree / "app" / "agent.py").write_text("x = 1\n")
    (tree / MANIFEST).write_text("name: a\n")
    first = template_digest(tree)
    assert first.startswith("sha256:") and len(first) == len("sha256:") + 64

    (tree / MANIFEST).write_text("name: b\n")  # the manifest is not part of the render
    (tree / "app" / "__pycache__").mkdir()
    (tree / "app" / "__pycache__" / "agent.pyc").write_bytes(b"\0")
    assert template_digest(tree) == first

    (tree / "app" / "agent.py").write_text("x = 2\n")
    assert template_digest(tree) != first
    (tree / "app" / "agent.py").write_text("x = 1\n")
    (tree / "app" / "agent.py").rename(tree / "app" / "graph.py")
    assert template_digest(tree) != first


def test_the_same_settings_render_the_same_digest(
    run_create: CreateRunner, tmp_path: pathlib.Path
) -> None:
    project = _project(run_create)
    again = tmp_path / "again"
    runner_args = ["my-agent", "--agent", "mini_agent", "--output-dir", str(again), "-y"]
    from graph_agents_cli.scaffold.commands.create import create

    result = CliRunner().invoke(create, [*runner_args, "--skip-checks"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert read_manifest(again / "my-agent")["cli_build"] == read_manifest(project)["cli_build"]

    other = _project(run_create, "--runtime", "langgraph-server", name="other")
    assert (
        read_manifest(other)["cli_build"]["template_digest"]
        != read_manifest(project)["cli_build"]["template_digest"]
    )


# --- upgrade at the same version ------------------------------------------------


def test_the_same_build_is_up_to_date(run_create: CreateRunner, uvx: FakeUvx) -> None:
    project = _project(run_create)
    before = _snapshot(project)
    result = _upgrade(project, "-y")
    assert result.exit_code == 0, result.output
    assert f"already at version 0.2.0 (build {RUNNING_ID})" in result.output
    assert uvx.calls == [] and _snapshot(project) == before


def test_a_manifest_without_a_build_is_compared_by_version_and_told_how_to_name_it(
    run_create: CreateRunner, uvx: FakeUvx
) -> None:
    project = _project(run_create)
    build_record.write_build_record(project, None)
    before = _snapshot(project)
    result = _upgrade(project, "--dry-run")
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "already at version 0.2.0" in out
    assert "Compared by version only" in out and "records no build (cli_build)" in out
    assert "--baseline-ref <ref>" in out and "<clone>@<commit>" in out
    assert "git -C <clone> log -1 --format=%H --before=" in out
    assert uvx.calls == [] and _snapshot(project) == before


def test_another_build_that_renders_the_same_files_is_up_to_date(
    run_create: CreateRunner, uvx: FakeUvx
) -> None:
    project = _project(run_create)
    digest = read_manifest(project)["cli_build"]["template_digest"]
    build_record.write_build_record(
        project, BuildRecord(f"0.2.0+g{OTHER_COMMIT[:7]}", OTHER_COMMIT, digest)
    )
    before = _snapshot(project)
    result = _upgrade(project, "-y")
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "already at version 0.2.0: build 0.2.0+gd99c816 renders the same files" in out
    assert uvx.calls == [] and _snapshot(project) == before


def test_another_build_with_other_templates_is_upgraded_from_its_commit(
    run_create: CreateRunner, uvx: FakeUvx
) -> None:
    project = _project(run_create)
    _old_build(project)
    old_id = f"0.2.0+g{OTHER_COMMIT[:7]}"
    build_record.write_build_record(project, BuildRecord(old_id, OTHER_COMMIT, OTHER_DIGEST))

    def old_templates(snapshot: pathlib.Path) -> None:
        _old_build(snapshot)
        build_record.write_build_record(snapshot, BuildRecord(old_id, OTHER_COMMIT, None))

    uvx.render = old_templates
    result = _upgrade(project, "-y")
    assert result.exit_code == 0, result.output
    assert uvx.specs() == [f"git+{REPO}@{OTHER_COMMIT}"]
    assert f"Upgrading 0.2.0 (build {old_id}) → 0.2.0 (build {RUNNING_ID})" in result.output
    # The file the project had not edited takes the new template's content.
    assert "OLD_TEMPLATE" not in (project / "README.md").read_text()
    record = read_manifest(project)["cli_build"]
    assert record["id"] == RUNNING_ID and record["commit"] == TEST_BUILD_COMMIT
    assert record["template_digest"] != OTHER_DIGEST

    # Now it is at this build.
    assert "already at version 0.2.0 (build" in _upgrade(project, "--dry-run").output


def test_a_commit_the_remote_does_not_have_says_how_to_name_a_clone(
    run_create: CreateRunner, uvx: FakeUvx, caplog: pytest.LogCaptureFixture
) -> None:
    project = _project(run_create)
    build_record.write_build_record(
        project, BuildRecord(f"0.2.0+g{OTHER_COMMIT[:7]}", OTHER_COMMIT, OTHER_DIGEST)
    )
    before = _snapshot(project)
    uvx.fail = True
    result = _upgrade(project, "-y")
    assert result.exit_code == 2, result.output
    out = " ".join(result.output.split())
    assert "commit not found" in caplog.text  # uvx's own reason
    assert "--baseline-ref <clone>@<commit>" in out and "not modified" in out
    assert _snapshot(project) == before


def test_a_build_between_releases_is_not_fetched_through_a_release_only_mirror(
    run_create: CreateRunner, uvx: FakeUvx, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(run_create)
    build_record.write_build_record(
        project, BuildRecord(f"0.2.0+g{OTHER_COMMIT[:7]}", OTHER_COMMIT, OTHER_DIGEST)
    )
    monkeypatch.setenv(version_module.INSTALL_SPEC_ENV, "git+https://git.example/gac@v{version}")
    result = _upgrade(project, "--dry-run")
    assert result.exit_code == 3, result.output
    out = " ".join(result.output.split())
    assert "names releases only" in out and f"git+https://<mirror>@{OTHER_COMMIT}" in out
    assert uvx.calls == []


def test_a_build_with_uncommitted_changes_has_no_authentic_baseline(
    run_create: CreateRunner, uvx: FakeUvx
) -> None:
    project = _project(run_create)
    build_record.write_build_record(
        project, BuildRecord(f"0.2.0+g{OTHER_COMMIT[:7]}.dirty", OTHER_COMMIT, OTHER_DIGEST)
    )
    result = _upgrade(project, "--dry-run")
    assert result.exit_code == 3, result.output
    assert "uncommitted changes" in result.output and "--baseline-ref" in result.output
    assert uvx.calls == []


def test_a_release_build_of_the_same_version_is_rebuilt_from_its_tag(
    run_create: CreateRunner, uvx: FakeUvx
) -> None:
    project = _project(run_create)
    build_record.write_build_record(project, BuildRecord("0.2.0", OTHER_COMMIT, OTHER_DIGEST))
    uvx.render = lambda snapshot: build_record.write_build_record(
        snapshot, BuildRecord("0.2.0", OTHER_COMMIT, None)
    )
    result = _upgrade(project, "--dry-run")
    assert result.exit_code == 0, result.output
    assert uvx.specs() == [f"git+{REPO}@v0.2.0"]


def test_the_baseline_must_be_the_recorded_build(run_create: CreateRunner, uvx: FakeUvx) -> None:
    """A tag that moved (or a mirror that serves another commit) is refused."""
    project = _project(run_create)
    build_record.write_build_record(project, BuildRecord("0.2.0", OTHER_COMMIT, OTHER_DIGEST))
    before = _snapshot(project)
    # The fake renders with this build, whose record names TEST_BUILD_COMMIT.
    result = _upgrade(project, "-y")
    assert result.exit_code == 3, result.output
    out = " ".join(result.output.split())
    assert f"is build {RUNNING_ID}, but the project records build 0.2.0" in out
    assert _snapshot(project) == before


def test_baseline_current_cannot_upgrade_a_project_of_the_same_version(
    run_create: CreateRunner, uvx: FakeUvx
) -> None:
    project = _project(run_create)
    result = CliRunner().invoke(upgrade, [str(project), "--baseline", "current"])
    assert result.exit_code == 2, result.output
    assert "--baseline-ref" in result.output


def test_a_record_of_another_version_is_ignored(run_create: CreateRunner, uvx: FakeUvx) -> None:
    project = _project(run_create)
    build_record.write_build_record(
        project, BuildRecord("0.1.9+gd99c816", OTHER_COMMIT, OTHER_DIGEST)
    )
    result = _upgrade(project, "--dry-run")
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "is not a build of cli_version '0.2.0' in graph-agents-cli-manifest.yaml" in out
    assert "it is ignored" in out
    assert "Compared by version only" in out


def test_a_malformed_record_is_ignored(run_create: CreateRunner, uvx: FakeUvx) -> None:
    project = _project(run_create)
    path = project / MANIFEST
    path.write_text(path.read_text().replace(f"commit: {TEST_BUILD_COMMIT}", "commit: HEAD"))
    result = _upgrade(project, "--dry-run")
    assert result.exit_code == 0, result.output
    assert "cli_build.commit is not a full git commit" in result.output
    assert "Compared by version only" in " ".join(result.output.split())


# --- --baseline-ref ---------------------------------------------------------------


def test_baseline_ref_upgrades_a_project_that_predates_the_record(
    run_create: CreateRunner, uvx: FakeUvx
) -> None:
    project = _project(run_create)
    _old_build(project)
    guidance = project / "AGENTS.md"
    guidance.write_text(guidance.read_text() + "\nOur own notes.\n")
    uvx.render = _old_build  # the named build rendered this, and records no build

    dry = _upgrade(project, "--baseline-ref", "d99c816", "--dry-run")
    assert dry.exit_code == 0, dry.output
    assert uvx.specs() == [f"git+{REPO}@d99c816"]
    assert "OLD_TEMPLATE" in (project / "README.md").read_text()  # a dry run

    result = _upgrade(project, "--baseline-ref", "d99c816", "-y")
    assert result.exit_code == 0, result.output
    assert "Baseline: d99c816 of" in result.output
    assert "OLD_TEMPLATE" not in (project / "README.md").read_text()
    assert guidance.read_text().endswith("Our own notes.\n")  # the project's edit is kept
    manifest = read_manifest(project)
    assert manifest["cli_version"] == "0.2.0"
    assert manifest["cli_build"]["id"] == RUNNING_ID
    assert manifest["cli_build"]["template_digest"].startswith("sha256:")
    assert "already at version 0.2.0 (build" in _upgrade(project, "--dry-run").output


def test_baseline_ref_of_another_version_is_refused(run_create: CreateRunner, uvx: FakeUvx) -> None:
    project = _project(run_create)
    _old_build(project)
    before = _snapshot(project)

    def other_version(snapshot: pathlib.Path) -> None:
        path = snapshot / MANIFEST
        path.write_text(path.read_text().replace("cli_version: '0.2.0'", "cli_version: '0.1.0'"))

    uvx.render = other_version
    result = _upgrade(project, "--baseline-ref", "v0.1.0", "-y")
    assert result.exit_code == 3, result.output
    out = " ".join(result.output.split())
    assert "is graph-agents-cli 0.1.0, but graph-agents-cli-manifest.yaml records 0.2.0" in out
    assert _snapshot(project) == before


def test_baseline_ref_that_renders_this_build_says_nothing_can_come_from_it(
    run_create: CreateRunner, uvx: FakeUvx
) -> None:
    project = _project(run_create)
    build_record.write_build_record(project, None)
    result = _upgrade(project, "--baseline-ref", "main", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "renders the same files as this build" in " ".join(result.output.split())


def test_a_later_build_named_as_the_baseline_is_flagged(
    run_create: CreateRunner, uvx: FakeUvx
) -> None:
    """The project has older content in files the named build and this one agree on."""
    project = _project(run_create)
    build_record.write_build_record(project, None)
    older = [
        p.relative_to(project).as_posix()
        for p in sorted(project.rglob("*"))
        if p.is_file()
        and p.suffix in (".py", ".yaml", ".md", ".json", "")
        and build_record.MANIFEST_FILENAME not in p.name
        and not p.relative_to(project).as_posix().startswith(("app/agent.py", "app/tools"))
        and "values-" not in p.name
        and p.name not in ("pyproject.toml", "README.md", ".env.example")
    ][:6]
    assert len(older) == 6, older
    for rel in older:
        (project / rel).write_text((project / rel).read_text() + "\n# as an older build had it\n")

    def later(snapshot: pathlib.Path) -> None:
        (snapshot / "README.md").write_text("# the named build's README\n")

    uvx.render = later
    result = _upgrade(project, "--baseline-ref", "main", "--dry-run")
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "6 of 7 template files differ from this baseline" in out
    assert "the baseline is not that build" in out

    # A named build that renders exactly this build's files: both warnings.
    uvx.render = None
    result = _upgrade(project, "--baseline-ref", "main", "--dry-run")
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "renders the same files as this build" in out
    assert "6 of 6 template files differ from this baseline" in out

    # The right build (it rendered what the project has) raises no such warning.
    def right(snapshot: pathlib.Path) -> None:
        for rel in older:
            (snapshot / rel).write_text((project / rel).read_text())

    uvx.render = right
    result = _upgrade(project, "--baseline-ref", "main", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "is not that build" not in result.output


def test_baseline_ref_and_baseline_current_exclude_each_other(run_create: CreateRunner) -> None:
    project = _project(run_create)
    result = CliRunner().invoke(
        upgrade, [str(project), "--baseline-ref", "main", "--baseline", "current"]
    )
    assert result.exit_code == 2 and "cannot be combined" in result.output


def test_a_record_of_another_commit_than_an_explicit_ref_is_a_warning(
    run_create: CreateRunner, uvx: FakeUvx
) -> None:
    project = _project(run_create)
    build_record.write_build_record(
        project, BuildRecord(f"0.2.0+g{OTHER_COMMIT[:7]}", OTHER_COMMIT, OTHER_DIGEST)
    )
    result = _upgrade(project, "--baseline-ref", "main", "--dry-run")
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert f"is build {RUNNING_ID}, but the project records build 0.2.0+gd99c816" in out
    assert "Using it, as --baseline-ref asks" in out


# --- upgrade across versions --------------------------------------------------------


def test_a_build_between_releases_of_an_older_version_is_rebuilt_from_its_commit(
    run_create: CreateRunner, uvx: FakeUvx
) -> None:
    project = _project(run_create)
    path = project / MANIFEST
    path.write_text(path.read_text().replace("cli_version: '0.2.0'", "cli_version: '0.1.0'"))
    build_record.write_build_record(
        project, BuildRecord(f"0.1.0+g{OTHER_COMMIT[:7]}", OTHER_COMMIT, OTHER_DIGEST)
    )

    def old(snapshot: pathlib.Path) -> None:
        snap = snapshot / MANIFEST
        snap.write_text(snap.read_text().replace("cli_version: '0.2.0'", "cli_version: '0.1.0'"))
        build_record.write_build_record(snapshot, BuildRecord("0.1.0+gd99c816", OTHER_COMMIT))

    uvx.render = old
    result = _upgrade(project, "-y")
    assert result.exit_code == 0, result.output
    assert uvx.specs() == [f"git+{REPO}@{OTHER_COMMIT}"]  # not the v0.1.0 tag
    manifest = read_manifest(project)
    assert manifest["cli_version"] == "0.2.0" and manifest["cli_build"]["id"] == RUNNING_ID


def test_a_release_or_a_manifest_without_a_record_of_an_older_version_uses_its_tag(
    run_create: CreateRunner, uvx: FakeUvx
) -> None:
    project = _project(run_create)
    path = project / MANIFEST
    path.write_text(path.read_text().replace("cli_version: '0.2.0'", "cli_version: '0.1.0'"))
    build_record.write_build_record(project, None)

    def old(snapshot: pathlib.Path) -> None:
        snap = snapshot / MANIFEST
        snap.write_text(snap.read_text().replace("cli_version: '0.2.0'", "cli_version: '0.1.0'"))
        build_record.write_build_record(snapshot, None)

    uvx.render = old
    result = _upgrade(project, "--dry-run")
    assert result.exit_code == 0, result.output
    assert uvx.specs() == [f"git+{REPO}@v0.1.0"]


# --- --baseline-ref forms -------------------------------------------------------------


@pytest.fixture
def real_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let `git` run for real (the scaffold tests otherwise refuse every subprocess)."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    import graph_agents_cli._runner as runner

    def run(args, *, resolve_executable=True, **kwargs):
        assert args[0] == "git", args
        return subprocess.run(args, **kwargs)

    monkeypatch.setattr(runner, "run_resolved", run)


def _clone(tmp_path: pathlib.Path) -> tuple[pathlib.Path, str]:
    clone = tmp_path / "gac clone"
    clone.mkdir()
    git = ["git", "-C", str(clone), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q"], check=True)
    (clone / "f").write_text("x")
    subprocess.run([*git, "add", "f"], check=True)
    subprocess.run([*git, "-c", "commit.gpgsign=false", "commit", "-qm", "one"], check=True)
    head = subprocess.run(
        [*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    return clone, head


def test_baseline_ref_forms(tmp_path: pathlib.Path, real_git: None) -> None:
    bare = resolve_baseline_ref("d99c816")
    assert bare.spec == f"git+{REPO}@d99c816" and not bare.refresh
    assert resolve_baseline_ref("v0.2.0").spec == f"git+{REPO}@v0.2.0"
    spec = "git+https://git.example/gac@1a2b3c4"
    assert resolve_baseline_ref(spec).spec == spec
    assert resolve_baseline_ref("graph-agents-cli==0.1.0").spec == "graph-agents-cli==0.1.0"

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    local = resolve_baseline_ref(str(checkout))
    assert local.spec == str(checkout.resolve()) and local.refresh  # rebuilt, never a stale cache

    wheel = tmp_path / "graph_agents_cli-0.2.0-py3-none-any.whl"
    wheel.write_bytes(b"")
    assert resolve_baseline_ref(str(wheel)).spec == str(wheel.resolve())

    clone, head = _clone(tmp_path)
    at = resolve_baseline_ref(f"{clone}@{head[:7]}")
    # The short ref is resolved to the full commit, in a file URL (the space escaped).
    assert at.spec == f"git+{clone.resolve().as_uri()}@{head}" and "%20" in at.spec
    assert at.commit == head and not at.refresh


def test_a_ref_named_like_a_directory_here_is_still_a_ref(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "main").mkdir()  # any directory, not a checkout
    (tmp_path / "release" / "0.2").mkdir(parents=True)
    assert resolve_baseline_ref("main").spec == f"git+{REPO}@main"
    assert resolve_baseline_ref("release/0.2").spec == f"git+{REPO}@release/0.2"
    # A checkout (it has a pyproject.toml) is a path, written relatively or not.
    (tmp_path / "gac").mkdir()
    (tmp_path / "gac" / "pyproject.toml").write_text("[project]\n")
    assert resolve_baseline_ref("gac").spec == str((tmp_path / "gac").resolve())
    assert resolve_baseline_ref("./main").spec == str((tmp_path / "main").resolve())


@pytest.mark.parametrize(
    "ref, message",
    [
        ("", "must name a graph-agents-cli build"),
        ("v1\nv2", "a newline"),
        ("--upload-pack=x", "is not a git ref"),
        ("a..b", "is not a git ref"),
        ("/no/such/clone@abc1234", "is not a directory"),
    ],
)
def test_baseline_ref_refuses_what_names_no_build(ref: str, message: str) -> None:
    with pytest.raises(InstallSpecError, match=message) as error:
        resolve_baseline_ref(ref)
    assert error.value.exit_code == 3


def test_baseline_ref_in_a_clone_without_the_commit(tmp_path: pathlib.Path, real_git: None) -> None:
    clone, _ = _clone(tmp_path)
    with pytest.raises(InstallSpecError, match="has no commit named 'feedbee'"):
        resolve_baseline_ref(f"{clone}@feedbee")


def test_a_local_build_is_refreshed_by_uvx(run_create: CreateRunner, uvx: FakeUvx) -> None:
    project = _project(run_create)
    build_record.write_build_record(project, None)
    checkout = project.parent / "checkout"
    checkout.mkdir()
    result = _upgrade(project, "--baseline-ref", str(checkout), "--dry-run")
    assert result.exit_code == 0, result.output
    assert uvx.calls[0][:5] == [
        "uvx",
        "--refresh-package",
        "graph-agents-cli",
        "--from",
        str(checkout.resolve()),
    ]


# --- enhance and in-folder renders ------------------------------------------------------


def _enhance(project: pathlib.Path, monkeypatch: pytest.MonkeyPatch, *args: str):
    monkeypatch.chdir(project)
    return CliRunner().invoke(
        enhance, [*args, "-y", "--skip-checks", "--skip-deps"], catch_exceptions=False
    )


def test_enhance_of_a_project_at_this_build_records_the_new_settings_digest(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(run_create)
    result = _enhance(project, monkeypatch, "--runtime", "langgraph-server")
    assert result.exit_code == 0, result.output
    record = read_manifest(project)["cli_build"]
    fresh = _project(run_create, "--runtime", "langgraph-server", name="my-agent-2")
    assert record["id"] == RUNNING_ID
    # The digest of this build's render of the new settings (the name differs, so
    # compare with a render of the same name).
    from graph_agents_cli._project import read_project_config
    from graph_agents_cli.scaffold.utils.generation_metadata import metadata_to_cli_args

    out = project.parent / "replay"
    cfg = read_project_config(str(project))
    assert merge._run_vendored_create(metadata_to_cli_args(cfg), out, cfg.project_name)
    assert record["template_digest"] == template_digest(out / cfg.project_name)
    assert fresh.is_dir()


def test_enhance_of_a_project_from_another_build_keeps_that_build(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(run_create)
    old = BuildRecord(f"0.2.0+g{OTHER_COMMIT[:7]}", OTHER_COMMIT, OTHER_DIGEST)
    build_record.write_build_record(project, old)
    result = _enhance(project, monkeypatch, "--runtime", "langgraph-server")
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "rendered by build 0.2.0+gd99c816, whose templates differ" in out
    assert "scaffold upgrade" in out
    record = read_manifest(project)["cli_build"]
    assert record == {"id": old.id, "commit": OTHER_COMMIT, "template_digest": None}


def test_enhance_of_a_project_without_a_record_leaves_it_without_one(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(run_create)
    build_record.write_build_record(project, None)
    result = _enhance(project, monkeypatch, "--runtime", "langgraph-server")
    assert result.exit_code == 0, result.output
    assert "cli_build" not in read_manifest(project)


def test_the_record_is_written_without_touching_comments(tmp_path: pathlib.Path) -> None:
    project = tmp_path / "p"
    project.mkdir()
    original = "# header\nname: p\ncli_version: '0.2.0'  # keep\nsecrets:\n  keys: [A]  # k\n"
    (project / MANIFEST).write_text(original)
    record = BuildRecord(RUNNING_ID, TEST_BUILD_COMMIT, OTHER_DIGEST)
    build_record.write_build_record(project, record)
    text = (project / MANIFEST).read_text()
    assert text.startswith("# header\nname: p\ncli_version: '0.2.0'  # keep\n# The build")
    assert "keys: [A]  # k" in text
    assert build_record.read_build_record(project) == record
    build_record.write_build_record(project, None)
    assert (project / MANIFEST).read_text() == original


def test_info_shows_the_recorded_build(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graph_agents_cli.info import cmd_info

    project = _project(run_create)
    monkeypatch.chdir(project)
    monkeypatch.setattr(cmd_info, "get_installed_skills", lambda: [])
    result = CliRunner().invoke(cmd_info.cmd_info, [])
    assert f"Scaffolded with:    0.2.0 (build {RUNNING_ID})" in result.output
    build_record.write_build_record(project, None)
    result = CliRunner().invoke(cmd_info.cmd_info, [])
    assert "Scaffolded with:    0.2.0 (no build recorded)" in result.output


def test_running_build_takes_the_version_the_engine_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_build, "current_build", lambda: _build.BuildInfo("9.9.9", OTHER_COMMIT))
    assert build_record.running_build().id == f"0.2.0+g{OTHER_COMMIT[:7]}"
