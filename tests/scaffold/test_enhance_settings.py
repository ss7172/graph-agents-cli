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

"""``scaffold enhance --runtime / --model-provider``: the result matches a fresh create.

A runtime or provider change is structural: the manifest's model and
``secrets.keys``, the chart values and ``.env.example`` all follow from it. The
enhanced project must end up where ``create`` with the new settings would put
it, keep what the developer changed, and say what is left to do by hand.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml
from click.testing import CliRunner

from graph_agents_cli.scaffold.commands import enhance as enhance_mod
from graph_agents_cli.scaffold.commands.enhance import enhance
from graph_agents_cli.scaffold.utils.manifest import recompute_secret_keys
from graph_agents_cli.scaffold.utils.merge3 import merge3_checked, merge3_text

from .conftest import CreateRunner, read_manifest

STAGING = "deployment/helm/mini/values-staging.yaml"


def _enhance(project: pathlib.Path, monkeypatch: pytest.MonkeyPatch, *args: str):
    monkeypatch.chdir(project)
    return CliRunner().invoke(
        enhance, [*args, "-y", "--skip-checks", "--skip-deps"], catch_exceptions=False
    )


def _fresh(run_create: CreateRunner, name: str, *args: str) -> pathlib.Path:
    result, project = run_create(*args, name=name)
    assert result.exit_code == 0, result.output
    return project


def _set_keys(project: pathlib.Path, keys: list[str]) -> None:
    path = project / "graph-agents-cli-manifest.yaml"
    data = yaml.safe_load(path.read_text())
    data["secrets"]["keys"] = keys
    path.write_text(yaml.safe_dump(data, sort_keys=False))


# ---------------------------------------------------------------------------
# runtime
# ---------------------------------------------------------------------------


def test_runtime_change_matches_a_fresh_create(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _fresh(run_create, "agent")
    fresh = _fresh(run_create, "fresh", "--runtime", "langgraph-server")
    # Same project name, so compare against a fresh one rendered in another directory.
    fresh_staging = (fresh / STAGING).read_text()
    assert "redis:" in fresh_staging and "redis:" not in (project / STAGING).read_text()

    result = _enhance(project, monkeypatch, "--runtime", "langgraph-server")
    assert result.exit_code == 0, result.output

    assert (project / STAGING).read_text() == fresh_staging
    manifest = read_manifest(project)
    assert manifest["create_params"]["runtime"] == "langgraph-server"
    assert manifest["secrets"]["keys"] == read_manifest(fresh)["secrets"]["keys"]
    assert "DATABASE_URI" in manifest["secrets"]["keys"]
    assert "POSTGRES_DSN" not in manifest["secrets"]["keys"]
    out = result.output
    assert "Recomputed for the new settings" in out
    assert "runtime: fastapi -> langgraph-server" in out
    assert "+DATABASE_URI" in out and "-POSTGRES_DSN" in out
    assert "Left for you" in out
    assert "reads DATABASE_URI and REDIS_URI in staging and prod" in out
    assert "graph-agents-cli secrets apply --env <env>" in out


def test_runtime_change_keeps_user_keys_and_user_removals(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _fresh(run_create, "agent")
    keys = read_manifest(project)["secrets"]["keys"]
    # The developer added a key and dropped LangSmith.
    _set_keys(project, [k for k in keys if k != "LANGSMITH_API_KEY"] + ["CRM_TOKEN"])

    result = _enhance(project, monkeypatch, "--runtime", "langgraph-server")
    assert result.exit_code == 0, result.output
    assert read_manifest(project)["secrets"]["keys"] == [
        "OPENAI_API_KEY",
        "JUDGE_API_KEY",
        "DATABASE_URI",
        "REDIS_URI",
        "API_KEY",
        "CRM_TOKEN",
    ]


def test_edited_values_file_gets_the_template_change_merged_in(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _fresh(run_create, "agent")
    staging = project / STAGING
    # An argocd tag bump: the developer's (or CI's) edit, far from the template's change.
    staging.write_text(staging.read_text().replace("tag: latest", "tag: abc1234"))

    result = _enhance(project, monkeypatch, "--runtime", "langgraph-server")
    assert result.exit_code == 0, result.output
    text = staging.read_text()
    assert "tag: abc1234" in text
    assert "redis:\n  enabled: false\n" in text
    assert "Will merge the template's change into your edited config" in result.output
    assert "Merged into your edits: 1 files" in result.output


def test_overlapping_edit_is_left_alone_and_reported_with_the_change(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _fresh(run_create, "agent")
    staging = project / STAGING
    mine = staging.read_text().replace(
        "postgresql:\n  enabled: false\n",
        "postgresql:\n  enabled: false\nredis:\n  enabled: true  # mine\n",
    )
    staging.write_text(mine)

    result = _enhance(project, monkeypatch, "--runtime", "langgraph-server")
    assert result.exit_code == 0, result.output
    assert staging.read_text() == mine  # never a guess, never two redis: keys
    out = result.output
    assert f"{STAGING}: left as it is, because you changed the keys" in out
    assert "redis: you set {enabled: true}; the new settings set {enabled: false}" in out


def test_dry_run_changes_nothing_and_previews_the_merge(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _fresh(run_create, "agent")
    staging = project / STAGING
    staging.write_text(staging.read_text().replace("tag: latest", "tag: abc1234"))
    before = {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}

    result = _enhance(project, monkeypatch, "--runtime", "langgraph-server", "--dry-run")
    assert result.exit_code == 0, result.output
    assert {p: p.read_bytes() for p in project.rglob("*") if p.is_file()} == before
    assert "Will merge the template's change into your edited config" in result.output
    assert "secrets.keys: +DATABASE_URI, +REDIS_URI, -POSTGRES_DSN" in result.output


def test_force_path_reconciles_like_the_smart_merge(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _fresh(run_create, "agent")
    fresh = _fresh(run_create, "fresh", "--runtime", "langgraph-server")

    def fake_execute(args, project_version, use_different_version):
        monkeypatch.setenv(enhance_mod._ENV_USING_SAVED_CONFIG, "1")
        try:
            sub = CliRunner().invoke(enhance, args[2:], catch_exceptions=False)
        finally:
            monkeypatch.delenv(enhance_mod._ENV_USING_SAVED_CONFIG, raising=False)
        assert sub.exit_code == 0, sub.output
        assert "Left for you" in sub.output
        return True

    monkeypatch.setattr(enhance_mod, "_execute_with_saved_config", fake_execute)
    result = _enhance(project, monkeypatch, "--runtime", "langgraph-server", "--force")
    assert result.exit_code == 0, result.output
    assert (project / STAGING).read_text() == (fresh / STAGING).read_text()
    assert read_manifest(project)["secrets"]["keys"] == read_manifest(fresh)["secrets"]["keys"]


def test_force_path_keeps_the_recorded_project_name_in_a_differently_named_checkout(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The in-folder render used to take the directory name, renaming the project
    (and rendering a second chart under deployment/helm/<directory>)."""
    project = _fresh(run_create, "agent")
    checkout = project.with_name("my-checkout")
    project.rename(checkout)

    def fake_execute(args, project_version, use_different_version):
        monkeypatch.setenv(enhance_mod._ENV_USING_SAVED_CONFIG, "1")
        try:
            sub = CliRunner().invoke(enhance, args[2:], catch_exceptions=False)
        finally:
            monkeypatch.delenv(enhance_mod._ENV_USING_SAVED_CONFIG, raising=False)
        assert sub.exit_code == 0, sub.output
        assert "Using the project name recorded in graph-agents-cli-manifest.yaml: agent" in (
            sub.output
        )
        return True

    monkeypatch.setattr(enhance_mod, "_execute_with_saved_config", fake_execute)
    result = _enhance(checkout, monkeypatch, "--runtime", "langgraph-server", "--force")
    assert result.exit_code == 0, result.output
    assert read_manifest(checkout)["name"] == "agent"
    assert "my-checkout" not in (checkout / "deployment/helm/mini/values.yaml").read_text()


