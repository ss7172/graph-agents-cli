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

"""`eval metric list`, `eval submit` (fake langsmith), and the eval group wiring."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from click.testing import CliRunner
from conftest import FakeJudge, good_traces, read_results, write_traces

from graph_agents_cli.eval import _paths
from graph_agents_cli.eval.cmd_eval_group import eval_group
from graph_agents_cli.eval.cmd_grade import cmd_grade
from graph_agents_cli.eval.cmd_metric import metric_group
from graph_agents_cli.eval.cmd_submit import cmd_submit
from graph_agents_cli.main import main


def test_eval_group_registers_exactly_the_contract_commands(runner: CliRunner) -> None:
    result = runner.invoke(eval_group, ["--help"], catch_exceptions=False)
    assert result.exit_code == 0
    names = set(eval_group.list_commands(None))
    assert names == {"run", "generate", "grade", "compare", "analyze", "submit", "metric"}
    for name in sorted(names):
        sub = runner.invoke(main, ["eval", name, "--help"], catch_exceptions=False)
        assert sub.exit_code == 0, f"{name}: {sub.output}"
    assert runner.invoke(main, ["eval", "metric", "list", "--help"]).exit_code == 0


def test_metric_list_shows_checks_and_judges(project: Path, runner: CliRunner) -> None:
    (project / _paths.DEFAULT_EVAL_CONFIG).write_text(
        "quality_metrics:\n  response_quality: {threshold: 4}\n"
        "judges:\n  helpfulness: {rubric: 'be helpful', description: 'Helpful?'}\n"
        "custom_metrics:\n  - tests.eval.metrics:word_count\n",
        encoding="utf-8",
    )
    result = runner.invoke(metric_group, ["list"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    for name in (
        "contains",
        "json_schema",
        "tool_calls",
        "max_tokens",
        "response_quality",
        "task_success",
        "groundedness",
        "helpfulness",
        "word_count",
    ):
        assert name in result.output
    assert "(quality)" in result.output
    assert "Check modifiers" in result.output
    result = runner.invoke(metric_group, ["list", "--json"], catch_exceptions=False)
    data = json.loads(result.output)
    assert {c["name"] for c in data["checks"]} >= {"contains", "regex"}
    # The documented behaviour, and the modifiers that change it.
    checks = {c["name"]: c["description"] for c in data["checks"]}
    assert "(case-insensitive; `case_insensitive: false` for exact case)" in checks["contains"]
    assert "case-insensitive" in checks["not_contains"]
    assert {m["name"] for m in data["modifiers"]} == {"ordered", "case_insensitive", "scope"}
    assert {j["name"] for j in data["judges"]} == {
        "response_quality",
        "task_success",
        "groundedness",
        "helpfulness",
    }
    assert data["custom_metrics"][0]["callable"] == "tests.eval.metrics:word_count"
    assert data["quality_metrics"] == ["response_quality"]


def test_submit_without_langsmith_key_is_a_clear_configuration_error(
    project: Path, runner: CliRunner
) -> None:
    result = runner.invoke(cmd_submit, [])
    assert result.exit_code == 3
    assert "LANGSMITH_API_KEY" in result.output and "optional" in result.output


class FakeLangSmithClient:
    """Records every call; datasets and examples persist across instances like a server."""

    instances: ClassVar[list[FakeLangSmithClient]] = []
    datasets: ClassVar[dict[str, SimpleNamespace]] = {}
    examples: ClassVar[list[dict]] = []

    @classmethod
    def reset(cls) -> None:
        cls.instances.clear()
        cls.datasets.clear()
        cls.examples.clear()

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.updated: list[dict] = []
        self.projects: list[dict] = []
        self.runs: list[dict] = []
        self.feedback: list[dict] = []
        FakeLangSmithClient.instances.append(self)

    def read_dataset(self, *, dataset_name: str):
        if dataset_name not in self.datasets:
            raise LookupError(dataset_name)
        return self.datasets[dataset_name]

    def create_dataset(self, *, dataset_name: str, description: str = ""):
        ds = SimpleNamespace(
            id=f"ds-{dataset_name}", name=dataset_name, url=f"https://smith/{dataset_name}"
        )
        self.datasets[dataset_name] = ds
        return ds

    def list_examples(self, *, dataset_id: str):
        return [
            SimpleNamespace(id=e["id"], metadata=e["metadata"])
            for e in self.examples
            if e["dataset_id"] == dataset_id
        ]

    def create_example(self, *, inputs, outputs=None, dataset_id=None, metadata=None):
        example = {
            "id": f"ex-{len(self.examples)}",
            "inputs": inputs,
            "outputs": outputs,
            "dataset_id": dataset_id,
            "metadata": metadata,
        }
        self.examples.append(example)
        return SimpleNamespace(id=example["id"])

    def update_example(self, *, example_id, inputs=None, outputs=None, metadata=None):
        self.updated.append(
            {"id": example_id, "inputs": inputs, "outputs": outputs, "metadata": metadata}
        )

    def create_project(self, *, project_name, reference_dataset_id=None, metadata=None):
        project = {
            "name": project_name,
            "reference_dataset_id": reference_dataset_id,
            "metadata": metadata,
        }
        self.projects.append(project)
        return SimpleNamespace(
            id=f"proj-{len(self.projects)}",
            name=project_name,
            url=f"https://smith/p/{project_name}",
        )

    def create_run(self, **kwargs):
        self.runs.append(kwargs)

    def create_feedback(self, run_id, key, **kwargs):
        self.feedback.append({"run_id": run_id, "key": key, **kwargs})


@pytest.fixture
def fake_langsmith(monkeypatch: pytest.MonkeyPatch):
    FakeLangSmithClient.reset()
    module = types.ModuleType("langsmith")
    module.Client = FakeLangSmithClient
    monkeypatch.setitem(sys.modules, "langsmith", module)
    return FakeLangSmithClient


def test_submit_uploads_dataset_examples_experiment_runs_and_feedback(
    project: Path,
    runner: CliRunner,
    fake_judge: FakeJudge,
    fake_langsmith,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_judge.scores["greeting/response_quality"] = 3
    write_traces(project, good_traces())
    # greeting is the only case scored on the quality metric: 0 of 1 met its
    # threshold, under min_pass_rate 0.5, so the gate fails (exit 1).
    assert runner.invoke(cmd_grade, [], catch_exceptions=False).exit_code == 1
    results = read_results(project)
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-key")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://smith.example")

    result = runner.invoke(cmd_submit, [], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    client = fake_langsmith.instances[0]
    assert client.kwargs == {"api_key": "ls-key", "api_url": "https://smith.example"}
    assert list(client.datasets) == ["demo-agent-eval"]
    assert [e["metadata"]["case_id"] for e in client.examples] == ["greeting", "weather", "plain"]
    assert client.examples[0]["inputs"] == {"messages": [{"role": "user", "content": "hi"}]}
    assert client.examples[0]["outputs"] == {"reference": "hello there"}
    assert client.examples[0]["metadata"]["dataset_hash"] == results["dataset_hash"]
    assert client.projects[0]["reference_dataset_id"] == "ds-demo-agent-eval"
    assert client.projects[0]["metadata"]["summary"] == results["summary"]
    assert client.projects[0]["name"].startswith("demo-agent-eval-")
    assert len(client.runs) == 3
    weather = next(r for r in client.runs if r["name"] == "weather")
    assert (
        weather["outputs"]["response"] == "It is sunny in SF."
        and weather["outputs"]["status"] == "passed"
    )
    assert (
        weather["reference_example_id"] == "ex-1"
        and weather["project_name"] == client.projects[0]["name"]
    )
    assert weather["extra"]["metadata"]["thread_id"] == "thread-weather"
    keys = {
        (f["key"], f["score"])
        for f in client.feedback
        if f["run_id"] == next(r["id"] for r in client.runs if r["name"] == "greeting")
    }
    assert (
        ("gate", 0) in keys and ("check:contains", 1) in keys and ("response_quality", 3.0) in keys
    )
    assert "https://smith/demo-agent-eval" in result.output

    # A second submit updates the existing examples instead of duplicating them.
    result = runner.invoke(
        cmd_submit,
        ["--experiment", "second", "--dataset-name", "demo-agent-eval"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    client2 = fake_langsmith.instances[1]
    assert list(fake_langsmith.datasets) == ["demo-agent-eval"]
    assert len(fake_langsmith.examples) == 3
    assert [u["id"] for u in client2.updated] == ["ex-0", "ex-1", "ex-2"]
    assert client2.projects[0]["name"] == "second"
    assert "Updating LangSmith dataset" in result.output


def test_submit_unreachable_endpoint_is_one_line_exit_2(
    project: Path,
    runner: CliRunner,
    fake_judge: FakeJudge,
    fake_langsmith,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK's connection error used to surface as a ~200-line traceback."""

    class LangSmithConnectionError(Exception):
        pass

    def unreachable(self, **kwargs):
        raise LangSmithConnectionError(
            "Connection error caused failure to POST /datasets\n  ... 200 more lines"
        )

    monkeypatch.setattr(fake_langsmith, "create_dataset", unreachable)
    write_traces(project, good_traces())
    assert runner.invoke(cmd_grade, [], catch_exceptions=False).exit_code == 0
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-key")
    result = runner.invoke(cmd_submit, ["--endpoint", "http://127.0.0.1:18645"])
    assert result.exit_code == 2, result.output
    assert "could not reach http://127.0.0.1:18645" in result.output
    assert "LangSmithConnectionError: Connection error caused failure" in result.output
    assert "200 more lines" not in result.output
    assert "Traceback" not in result.output


def test_submit_reports_missing_langsmith_package(
    project: Path, runner: CliRunner, fake_judge: FakeJudge, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_traces(project, good_traces())
    assert runner.invoke(cmd_grade, [], catch_exceptions=False).exit_code == 0
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-key")
    monkeypatch.setitem(sys.modules, "langsmith", None)
    result = runner.invoke(cmd_submit, [])
    assert result.exit_code == 3 and "graph-agents-cli[langsmith]" in result.output


def test_submit_needs_a_dataset_it_can_locate(
    project: Path,
    runner: CliRunner,
    fake_langsmith,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-key")
    results = tmp_path / "r.json"
    results.write_text(
        json.dumps(
            {
                "dataset_hash": "x",
                "summary": {},
                "quality": {},
                "cases": [{"id": "a", "status": "passed", "reasons": []}],
            }
        ),
        encoding="utf-8",
    )
    result = runner.invoke(cmd_submit, ["--results", str(results)])
    assert result.exit_code == 3 and "pass --dataset" in result.output
    result = runner.invoke(
        cmd_submit,
        ["--results", str(results), "--dataset", "tests/eval/datasets"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "no traces found" in result.output
    assert len(fake_langsmith.instances[-1].runs) == 1
