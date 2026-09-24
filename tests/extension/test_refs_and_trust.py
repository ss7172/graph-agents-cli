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

"""Extension references (every accepted form) and the trust gate."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from graph_agents_cli.extension._refs import (
    FIRST_PARTY_REPO,
    RefParseError,
    anchor_local,
    looks_like_path,
    parse_ref,
)
from graph_agents_cli.extension._trust import confirm_trust


@pytest.mark.parametrize(
    ("raw", "kind", "repo", "selector"),
    [
        ("soc2", "first_party", FIRST_PARTY_REPO, "soc2"),
        ("acme/acli-extensions", "github", "acme/acli-extensions", None),
        ("acme/acli-extensions#soc2", "github", "acme/acli-extensions", "soc2"),
        ("https://git.example.com/acme/tools.git", "git", "acme/tools", None),
        ("git@git.example.com:acme/tools", "git", "acme/tools", None),
        ("ssh://git@git.example.com/acme/tools#x", "git", "acme/tools", "x"),
    ],
)
def test_remote_reference_forms(raw, kind, repo, selector) -> None:
    ref = parse_ref(raw)
    assert (ref.kind, ref.repo, ref.selector) == (kind, repo, selector)
    assert ref.local_path is None


@pytest.mark.parametrize(
    ("raw", "path", "selector"),
    [
        ("local@../ext", "../ext", None),
        ("local@/abs/ext#tools", "/abs/ext", "tools"),
        # Written as paths: local without the prefix (the `add /abs/dir` UX fix).
        ("/abs/ext", "/abs/ext", None),
        ("./ext", "ext", None),
        ("../ext#tools", "../ext", "tools"),
        (".", ".", None),
    ],
)
def test_local_reference_forms(raw, path, selector) -> None:
    ref = parse_ref(raw)
    assert ref.kind == "local"
    assert ref.local_path == Path(path)
    assert ref.selector == selector
    assert ref.raw.startswith("local@")


def test_home_relative_paths_expand(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert parse_ref("~/ext").local_path == tmp_path / "ext"
    assert parse_ref("local@~/ext").local_path == tmp_path / "ext"


@pytest.mark.parametrize("value", ["/x", "./x", "../x", "~/x", "C:\\x", "c:/x", ".", ".."])
def test_looks_like_path(value) -> None:
    assert looks_like_path(value)


@pytest.mark.parametrize("value", ["org/repo", "soc2", "git@h:o/r", ".hidden-name"])
def test_not_a_path(value) -> None:
    assert not looks_like_path(value)


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ("", "empty"),
        ("local@", "missing a path"),
        ("ftp://host/org/repo", "unsupported scheme"),
        ("git://host/org/repo", "unsupported scheme"),
    ],
)
def test_bad_references(raw, fragment) -> None:
    with pytest.raises(RefParseError, match=fragment):
        parse_ref(raw)


def test_ref_override_pins() -> None:
    assert parse_ref("acme/tools", ref_override="v1").ref == "v1"
    assert parse_ref("local@x", ref_override="abc").ref == "abc"


def test_anchor_local_resolves_against_the_scope_root(tmp_path) -> None:
    ref = anchor_local(parse_ref("local@../shared/ext"), tmp_path / "proj")
    assert ref.local_path == tmp_path / "proj" / "../shared/ext"
    absolute = parse_ref(f"local@{tmp_path}/ext")
    assert anchor_local(absolute, Path("/elsewhere")) is absolute
    remote = parse_ref("acme/tools")
    assert anchor_local(remote, tmp_path) is remote


def test_first_party_is_trusted_without_a_prompt() -> None:
    assert confirm_trust(parse_ref("soc2"), auto_approve=False) is True


def test_third_party_needs_consent(monkeypatch) -> None:
    import click

    from graph_agents_cli.extension import _trust

    monkeypatch.setattr(_trust, "stdin_is_interactive", lambda: True)
    answers = iter([False, True])
    monkeypatch.setattr(click, "confirm", lambda *a, **k: next(answers))
    ref = parse_ref("acme/tools")
    assert confirm_trust(ref, auto_approve=False) is False
    assert confirm_trust(ref, auto_approve=False) is True
    assert confirm_trust(ref, auto_approve=True) is True


def test_without_a_terminal_it_never_prompts_and_says_to_pass_y(monkeypatch) -> None:
    """A prompt with no terminal aborts on EOF or blocks forever on an open pipe."""
    import click

    from graph_agents_cli.extension import _trust

    monkeypatch.setattr(_trust, "stdin_is_interactive", lambda: False)

    def no_prompt(*args, **kwargs):
        raise AssertionError("prompted without a terminal")

    monkeypatch.setattr(click, "confirm", no_prompt)
    assert confirm_trust(parse_ref("acme/tools"), auto_approve=False) is False
    assert confirm_trust(parse_ref("acme/tools"), auto_approve=True) is True
    assert confirm_trust(parse_ref("soc2"), auto_approve=False) is True


def test_trust_prompt_names_the_source() -> None:
    import click

    @click.command()
    def ask() -> None:
        click.echo(str(confirm_trust(parse_ref("local@../x"), auto_approve=False)))

    result = CliRunner().invoke(ask, input="n\n")
    assert "local@../x" in result.output and "third-party" in result.output
    assert result.output.strip().endswith("False")
