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

"""Backups made by enhance/upgrade: private, and only the newest few kept."""

from __future__ import annotations

import pathlib
import stat

import pytest

from graph_agents_cli.scaffold.utils import backup


def _mode(path: pathlib.Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def project(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "agent"
    (root / "app").mkdir(parents=True)
    (root / "app" / "agent.py").write_text("graph = 1\n")
    (root / ".env").write_text("API_KEY=secret\n")
    (root / ".env.staging").write_text("API_KEY=staging-secret\n")
    (root / ".env").chmod(0o644)
    return root


def test_backup_is_private(project: pathlib.Path, isolated_home: pathlib.Path) -> None:
    made = backup.create_project_backup(project)
    assert made is not None and (made / ".env").read_text() == "API_KEY=secret\n"
    assert _mode(backup.BACKUP_BASE_DIR) == 0o700
    assert _mode(made) == 0o700 and _mode(made / "app") == 0o700
    assert _mode(made / ".env") == 0o600 and _mode(made / ".env.staging") == 0o600
    assert _mode(project / ".env") == 0o644  # the project itself is left alone


def test_only_the_newest_backups_are_kept(
    project: pathlib.Path, isolated_home: pathlib.Path
) -> None:
    base = backup.BACKUP_BASE_DIR
    base.mkdir(parents=True)
    prefix = f"agent_{backup.project_backup_id(project)}"
    old = [base / f"{prefix}_2026010{day}_120000" for day in range(1, 8)]
    for path in old:
        path.mkdir()
    # Another project's backups, including one whose name extends this one's, and
    # backups an older CLI named without a project id (never pruned: whose are they?).
    others = [
        base / "agent-two_20260101_120000",
        base / "agent_x_20260101_120000",
        base / "agent_20260101_120000",
        base / "agent_0123456789ab_20260101_120000",
    ]
    for path in others:
        path.mkdir()

    made = backup.create_project_backup(project)
    assert made is not None and made.name.startswith(prefix + "_")
    kept = sorted(p.name for p in base.iterdir() if p.name.startswith(prefix))
    assert len(kept) == backup.KEEP_BACKUPS
    assert made.name in kept
    # The newest four of the older ones survive with the new one.
    assert kept[:-1] == [p.name for p in old[-(backup.KEEP_BACKUPS - 1) :]]
    assert all(p.exists() for p in others)


def test_a_checkout_with_the_same_directory_name_keeps_its_backups(
    tmp_path: pathlib.Path, isolated_home: pathlib.Path
) -> None:
    """The verifier's case: six backups of B/same pruned A/same's only backup (and its .env)."""
    a = tmp_path / "A" / "same"
    b = tmp_path / "B" / "same"
    for root, key in ((a, "only-copy-of-A"), (b, "b")):
        root.mkdir(parents=True)
        (root / ".env").write_text(f"OPENAI_API_KEY={key}\n")
    assert backup.project_backup_id(a) != backup.project_backup_id(b)

    a_backup = backup.create_project_backup(a)
    for _ in range(backup.KEEP_BACKUPS + 1):
        backup.create_project_backup(b)
    assert (
        a_backup is not None
        and (a_backup / ".env").read_text() == "OPENAI_API_KEY=only-copy-of-A\n"
    )
    b_backups = [p for p in backup.BACKUP_BASE_DIR.iterdir() if p != a_backup]
    assert len(b_backups) == backup.KEEP_BACKUPS


def test_the_project_id_follows_the_path_and_the_manifest_name(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "agent"
    root.mkdir()
    bare = backup.project_backup_id(root)
    assert backup.project_backup_id(tmp_path / "." / "agent") == bare  # resolved
    (root / "graph-agents-cli-manifest.yaml").write_text("name: agent\n")
    named = backup.project_backup_id(root)
    assert named != bare and backup.project_backup_id(root) == named  # stable
    (root / "graph-agents-cli-manifest.yaml").write_text("name: [unclosed\n")
    assert backup.project_backup_id(root) == bare  # an unreadable manifest never fails a backup


def test_two_backups_in_the_same_second_do_not_collide(
    project: pathlib.Path, isolated_home: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = backup.create_project_backup(project)
    second = backup.create_project_backup(project)
    assert first is not None and second is not None and first != second
    assert second.exists() and first.exists()
