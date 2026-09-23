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

"""``scaffold enhance --runtime / --model-provider`` on a project whose chart or image was edited.

The chart's values.yaml decides the runtime wiring and the model the pod calls,
and the Dockerfile decides what the image runs. A developer edit anywhere in
them (a replica count, an extra variable) must not leave them on the old
settings: the change is applied key by key around the edits, a key the
developer changed too is reported, and whatever still disagrees with the new
settings (or an edited Dockerfile) is a required step that makes enhance exit 1.
"""

from __future__ import annotations

import pathlib
import subprocess

import click
import pytest
import yaml
from click.testing import CliRunner

from graph_agents_cli.scaffold.commands import enhance as enhance_mod
from graph_agents_cli.scaffold.commands.enhance import enhance

from .conftest import CreateRunner, read_manifest

VALUES = "deployment/helm/mini/values.yaml"
PROD = "deployment/helm/mini/values-prod.yaml"

# The keys of the real chart's values.yaml that a runtime or provider change decides.
VALUES_TEMPLATE = """\
# Replicas (the HPA owns them when enabled).
replicaCount: 1

# fastapi -> POSTGRES_DSN, langgraph-server -> DATABASE_URI + REDIS_URI.
runtime: {{cookiecutter.runtime}}

env:
  APP_ENV: prod
  MODEL_PROVIDER: {{cookiecutter.model_provider}}
  MODEL_NAME: {{cookiecutter.model}}
{%- if cookiecutter.runtime == 'fastapi' %}
  CHECKPOINTER: postgres
{%- endif %}
  AUTH_POLICY: {{cookiecutter.auth_policy}}
  PORT: "8000"
"""


@pytest.fixture(autouse=True)
def chart_with_settings(scaffold_root: pathlib.Path) -> None:
    path = scaffold_root / "agents" / "mini_agent" / "deployment" / "helm" / "mini" / "values.yaml"
    path.write_text(VALUES_TEMPLATE)


def _enhance(project: pathlib.Path, monkeypatch: pytest.MonkeyPatch, *args: str):
    monkeypatch.chdir(project)
    return CliRunner().invoke(
        enhance, [*args, "-y", "--skip-checks", "--skip-deps"], catch_exceptions=False
    )


def _create(run_create: CreateRunner, name: str, *args: str) -> pathlib.Path:
    result, project = run_create(*args, name=name)
    assert result.exit_code == 0, result.output
    return project


