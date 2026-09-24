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

from __future__ import annotations

import pytest
from click.testing import CliRunner

from graph_agents_cli.dev import cmd_lint
from graph_agents_cli.dev.cmd_install import cmd_install
from graph_agents_cli.dev.cmd_lint import cmd_lint as lint
from graph_agents_cli.extension import _sync

# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


def test_install_runs_uv_sync_and_extension_sync(fake_project, recorded_runs, monkeypatch):
    synced = []
    monkeypatch.setattr(_sync, "sync_extensions", lambda root: synced.append(root) or [])
    result = CliRunner().invoke(cmd_install, [], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert recorded_runs.commands == [["uv", "sync"]]
    assert synced == [fake_project.root]


def test_install_locked_and_clean(fake_project, recorded_runs, monkeypatch):
    monkeypatch.setattr(_sync, "sync_extensions", lambda root: [])
    venv = fake_project.root / ".venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text("x")
    result = CliRunner().invoke(cmd_install, ["--locked", "--clean"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert recorded_runs.commands == [["uv", "sync", "--locked"]]
    assert not venv.exists()


def test_install_reports_uv_failure(fake_project, recorded_runs, monkeypatch):
    monkeypatch.setattr(_sync, "sync_extensions", lambda root: [])
    recorded_runs.returncodes = [1]
    result = CliRunner().invoke(cmd_install, [])
    assert result.exit_code == 1
    assert "Failed to install dependencies" in result.output


# ---------------------------------------------------------------------------
# lint
# ---------------------------------------------------------------------------


def test_lint_runs_ruff_check_and_format_check_then_policy(
    fake_project, recorded_runs, monkeypatch
):
    policy_calls = []
    monkeypatch.setattr(
        cmd_lint,
        "run_policy_check",
        lambda root, agent_dir, **kw: policy_calls.append((root, agent_dir, kw)) or 0,
    )
    result = CliRunner().invoke(lint, [], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert recorded_runs.commands == [
        ["uv", "run", "ruff", "check", "."],
        ["uv", "run", "ruff", "format", ".", "--check"],
    ]
    assert policy_calls == [
        (
            fake_project.root,
            "app",
            {"policy_file": "api-policy.yaml", "runtime": "fastapi", "policy_declared": False},
        )
    ]


def test_lint_fix(fake_project, recorded_runs, monkeypatch):
    monkeypatch.setattr(cmd_lint, "run_policy_check", lambda root, agent_dir, **kw: 0)
    result = CliRunner().invoke(lint, ["--fix"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert recorded_runs.commands == [
        ["uv", "run", "ruff", "check", ".", "--fix"],
        ["uv", "run", "ruff", "format", "."],
    ]


def test_lint_policy_only_skips_ruff_and_fails_on_violation(
    fake_project, recorded_runs, monkeypatch
):
    monkeypatch.setattr(cmd_lint, "run_policy_check", lambda root, agent_dir, **kw: 2)
    result = CliRunner().invoke(lint, ["--policy-only"])
    assert result.exit_code == 1
    assert recorded_runs.commands == []
    assert "2 violation(s)" in result.output


def test_lint_ruff_failure_stops_before_policy(fake_project, recorded_runs, monkeypatch):
    called = []
    monkeypatch.setattr(
        cmd_lint, "run_policy_check", lambda root, agent_dir, **kw: called.append(1) or 0
    )
    recorded_runs.returncodes = [1]
    result = CliRunner().invoke(lint, [])
    assert result.exit_code == 1
    assert "Ruff check failed" in result.output
    assert called == []


def test_lint_policy_only_end_to_end_with_real_check(fake_project, recorded_runs):
    tools = fake_project.root / "app" / "tools"
    tools.mkdir()
    (tools / "incidents.py").write_text(
        'API_CALLS = [{"api": "incidents", "method": "DELETE", "operation_id": "purgeIncident"}]\n'
    )
    (fake_project.root / "api-policy.yaml").write_text(
        "apis:\n  incidents:\n    base_url_env: I\n    auth: none\n    allowed_methods: [GET, POST]\n"
    )
    result = CliRunner().invoke(lint, ["--policy-only"])
    assert result.exit_code == 1
    assert "purgeIncident" in result.output
    assert "denied" in result.output
    assert "graph-agents-cli api access incidents custom --methods GET,POST,DELETE" in (
        result.output.replace("\n", " ")
    )


def test_lint_exits_3_on_an_invalid_policy_file(fake_project, recorded_runs):
    (fake_project.root / "api-policy.yaml").write_text(
        "apis:\n  incidents:\n    base_url_env: I\n    auth: none\n    alowed_methods: [GET]\n"
    )
    result = CliRunner().invoke(lint, ["--policy-only"])
    assert result.exit_code == 3, result.output
    assert "api-policy.yaml is invalid (see above)" in result.output
    assert "configuration error" in result.output


@pytest.mark.parametrize(
    ("legacy_file", "content"),
    [
        ("product-policy.yaml", "product_api:\n  allowed_methods: [GET]\n"),
        ("graph-agents-cli-manifest.yaml", "name: my-agent\nproduct_api:\n  policy_file: p.yaml\n"),
    ],
)
def test_lint_stops_with_a_migration_message_on_the_retired_policy(
    fake_project, recorded_runs, legacy_file, content
):
    (fake_project.root / legacy_file).write_text(content)
    result = CliRunner().invoke(lint, [])
    assert result.exit_code == 3
    assert recorded_runs.commands == []  # nothing ran
    assert "Migrate it to api-policy.yaml" in result.output
    assert "API_CALLS" in result.output and "allowed_methods" in result.output
