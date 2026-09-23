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

"""`eval run`: generate + grade chaining, override parity, worst exit code."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from conftest import DATASET, FakeJudge, good_traces, ok_stream, read_results, sse

from graph_agents_cli.eval import _paths
from graph_agents_cli.eval.cmd_generate import cmd_generate
from graph_agents_cli.eval.cmd_run import cmd_run
from graph_agents_cli.eval.dataset import dataset_hash

STREAMS = {
    "hi": ok_stream("hello there"),
    "weather in SF?": ok_stream(
        "sunny", tool_calls=[{"id": "c1", "name": "get_weather", "args": {"query": "SF"}}]
    ),
    "say nothing special": ok_stream("ok"),
}


def _override(name: str, vector: tuple[str, ...] = ("python", "ext.py")) -> SimpleNamespace:
    return SimpleNamespace(
        contribution=SimpleNamespace(name=name, run=vector, kind="override", description="ext"),
        extension_name="my-ext",
        scope="project",
        extension_root=Path("/ext"),
        blocked_requires=None,
    )


@pytest.fixture
def overrides(monkeypatch: pytest.MonkeyPatch):
    """Install fake `installed_override` and `run_extension_command`; returns the recorder."""
    state = SimpleNamespace(installed={}, calls=[], codes={})

    def installed_override(dotted: str):
        return state.installed.get(dotted)

    def run_extension_command(run_vector, extra_args, *, cwd, extension_root):
        state.calls.append(
            {"vector": tuple(run_vector), "argv": list(extra_args), "cwd": Path(cwd)}
        )
        handler = state.codes.get(tuple(run_vector))
        return handler(extra_args) if callable(handler) else (handler or 0)

    monkeypatch.setattr(
        "graph_agents_cli.extension._overrides.installed_override", installed_override
    )
    monkeypatch.setattr("graph_agents_cli._runner.run_extension_command", run_extension_command)
    return state


def _fake_generate_writing_traces(project: Path):
    def handler(argv: list[str]) -> int:
        out = Path(argv[argv.index("--output") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "dataset_hash": dataset_hash(DATASET["cases"]),
                    "dataset_paths": [_paths.DEFAULT_INPUT_DATASET],
                    "generated_at": "2026-09-22T00:00:00+00:00",
                    "traces": good_traces(),
                }
            ),
            encoding="utf-8",
        )
        return 0

    return handler


def test_run_exits_2_not_1_when_the_local_server_cannot_start(
    project: Path, runner: CliRunner, fake_judge: FakeJudge, overrides, monkeypatch
) -> None:
    """1 means "the gate failed": a broken environment must not read as a quality regression."""
    from graph_agents_cli.run import _local_server

    def cannot_start(*args, **kwargs):
        raise _local_server.ServerStartError("Local server did not become healthy within 60s.")

    monkeypatch.setattr(_local_server, "ensure_server", cannot_start)
    result = runner.invoke(cmd_run, [])
    assert result.exit_code == 2, result.output
    assert "did not become healthy" in result.output
    assert "Step 2/2" not in result.output


def test_run_builtin_generate_and_grade_exit_0(
    project: Path, runner: CliRunner, fake_chat, fake_judge: FakeJudge, overrides
) -> None:
    fake_chat(STREAMS)
    result = runner.invoke(cmd_run, ["--url", "http://agent.example"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert "Step 1/2" in result.output and "Step 2/2" in result.output
    traces = sorted(_paths.default_traces_dir(project).glob("traces_*.json"))
    assert len(traces) == 1
    results = read_results(project)
    assert results["summary"]["exit_code"] == 0
    assert results["traces_files"] == [str(traces[0])]
    assert overrides.calls == []


def test_run_returns_worst_code_generate_2_grade_2(
    project: Path, runner: CliRunner, fake_chat, fake_judge: FakeJudge, overrides
) -> None:
    streams = dict(STREAMS)
    streams["hi"] = [sse("error", {"message": "down"})]
    fake_chat(streams)
    result = runner.invoke(cmd_run, ["--url", "http://agent.example"])
    assert result.exit_code == 2, result.output
    assert read_results(project)["summary"]["error"] == 1
    assert "generate exit 2, grade exit 2" in result.output


def test_run_grade_failure_wins_over_clean_generate(
    project: Path, runner: CliRunner, fake_chat, fake_judge: FakeJudge, overrides
) -> None:
    fake_judge.scores["weather/task_success"] = 1
    fake_chat(STREAMS)
    result = runner.invoke(cmd_run, ["--url", "http://agent.example"])
    assert result.exit_code == 1, result.output


def test_run_honours_generate_override(
    project: Path, runner: CliRunner, fake_judge: FakeJudge, overrides
) -> None:
    overrides.installed["eval.generate"] = _override("eval.generate")
    overrides.codes[("python", "ext.py")] = _fake_generate_writing_traces(project)
    result = runner.invoke(
        cmd_run,
        [
            "--url",
            "http://agent.example",
            "--header",
            "X-A: 1",
            "--cookie",
            "s=1",
            "--session-token",
            "tok",
            "--concurrency",
            "9",
            "--app-name",
            "svc",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert len(overrides.calls) == 1
    call = overrides.calls[0]
    assert call["vector"] == ("python", "ext.py") and call["cwd"] == project
    argv = call["argv"]
    assert argv[:2] == ["--dataset", str((project / _paths.DEFAULT_INPUT_DATASET).resolve())]
    assert argv[2] == "--output" and argv[3].startswith(str(_paths.default_traces_dir(project)))
    assert argv[4:] == [
        "--url",
        "http://agent.example",
        "--app-name",
        "svc",
        "--concurrency",
        "9",
        "--header",
        "X-A: 1",
        "--header",
        "X-Session-Token: tok",
        "--cookie",
        "s=1",
    ]
    # The deprecated alias warns once and reaches the override as a plain --header.
    assert result.output.count("--session-token is deprecated") == 1
    # The built-in grade ran on the override's traces.
    assert read_results(project)["summary"]["passed"] == 3
    assert len(fake_judge.calls) == 1


def test_run_forwards_non_default_timeouts_to_overrides(
    project: Path, runner: CliRunner, fake_judge: FakeJudge, overrides
) -> None:
    """`eval run --timeout/--judge-timeout` reach the stage overrides."""
    overrides.installed["eval.generate"] = _override("eval.generate", ("gen",))
    overrides.installed["eval.grade"] = _override("eval.grade", ("grader",))
    overrides.codes[("gen",)] = _fake_generate_writing_traces(project)
    overrides.codes[("grader",)] = 0
    result = runner.invoke(
        cmd_run, ["--timeout", "900", "--judge-timeout", "1800"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    gen_argv, grade_argv = (c["argv"] for c in overrides.calls)
    assert gen_argv[-2:] == ["--timeout", "900"]
    assert grade_argv[-2:] == ["--judge-timeout", "1800"]
    # At the defaults neither flag is forwarded (like --concurrency).
    overrides.calls.clear()
    result = runner.invoke(cmd_run, [], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert not any(
        f in c["argv"] for c in overrides.calls for f in ("--timeout", "--judge-timeout")
    )


def test_run_honours_grade_override_and_its_exit_code(
    project: Path, runner: CliRunner, fake_chat, fake_judge: FakeJudge, overrides
) -> None:
    fake_chat(STREAMS)
    overrides.installed["eval.grade"] = _override("eval.grade", ("grader",))
    overrides.codes[("grader",)] = 1
    result = runner.invoke(
        cmd_run,
        [
            "--url",
            "http://agent.example",
            "--config",
            "tests/eval/eval_config.yaml",
            "--output",
            "out/",
            "--judge-provider",
            "openai",
            "--judge-model",
            "m",
        ],
    )
    assert result.exit_code == 1, result.output
    call = overrides.calls[0]
    assert call["vector"] == ("grader",)
    argv = call["argv"]
    assert argv[0] == "--traces" and Path(argv[1]).exists()
    assert argv[2:] == [
        "--config",
        str((project / "tests/eval/eval_config.yaml").resolve()),
        "--output",
        "out/",
        "--judge-provider",
        "openai",
        "--judge-model",
        "m",
    ]
    assert fake_judge.calls == []  # the built-in grade did not run
    assert not _paths.default_grade_results_dir(project).exists()


def test_run_honours_both_overrides(
    project: Path, runner: CliRunner, fake_judge: FakeJudge, overrides
) -> None:
    overrides.installed["eval.generate"] = _override("eval.generate", ("gen",))
    overrides.installed["eval.grade"] = _override("eval.grade", ("grader",))
    overrides.codes[("gen",)] = _fake_generate_writing_traces(project)
    overrides.codes[("grader",)] = 0
    result = runner.invoke(cmd_run, [], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert [c["vector"] for c in overrides.calls] == [("gen",), ("grader",)]
    assert fake_judge.calls == []


def test_run_skips_grade_when_generate_override_produces_no_traces(
    project: Path, runner: CliRunner, fake_judge: FakeJudge, overrides
) -> None:
    overrides.installed["eval.generate"] = _override("eval.generate", ("gen",))
    overrides.codes[("gen",)] = 3
    result = runner.invoke(cmd_run, [])
    assert result.exit_code == 3
    assert "skipping grade" in result.output
    overrides.codes[("gen",)] = 0  # exits 0 but writes nothing
    result = runner.invoke(cmd_run, [])
    assert result.exit_code == 2


def test_run_outside_project_exit_3(
    tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cmd_run, [])
    assert result.exit_code == 3


def test_run_validates_the_grade_config_before_generating(
    project: Path, runner: CliRunner, fake_chat, fake_judge: FakeJudge, overrides
) -> None:
    """An unknown metric used to fail only after every case had been generated
    (and, with a real model, paid for)."""
    data = json.loads((project / _paths.DEFAULT_INPUT_DATASET).read_text())
    data["cases"][0]["judge"] = {"nonexistent": {"threshold": 3}}
    (project / _paths.DEFAULT_INPUT_DATASET).write_text(json.dumps(data))
    chat = fake_chat(STREAMS)
    result = runner.invoke(cmd_run, ["--url", "http://agent.example"])
    assert result.exit_code == 3, result.output
    assert "unknown judge metric 'nonexistent'" in result.output
    assert chat.calls == []
    assert not list(_paths.default_traces_dir(project).glob("traces_*.json"))
    assert "Step 1/2" not in result.output

    # A broken --config is caught up front the same way.
    result = runner.invoke(cmd_run, ["--url", "http://agent.example", "--config", "nope.yaml"])
    assert result.exit_code == 3, result.output
    assert chat.calls == []


def test_run_leaves_validation_to_a_grade_override(
    project: Path, runner: CliRunner, fake_chat, overrides
) -> None:
    """An eval.grade override may accept metrics the built-in grade does not know."""
    data = json.loads((project / _paths.DEFAULT_INPUT_DATASET).read_text())
    data["cases"][0]["judge"] = {"team_metric": {"threshold": 3}}
    (project / _paths.DEFAULT_INPUT_DATASET).write_text(json.dumps(data))
    overrides.installed["eval.grade"] = _override("eval.grade", ("grader",))
    overrides.codes[("grader",)] = 0
    fake_chat(STREAMS)
    result = runner.invoke(cmd_run, ["--url", "http://agent.example"])
    assert result.exit_code == 0, result.output


def test_session_token_is_accepted_but_hidden_from_help(runner: CliRunner) -> None:
    """Like `run` and `eval generate`: a deprecated alias of --header, never advertised."""
    for command in (cmd_run, cmd_generate):
        result = runner.invoke(command, ["--help"])
        assert result.exit_code == 0, result.output
        assert "--session-token" not in result.output
        assert "session" not in result.output.lower()
        assert "--header" in result.output