def _replace(path: pathlib.Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, text
    path.write_text(text.replace(old, new, 1))


def _tree(project: pathlib.Path) -> dict[pathlib.Path, bytes]:
    return {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}


def test_edited_values_yaml_gets_the_runtime_and_provider_change(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verifier's case: an unrelated edit used to keep the whole file on the old settings."""
    project = _create(run_create, "agent")
    fresh = _create(
        run_create, "fresh", "--runtime", "langgraph-server", "--model-provider", "anthropic"
    )
    values = project / VALUES
    _replace(values, "replicaCount: 1", "replicaCount: 3")
    # Right next to a line the change removes: the line merge alone gives up here.
    _replace(values, "  CHECKPOINTER: postgres\n", "  CHECKPOINTER: postgres\n  MINE: x\n")

    result = _enhance(
        project, monkeypatch, "--runtime", "langgraph-server", "--model-provider", "anthropic"
    )
    assert result.exit_code == 0, result.output
    expected = (fresh / VALUES).read_text().replace("replicaCount: 1", "replicaCount: 3")
    expected = expected.replace("claude-sonnet-5\n", "claude-sonnet-5\n  MINE: x\n")
    assert values.read_text() == expected
    assert "Will merge the template's change into your edited config" in result.output
    assert "(required)" not in result.output


def test_a_chart_model_chosen_for_the_old_provider_is_a_required_step(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _create(run_create, "agent")
    values = project / VALUES
    _replace(values, "MODEL_NAME: gpt-5-mini", "MODEL_NAME: gpt-4.1")

    result = _enhance(project, monkeypatch, "--model-provider", "anthropic")
    assert result.exit_code == 1, result.output
    data = yaml.safe_load(values.read_text())
    assert data["env"]["MODEL_PROVIDER"] == "anthropic"  # applied around the edit
    assert data["env"]["MODEL_NAME"] == "gpt-4.1"  # the developer's, kept
    assert read_manifest(project)["create_params"]["model_provider"] == "anthropic"
    out = " ".join(result.output.split())
    assert "env.MODEL_NAME: you set 'gpt-4.1'; the new settings set 'claude-sonnet-5'" in out
    assert (
        f"(required) {VALUES}: env.MODEL_NAME is 'gpt-4.1': set it to 'claude-sonnet-5' or "
        "another anthropic model (a model chosen for openai does not run on anthropic)"
    ) in out
    assert "1 item(s) marked (required) above must be done by hand" in out
    assert "Enhancement complete" not in out


def test_an_environment_file_that_still_names_the_old_provider_is_a_required_step(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _create(run_create, "agent")
    _replace(project / PROD, "  APP_ENV: prod\n", "  APP_ENV: prod\n  MODEL_PROVIDER: openai\n")

    result = _enhance(project, monkeypatch, "--model-provider", "gemini")
    assert result.exit_code == 1, result.output
    out = " ".join(result.output.split())
    assert f"(required) {PROD}: env.MODEL_PROVIDER is 'openai': set it to 'gemini'" in out
    # values.yaml itself was untouched by the developer and is simply updated.
    assert f"(required) {VALUES}" not in out
    assert yaml.safe_load((project / VALUES).read_text())["env"]["MODEL_PROVIDER"] == "gemini"


def test_a_runtime_change_back_to_fastapi_needs_the_postgres_checkpointer(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _create(run_create, "agent", "--runtime", "langgraph-server")
    values = project / VALUES
    # The developer pinned their own CHECKPOINTER line where the template adds one.
    _replace(values, "  AUTH_POLICY", "  CHECKPOINTER: memory\n  AUTH_POLICY")

    result = _enhance(project, monkeypatch, "--runtime", "fastapi")
    assert result.exit_code == 1, result.output
    data = yaml.safe_load(values.read_text())
    assert data["runtime"] == "fastapi"
    assert data["env"]["CHECKPOINTER"] == "memory"
    out = " ".join(result.output.split())
    assert f"(required) {VALUES}: env.CHECKPOINTER is 'memory': set it to 'postgres'" in out


def test_an_edited_dockerfile_is_kept_with_the_new_runtimes_version_beside_it(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _create(run_create, "agent")
    fresh = _create(run_create, "fresh", "--runtime", "langgraph-server")
    dockerfile = project / "Dockerfile"
    mine = dockerfile.read_text() + "RUN echo my-step\n"
    dockerfile.write_text(mine)

    result = _enhance(project, monkeypatch, "--runtime", "langgraph-server")
    assert result.exit_code == 1, result.output
    assert dockerfile.read_text() == mine
    assert (project / "Dockerfile.new").read_text() == (fresh / "Dockerfile").read_text()
    out = " ".join(result.output.split())
    assert "(required) Dockerfile: your edited version was kept" in out
    assert "was written to Dockerfile.new" in out
    # The chart and the manifest did move to the new runtime.
    assert yaml.safe_load((project / VALUES).read_text())["runtime"] == "langgraph-server"
    assert read_manifest(project)["create_params"]["runtime"] == "langgraph-server"


def test_prefer_new_takes_the_new_dockerfile_and_succeeds(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _create(run_create, "agent")
    fresh = _create(run_create, "fresh", "--runtime", "langgraph-server")
    (project / "Dockerfile").write_text((project / "Dockerfile").read_text() + "RUN true\n")

    result = _enhance(project, monkeypatch, "--runtime", "langgraph-server", "--prefer-new")
    assert result.exit_code == 0, result.output
    assert (project / "Dockerfile").read_text() == (fresh / "Dockerfile").read_text()
    assert not (project / "Dockerfile.new").exists()


def test_dry_run_lists_the_required_steps_changes_nothing_and_exits_zero(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _create(run_create, "agent")
    _replace(project / VALUES, "MODEL_NAME: gpt-5-mini", "MODEL_NAME: gpt-4.1")
    (project / "Dockerfile").write_text((project / "Dockerfile").read_text() + "RUN true\n")
    before = _tree(project)

    result = _enhance(
        project,
        monkeypatch,
        "--runtime",
        "langgraph-server",
        "--model-provider",
        "anthropic",
        "--dry-run",
    )
    assert result.exit_code == 0, result.output
    assert _tree(project) == before
    out = " ".join(result.output.split())
    assert "Left for you after applying" in out
    assert "(required) Dockerfile" in out and "will be written to Dockerfile.new" in out
    assert "env.MODEL_NAME: you set 'gpt-4.1'" in out


@pytest.mark.parametrize(
    ("current", "old", "new", "action"),
    [
        ("a: 1\n", "a: 1\n", None, "removed"),  # the target left kubernetes: goes with the chart
        ("a: 2\n", "a: 1\n", None, "conflict"),  # edited: kept, and listed
        (None, "a: 1\n", None, "skip"),  # already gone: nothing to say
        (None, None, "a: 1\n", "new"),  # the target moved to kubernetes
        (None, "a: 1\n", "a: 2\n", "new"),  # deleted by the developer: re-added like scaffolding
    ],
)
def test_chart_values_yaml_keeps_scaffolding_semantics_for_added_and_removed_files(
    tmp_path: pathlib.Path, current, old, new, action
) -> None:
    from graph_agents_cli.scaffold.utils.upgrade import three_way_compare

    dirs = {name: tmp_path / name for name in ("project", "old", "new")}
    for name, text in (("project", current), ("old", old), ("new", new)):
        if text is not None:
            path = dirs[name] / "deployment/helm/k/values.yaml"
            path.parent.mkdir(parents=True)
            path.write_text(text)
    result = three_way_compare(
        "deployment/helm/k/values.yaml",
        dirs["project"],
        dirs["old"],
        dirs["new"],
        merge_config=True,
    )
    assert result.action == action
    assert result.followup is None


def test_a_failed_same_version_replay_keeps_its_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--force replays the saved config in a subprocess of this same CLI: when it stops
    (for example with required steps left, exit 1) the in-process fallback must not run
    again and report success over it."""

    def failing(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(enhance_mod, "run_resolved", failing)
    with pytest.raises(click.exceptions.Exit) as info:
        enhance_mod._execute_with_saved_config(["scaffold", "enhance"], None, False)
    assert info.value.exit_code == 1
