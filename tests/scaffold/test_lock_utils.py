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

"""Lock generation: pyproject rendering per runtime and the generate_locks entry point."""

from __future__ import annotations

import pathlib
import subprocess
import tomllib

import pytest
from click.testing import CliRunner

from graph_agents_cli.scaffold.utils import generate_locks, lock_utils
from graph_agents_cli.scaffold.utils.lock_utils import (
    LOCK_FILENAMES,
    lock_filename,
    render_pyproject,
    replace_lock_project_name,
)

from .conftest import MINI_AGENT_DIR

FIXTURE_PYPROJECT = MINI_AGENT_DIR / "pyproject.toml"


def test_lock_filenames() -> None:
    assert lock_filename("fastapi") == "uv-fastapi.lock"
    assert lock_filename("langgraph-server") == "uv-langgraph-server.lock"
    assert LOCK_FILENAMES == {
        "fastapi": "uv-fastapi.lock",
        "langgraph-server": "uv-langgraph-server.lock",
    }
    with pytest.raises(ValueError):
        lock_filename("go")


@pytest.mark.parametrize(
    ("runtime", "present", "absent"),
    [
        ("fastapi", ("fastapi>=0.115.8,<1.0.0", "uvicorn>=0.34,<1.0"), ("langgraph-cli",)),
        ("langgraph-server", ("langgraph-cli[inmem]>=0.2.10,<1.0.0",), ("fastapi>=", "uvicorn")),
    ],
)
def test_render_pyproject_per_runtime(
    runtime: str, present: tuple[str, ...], absent: tuple[str, ...]
) -> None:
    rendered = render_pyproject(FIXTURE_PYPROJECT, runtime)
    data = tomllib.loads(rendered)
    assert data["project"]["name"] == lock_utils.LOCK_PROJECT_NAME
    deps = data["project"]["dependencies"]
    for dep in present:
        assert dep in deps
    for dep in absent:
        assert not any(dep in d for d in deps)
    assert "langgraph>=1.0.0,<2.0.0" in deps
    assert "langchain-openai>=1.0,<2.0" in deps
    assert data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["app"]


def test_render_pyproject_overrides() -> None:
    rendered = render_pyproject(FIXTURE_PYPROJECT, "fastapi", model_provider="anthropic")
    assert "langchain-anthropic" in rendered
    assert "langchain-openai" not in rendered


def test_replace_lock_project_name() -> None:
    assert (
        replace_lock_project_name('name = "{{cookiecutter.project_name}}"', "svc") == 'name = "svc"'
    )


def test_generate_locks_writes_one_lock_per_runtime(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import graph_agents_cli._runner as runner

    seen: list[tuple[list[str], pathlib.Path]] = []

    def fake_run_resolved(args, *, resolve_executable=True, **kwargs):
        cwd = pathlib.Path(kwargs["cwd"])
        seen.append((list(args), cwd))
        if args[:2] == ["uv", "lock"]:
            assert "--no-config" in args
            pyproject = tomllib.loads((cwd / "pyproject.toml").read_text())
            deps = ", ".join(pyproject["project"]["dependencies"])
            (cwd / "uv.lock").write_text(
                f'version = 1\n\n[[package]]\nname = "{lock_utils.LOCK_PROJECT_NAME}"\n# deps: {deps}\n'
            )
            return subprocess.CompletedProcess(args, 0)
        if args[:2] == ["uv", "audit"]:
            return subprocess.CompletedProcess(args, 0)
        raise AssertionError(args)

    monkeypatch.setattr(runner, "run_resolved", fake_run_resolved)
    monkeypatch.setattr(generate_locks.shutil, "which", lambda name: "/usr/bin/uv")

    out = tmp_path / "locks"
    result = CliRunner().invoke(
        generate_locks.main,
        ["--template", str(FIXTURE_PYPROJECT), "--output-dir", str(out)],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output

    fastapi_lock = (out / "uv-fastapi.lock").read_text()
    server_lock = (out / "uv-langgraph-server.lock").read_text()
    assert 'name = "{{cookiecutter.project_name}}"' in fastapi_lock
    assert lock_utils.LOCK_PROJECT_NAME not in fastapi_lock
    assert "fastapi>=0.115.8" in fastapi_lock
    assert "langgraph-cli" in server_lock
    assert "fastapi>=" not in server_lock
    assert [a[:2] for a, _ in seen] == [["uv", "lock"], ["uv", "audit"]] * 2


def test_generate_locks_single_runtime_no_audit(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import graph_agents_cli._runner as runner

    calls: list[list[str]] = []

    def fake_run_resolved(args, *, resolve_executable=True, **kwargs):
        calls.append(list(args))
        (pathlib.Path(kwargs["cwd"]) / "uv.lock").write_text("version = 1\n")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(runner, "run_resolved", fake_run_resolved)
    monkeypatch.setattr(generate_locks.shutil, "which", lambda name: "/usr/bin/uv")
    out = tmp_path / "locks"
    result = CliRunner().invoke(
        generate_locks.main,
        [
            "--template",
            str(FIXTURE_PYPROJECT),
            "--output-dir",
            str(out),
            "--runtime",
            "fastapi",
            "--no-audit",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in out.iterdir()) == ["uv-fastapi.lock"]
    assert calls == [["uv", "lock", "--no-config", "--default-index", "https://pypi.org/simple"]]


def test_default_paths_point_at_the_langgraph_template() -> None:
    assert lock_utils.langgraph_template_dir().name == "langgraph"
    assert lock_utils.langgraph_template_dir().parent.name == "agents"
