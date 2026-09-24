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

"""Suite-wide isolation: nothing a test runs in process writes to the developer's home."""

from __future__ import annotations

import pathlib

import pytest


@pytest.fixture(autouse=True)
def _backups_under_tmp(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """`scaffold enhance` / `upgrade` back projects up to ~/.graph-agents-cli/backups;
    in tests they go to a temporary directory (subprocess tests set HOME themselves)."""
    from graph_agents_cli.scaffold.utils import backup

    base: pathlib.Path = tmp_path_factory.mktemp("home") / ".graph-agents-cli" / "backups"
    monkeypatch.setattr(backup, "BACKUP_BASE_DIR", base)
    return base