# ---------------------------------------------------------------------------
# model provider
# ---------------------------------------------------------------------------


def test_provider_change_takes_the_new_default_model(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _fresh(run_create, "agent")
    fresh = _fresh(run_create, "fresh", "--model-provider", "anthropic")
    (project / ".env").write_text("MODEL_PROVIDER=openai\nMODEL_NAME=gpt-5-mini\n")

    result = _enhance(project, monkeypatch, "--model-provider", "anthropic")
    assert result.exit_code == 0, result.output
    manifest = read_manifest(project)
    assert manifest["create_params"]["model_provider"] == "anthropic"
    assert manifest["create_params"]["model"] == "claude-sonnet-5"
    assert manifest["secrets"]["keys"] == read_manifest(fresh)["secrets"]["keys"]
    assert (project / ".env.example").read_text() == (fresh / ".env.example").read_text()
    # .env is the developer's: never edited, but the stale values are pointed out.
    assert (project / ".env").read_text() == "MODEL_PROVIDER=openai\nMODEL_NAME=gpt-5-mini\n"
    out = result.output
    assert "model: gpt-5-mini -> claude-sonnet-5" in out
    assert ".env still sets MODEL_PROVIDER=openai MODEL_NAME=gpt-5-mini" in out
    assert "Set ANTHROPIC_API_KEY in .env" in out


def test_provider_change_refuses_to_carry_a_chosen_model(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _fresh(run_create, "agent", "--model", "gpt-5")
    before = (project / "graph-agents-cli-manifest.yaml").read_text()
    monkeypatch.chdir(project)
    result = CliRunner().invoke(
        enhance, ["--model-provider", "anthropic", "-y", "--skip-checks", "--skip-deps"]
    )
    assert result.exit_code == 2, result.output
    assert "recorded model 'gpt-5' was chosen for openai" in result.output
    assert "Pass --model <name>" in result.output
    assert (project / "graph-agents-cli-manifest.yaml").read_text() == before

    result = _enhance(
        project, monkeypatch, "--model-provider", "anthropic", "--model", "claude-opus-5"
    )
    assert result.exit_code == 0, result.output
    assert read_manifest(project)["create_params"]["model"] == "claude-opus-5"


def test_replayed_args_do_not_carry_the_old_providers_model(
    run_create: CreateRunner,
) -> None:
    from graph_agents_cli._project import read_project_config

    _result, project = run_create()
    config = read_project_config(str(project))
    args = enhance_mod.build_args_from_config(
        config, auto_approve=True, cli_overrides={"model_provider": "gemini"}
    )
    assert args[args.index("--model") + 1] == "gemini-3.8-flash"
    assert args.count("--model") == 1
    args = enhance_mod.build_args_from_config(
        config, cli_overrides={"model_provider": "gemini", "model": "gemini-pro"}
    )
    assert args[args.index("--model") + 1] == "gemini-pro"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("recorded", "expected"),
    [
        # Untouched list: exactly the new defaults.
        (["A_KEY", "J", "POSTGRES_DSN", "API_KEY"], ["A_KEY", "J", "DB", "API_KEY"]),
        # A user key is kept after the defaults; a removed default stays removed.
        (["A_KEY", "POSTGRES_DSN", "API_KEY", "MINE"], ["A_KEY", "DB", "API_KEY", "MINE"]),
    ],
)
def test_recompute_secret_keys(recorded, expected) -> None:
    old = ["A_KEY", "J", "POSTGRES_DSN", "API_KEY"]
    new = ["A_KEY", "J", "DB", "API_KEY"]
    assert recompute_secret_keys(recorded, old, new) == expected


def test_merge3_applies_separate_edits_and_refuses_overlaps() -> None:
    base = "a\nb\nc\nd\n"
    assert merge3_text(base, "A\nb\nc\nd\n", "a\nb\nc\nD\n") == "A\nb\nc\nD\n"
    assert merge3_text(base, "a\nX\nc\nd\n", "a\nY\nc\nd\n") is None
    assert merge3_text(base, base, "a\nb\nc\nd\ne\n") == "a\nb\nc\nd\ne\n"


def test_checked_merge_refuses_a_line_merge_that_changes_the_meaning() -> None:
    """Two blocks inserted either side of a shared line merge as text into two
    `redis:` keys; the parsed check turns that into a conflict."""
    base = "postgresql:\n  enabled: true\ngateway:\n  enabled: false\n"
    ours = base.replace("true\n", "true\nredis:\n  enabled: false  # mine\n")
    theirs = base.replace("true\n", "true\nredis:\n  enabled: true\n")
    assert merge3_text(base, ours, theirs) is not None  # the line merge alone is fooled
    assert merge3_checked(base, ours, theirs, "values-dev.yaml") is None
    env_base = "A=1\nB=1\n"
    assert merge3_checked(env_base, "A=1\nB=1\nX=2\n", "A=1\nB=1\nX=3\n", ".env.example") is None
    merged = merge3_checked(env_base, "A=2\nB=1\n", "A=1\nB=1\nC=3\n", ".env.example")
    assert merged == "A=2\nB=1\nC=3\n"


def test_dry_run_errors_exit_non_zero(tmp_path: pathlib.Path, monkeypatch, run_create) -> None:
    """They used to print 'Error: ...' and exit 0, so scripts took them for success."""
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(enhance, ["--cd", "argocd", "--dry-run", "-y"])
    assert result.exit_code == 3, result.output
    assert "no graph-agents-cli-manifest.yaml" in result.output
    result = CliRunner().invoke(enhance, ["--dry-run", "--force", "-y"])
    assert result.exit_code == 2, result.output

    project = _fresh(run_create, "agent")
    monkeypatch.chdir(project)
    result = CliRunner().invoke(enhance, ["--dry-run", "-y", "--skip-checks"])
    assert result.exit_code == 2, result.output
    assert "--dry-run requires specifying what to change" in result.output
