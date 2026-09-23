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

"""Resolving and vendoring extension sources (local paths; git mocked)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from graph_agents_cli.extension import _resolver
from graph_agents_cli.extension._refs import parse_ref
from graph_agents_cli.extension._resolver import (
    LOCAL_SHA,
    ResolverError,
    expected_stamp,
    materialize,
    read_stamp,
    resolve_sha,
    validate_extension_name,
)

from .conftest import write_extension


def test_local_source_is_vendored_without_its_secrets(tmp_path: Path) -> None:
    src = write_extension(tmp_path / "src" / "team", "team-tools", add={"hi": ["echo", "hi"]})
    (src / ".env").write_text("TOKEN=secret\n")
    (src / ".env.example").write_text("TOKEN=\n")
    (src / "scripts").mkdir()
    (src / "scripts" / "run.sh").write_text("echo run\n")
    ref = parse_ref(f"local@{src}")
    assert resolve_sha(ref) == LOCAL_SHA
    name, dest = materialize(ref, LOCAL_SHA, tmp_path / "vendored")
    assert name == "team-tools" and dest == tmp_path / "vendored" / "team-tools"
    assert (dest / "scripts" / "run.sh").read_text() == "echo run\n"
    assert not (dest / ".env").exists()  # credentials never travel with a source
    assert (dest / ".env.example").exists()  # documentation does
    assert read_stamp(dest) == expected_stamp(ref, LOCAL_SHA)


def test_local_stamp_follows_edits_to_the_source(tmp_path: Path) -> None:
    src = write_extension(tmp_path / "src", "team-tools")
    ref = parse_ref(f"local@{src}")
    _name, dest = materialize(ref, LOCAL_SHA, tmp_path / "vendored")
    before = read_stamp(dest)
    assert before == expected_stamp(ref, LOCAL_SHA)
    (src / "extra.txt").write_text("new\n")
    assert expected_stamp(ref, LOCAL_SHA) != before


def test_symlinks_in_a_source_are_not_copied(tmp_path: Path) -> None:
    src = write_extension(tmp_path / "src", "team-tools")
    secret = tmp_path / "outside-secret"
    secret.write_text("x")
    (src / "link").symlink_to(secret)
    _name, dest = materialize(parse_ref(f"local@{src}"), LOCAL_SHA, tmp_path / "vendored")
    assert not (dest / "link").exists()


@pytest.mark.parametrize("missing", ["path", "manifest"])
def test_unusable_local_sources_are_clear_errors(tmp_path: Path, missing: str) -> None:
    src = tmp_path / "src"
    if missing == "manifest":
        src.mkdir()
    with pytest.raises(ResolverError, match="not found" if missing == "path" else "has no"):
        materialize(parse_ref(f"local@{src}"), LOCAL_SHA, tmp_path / "vendored")


def test_selector_picks_a_subdirectory(tmp_path: Path) -> None:
    write_extension(tmp_path / "repo" / "soc2", "soc2")
    name, dest = materialize(
        parse_ref(f"local@{tmp_path / 'repo'}#soc2"), LOCAL_SHA, tmp_path / "v"
    )
    assert name == "soc2" and (dest / "graph-agents-cli-extension.yaml").exists()


def test_a_full_sha_is_used_as_given_and_ls_remote_failures_are_errors(monkeypatch) -> None:
    sha = "a" * 40
    assert resolve_sha(parse_ref("acme/tools", ref_override=sha)) == sha

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 128, stdout="", stderr="not found")

    monkeypatch.setattr(_resolver, "run_resolved", fake_run)
    with pytest.raises(ResolverError, match="could not resolve 'v9'"):
        resolve_sha(parse_ref("acme/tools", ref_override="v9"))


def test_ls_remote_output_is_parsed(monkeypatch) -> None:
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=f"{'b' * 40}\trefs/tags/v1\n")

    monkeypatch.setattr(_resolver, "run_resolved", fake_run)
    assert resolve_sha(parse_ref("acme/tools", ref_override="v1")) == "b" * 40
    assert calls[0][:3] == ["git", "ls-remote", "https://github.com/acme/tools.git"]


@pytest.mark.parametrize("name", ["../escape", "a/b", "", ".."])
def test_extension_names_cannot_escape_the_vendor_dir(name: str) -> None:
    with pytest.raises(ResolverError):
        validate_extension_name(name)
