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

"""Where the CLI installs from (``install_spec``) and the GitHub-release update check."""

from __future__ import annotations

import shlex
from typing import Any

import pytest

from graph_agents_cli.scaffold.utils import version as v

REPO = "git+https://github.com/ss7172/graph-agents-cli"


@pytest.fixture(autouse=True)
def no_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(v.INSTALL_SPEC_ENV, raising=False)


@pytest.mark.parametrize(
    ("version", "expected"),
    [("0.2.0", f"{REPO}@v0.2.0"), (None, REPO), ("0.0.0", REPO), ("", REPO)],
)
def test_install_spec_pins_the_release_tag(version: str | None, expected: str) -> None:
    assert v.install_spec(version) == expected


def test_the_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(v.INSTALL_SPEC_ENV, " graph-agents-cli==0.2.0 ")
    assert v.install_spec("0.1.0") == "graph-agents-cli==0.2.0"
    assert v.requirement(extras="a2a") == "graph-agents-cli[a2a]==0.2.0"
    assert v.cli_install_spec() == "graph-agents-cli==0.2.0"


def test_requirement_with_extras_for_a_url_spec() -> None:
    assert v.requirement(f"{REPO}@v0.2.0", "a2a") == f"graph-agents-cli[a2a] @ {REPO}@v0.2.0"
    assert v.requirement(f"{REPO}@v0.2.0") == f"{REPO}@v0.2.0"


def test_install_command_is_a_copyable_shell_line() -> None:
    line = v.install_command("langsmith", version="0.2.0")
    assert shlex.split(line) == [
        "uv",
        "tool",
        "install",
        "--force",
        f"graph-agents-cli[langsmith] @ {REPO}@v0.2.0",
    ]


def test_cli_install_spec_pins_the_running_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(v, "get_current_version", lambda: "0.3.1")
    assert v.cli_install_spec() == f"{REPO}@v0.3.1"
    monkeypatch.setattr(v, "get_current_version", lambda: v.UNKNOWN_VERSION)
    assert v.cli_install_spec() == REPO


class _Response:
    def __init__(self, status: int, payload: Any) -> None:
        self.status_code = status
        self._payload = payload

    def json(self) -> Any:
        return self._payload


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (200, {"tag_name": "v0.2.0"}, "0.2.0"),
        (200, {"tag_name": "1.4.2"}, "1.4.2"),
        (200, {"tag_name": "nightly"}, v.UNKNOWN_VERSION),
        (200, {}, v.UNKNOWN_VERSION),
        (404, {"message": "Not Found"}, v.UNKNOWN_VERSION),  # no release yet
        (403, {"message": "rate limited"}, v.UNKNOWN_VERSION),
    ],
)
def test_latest_version_comes_from_github_releases(
    monkeypatch: pytest.MonkeyPatch, status: int, payload: Any, expected: str
) -> None:
    import requests

    seen: list[str] = []

    def fake_get(url: str, **kwargs: Any) -> _Response:
        seen.append(url)
        return _Response(status, payload)

    monkeypatch.setattr(requests, "get", fake_get)
    assert v.get_latest_version() == expected
    assert seen == ["https://api.github.com/repos/ss7172/graph-agents-cli/releases/latest"]


def test_latest_version_is_unknown_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    import requests

    def offline(url: str, **kwargs: Any) -> _Response:
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(requests, "get", offline)
    assert v.get_latest_version() == v.UNKNOWN_VERSION


def test_update_message_suggests_the_update_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(v.NO_UPDATE_CHECK_ENV, raising=False)
    monkeypatch.setattr(v, "_update_check_is_due", lambda: True)
    monkeypatch.setattr(v, "_record_update_check", lambda: None)
    monkeypatch.setattr(v, "check_for_updates", lambda: (True, "0.1.0", "0.2.0"))
    v.display_update_message()
    err = capsys.readouterr().err
    assert "0.1.0 → 0.2.0" in err
    assert "graph-agents-cli update" in err and f"{REPO}@v0.2.0" in err
