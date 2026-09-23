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
"""argocd desired-state writer against a real (local, bare) git remote.

No network: ``origin`` is a bare repository in the temp dir; ``origin_url`` is
patched to a github.com URL only so the slug parsing passes, and the pull
request step stops at the "no gh, no token" configuration error after the push.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from graph_agents_cli.deploy import _kube, gitops
from graph_agents_cli.deploy._kube import ConfigError

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

VALUES = 'image:\n  tag: "old0000"  # keep\nreplicaCount: 4\n'
REL = Path("deployment/helm/app/values-prod.yaml")


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A bare origin with main, a teammate clone that pushed replicaCount 4, a stale dev clone."""
    bare = tmp_path / "origin.git"
    _git("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    seed = tmp_path / "seed"
    _git("clone", "-q", str(bare), str(seed), cwd=tmp_path)
    for k, v in (("user.name", "t"), ("user.email", "t@example.com")):
        _git("config", k, v, cwd=seed)
    (seed / "apps" / "agent" / REL).parent.mkdir(parents=True)
    (seed / "apps" / "agent" / REL).write_text(
        'image:\n  tag: "old0000"  # keep\nreplicaCount: 2\n'
    )
    (seed / "README.md").write_text("hi\n")
    _git("add", ".", cwd=seed)
    _git("commit", "-q", "-m", "seed", cwd=seed)
    _git("push", "-q", "origin", "HEAD:main", cwd=seed)

    dev = tmp_path / "dev"
    _git("clone", "-q", str(bare), str(dev), cwd=tmp_path)
    for k, v in (("user.name", "d"), ("user.email", "d@example.com")):
        _git("config", k, v, cwd=dev)

    # A teammate lands replicaCount: 4 on main after the developer cloned.
    (seed / "apps" / "agent" / REL).write_text(VALUES)
    _git("commit", "-q", "-am", "scale", cwd=seed)
    _git("push", "-q", "origin", "HEAD:main", cwd=seed)

    monkeypatch.chdir(dev / "apps" / "agent")
    monkeypatch.setattr(gitops, "origin_url", lambda: "https://github.com/o/r.git")
    monkeypatch.setattr(_kube, "tool_available", lambda name: False)
    for var in ("GITHUB_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN", "GH_HOST"):
        monkeypatch.delenv(var, raising=False)
    return bare, dev


def _blob_on_branch(bare: Path, branch: str, path: str) -> str:
    return subprocess.run(
        ["git", "show", f"{branch}:{path}"], cwd=bare, check=True, capture_output=True, text=True
    ).stdout


def test_pr_branch_is_built_from_origin_main_below_the_git_root(repos) -> None:
    bare, dev = repos
    local = dev / "apps" / "agent" / REL
    local.write_text('image:\n  tag: "old0000"  # keep\nreplicaCount: 2\nlocalEdit: true\n')
    with pytest.raises(ConfigError, match="GITHUB_TOKEN"):
        gitops.write_desired_state(
            env="prod",
            values_path=REL,
            image_repository="ghcr.io/o/app",
            tag="abc1234",
            project_name="app",
        )
    # The branch exists on the remote, parented on main, touching only the repo-relative file.
    branch = "deploy/prod/abc1234"
    assert _git("rev-parse", f"{branch}^", cwd=bare) == _git("rev-parse", "main", cwd=bare)
    changed = _git("diff-tree", "-r", "--name-status", "main", branch, cwd=bare)
    assert changed == "M\tapps/agent/deployment/helm/app/values-prod.yaml"
    content = _blob_on_branch(bare, branch, "apps/agent/deployment/helm/app/values-prod.yaml")
    assert content == 'image:\n  tag: "abc1234"  # keep\nreplicaCount: 4\n'
    # The developer's stale, edited working tree was neither used nor touched.
    assert "localEdit: true" in local.read_text()
    # The edit is still an unstaged working-tree change: the index was never touched.
    project_dir = dev / "apps" / "agent"
    assert _git("diff", "--name-only", "--", str(REL), cwd=project_dir).endswith(str(REL))
    assert _git("diff", "--cached", "--name-only", cwd=project_dir) == ""


def test_noop_is_judged_on_origin_main_not_the_local_file(repos) -> None:
    bare, dev = repos
    (dev / "apps" / "agent" / REL).write_text('image:\n  tag: "abc1234"\n')  # local already has it
    with pytest.raises(ConfigError, match="GITHUB_TOKEN"):
        gitops.write_desired_state(
            env="prod", values_path=REL, image_repository=None, tag="abc1234", project_name="app"
        )
    assert "deploy/prod/abc1234" in _git("branch", "--list", "deploy/prod/*", cwd=bare)
    # A tag main already carries is a real no-op (no branch pushed).
    result = gitops.write_desired_state(
        env="prod", values_path=REL, image_repository=None, tag="old0000", project_name="app"
    )
    assert result.changed is False
    assert "deploy/prod/old0000" not in _git("branch", "--list", cwd=bare)


def _fresh_ci_clone(bare: Path, tmp: Path, name: str, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A shallow clone of main, as actions/checkout makes: no refs for other branches."""
    clone = tmp / name
    _git("clone", "-q", "--depth", "1", "--branch", "main", f"file://{bare}", str(clone), cwd=tmp)
    for k, v in (("user.name", "bot"), ("user.email", "bot@example.com")):
        _git("config", k, v, cwd=clone)
    monkeypatch.chdir(clone / "apps" / "agent")
    return clone


def _promote(tag: str = "abc1234") -> None:
    with pytest.raises(ConfigError, match="GITHUB_TOKEN"):
        gitops.write_desired_state(
            env="prod",
            values_path=REL,
            image_repository="ghcr.io/o/app",
            tag=tag,
            project_name="app",
        )


def test_retrying_a_promotion_from_a_fresh_ci_clone_is_idempotent(
    repos, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first run pushed deploy/prod/<tag>; a retry in a fresh clone must not fail with
    'stale info', and with nothing changed it must not replace the branch (an approved PR
    would otherwise lose its approval)."""
    bare, _dev = repos
    _promote()
    branch = "deploy/prod/abc1234"
    first = _git("rev-parse", branch, cwd=bare)
    _fresh_ci_clone(bare, tmp_path, "ci1", monkeypatch)
    _promote()
    assert _git("rev-parse", branch, cwd=bare) == first


def test_retry_after_main_moved_replaces_the_branch(
    repos, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare, _dev = repos
    _promote()
    branch = "deploy/prod/abc1234"
    first = _git("rev-parse", branch, cwd=bare)
    # main moves on (an unrelated commit), then the promotion is re-run from a fresh clone.
    seed = tmp_path / "seed"
    (seed / "README.md").write_text("moved\n")
    _git("commit", "-q", "-am", "move main", cwd=seed)
    _git("push", "-q", "origin", "HEAD:main", cwd=seed)
    _fresh_ci_clone(bare, tmp_path, "ci2", monkeypatch)
    _promote()
    second = _git("rev-parse", branch, cwd=bare)
    assert second != first
    assert _git("rev-parse", f"{branch}^", cwd=bare) == _git("rev-parse", "main", cwd=bare)


def test_the_lease_still_refuses_a_push_that_landed_in_between(
    repos, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare, _dev = repos
    _promote()
    branch = "deploy/prod/abc1234"
    first = _git("rev-parse", branch, cwd=bare)
    # Someone pushes to the branch after this run read it with ls-remote.
    seed = tmp_path / "seed"
    _git("fetch", "-q", "origin", f"{branch}:{branch}", cwd=seed)
    _git("checkout", "-q", branch, cwd=seed)
    (seed / "README.md").write_text("reviewer fix\n")
    _git("commit", "-q", "-am", "reviewer fix", cwd=seed)
    _git("push", "-q", "origin", f"{branch}:{branch}", cwd=seed)
    theirs = _git("rev-parse", branch, cwd=bare)
    # main moves too, so this run's commit differs from the one it read.
    _git("checkout", "-q", "main", cwd=seed)
    _git("pull", "-q", "origin", "main", cwd=seed)
    (seed / "NOTES.md").write_text("main moved\n")
    _git("add", "NOTES.md", cwd=seed)
    _git("commit", "-q", "-m", "move main", cwd=seed)
    _git("push", "-q", "origin", "HEAD:main", cwd=seed)
    monkeypatch.setattr(gitops, "_remote_branch_sha", lambda _b: first)
    with pytest.raises(_kube.ToolFailed):
        _promote()
    assert _git("rev-parse", branch, cwd=bare) == theirs


def test_file_absent_from_origin_main_is_a_config_error(repos) -> None:
    bare, _dev = repos
    with pytest.raises(ConfigError, match="not committed on origin/main"):
        gitops.write_desired_state(
            env="staging",
            values_path=Path("deployment/helm/app/values-staging.yaml"),
            image_repository=None,
            tag="x",
            project_name="app",
        )
    assert _git("branch", "--list", "deploy/*", cwd=bare) == ""
