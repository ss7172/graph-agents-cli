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

"""`eval grade`: statuses, planned-case accounting, quality rates, exit codes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from conftest import DATASET, FakeJudge, good_traces, make_trace, read_results, write_traces

from graph_agents_cli.eval import _paths
from graph_agents_cli.eval._common import write_json_file
from graph_agents_cli.eval.cmd_grade import cmd_grade, load_traces
from graph_agents_cli.eval.dataset import dataset_hash
from graph_agents_cli.main import main


def _grade(runner: CliRunner, *args: str):
    return runner.invoke(cmd_grade, list(args), catch_exceptions=False)


def _statuses(results: dict) -> dict[str, str]:
    return {c["id"]: c["status"] for c in results["cases"]}


def test_all_pass_exit_0_and_results_schema(
    project: Path, runner: CliRunner, fake_judge: FakeJudge, dataset_digest: str
) -> None:
    write_traces(project, good_traces())
    result = _grade(runner)
    assert result.exit_code == 0, result.output
    results = read_results(project)
    assert set(results) >= {
        "dataset_hash",
        "graded_at",
        "judge",
        "capture",
        "summary",
        "quality",
        "cases",
    }
    assert results["dataset_hash"] == dataset_digest
    assert results["judge"] == {"provider": "fake", "model": "fake-judge"}
    assert results["capture"] == "metadata"
    assert results["summary"] == {
        "passed": 3,
        "failed": 0,
        "quality_below_threshold": 0,
        "error": 0,
        "missing": 0,
        "exit_code": 0,
    }
    # Only greeting declares response_quality: the rate is over 1 scored case, not 3.
    assert results["quality"]["response_quality"] == {
        "pass_rate": 1.0,
        "min_pass_rate": 0.5,
        "met": True,
        "below_threshold": 0,
        "scored": 1,
        "passed": 1,
        "status": "met",
    }
    # The judge is the fake model: said next to the result and recorded.
    assert results["fake_model"] == ["judge"]
    assert any("not a quality signal" in w for w in results["warnings"])
    assert "plumbing check only, not a quality signal" in result.output
    greeting = next(c for c in results["cases"] if c["id"] == "greeting")
    assert greeting["checks"]["contains"]["passed"] is True
    assert greeting["judge_scores"]["response_quality"]["score"] == 5.0
    assert greeting["judge_scores"]["response_quality"]["quality"] is True
    assert greeting["reasons"] == []
    assert "gate met" in result.output
    # Only two cases declare judges, so two runner items in one call.
    assert len(fake_judge.calls) == 1
    assert sorted(i["id"] for i in fake_judge.calls[0]["items"]) == [
        "greeting/response_quality",
        "weather/task_success",
    ]
    assert fake_judge.calls[0]["judge"] == {"provider": "fake", "model": "fake-judge"}
    prompt = next(i for i in fake_judge.calls[0]["items"] if i["case_id"] == "greeting")["prompt"]
    assert "hello there" in prompt and "user: hi" in prompt


def test_missing_case_is_incomplete_exit_2(
    project: Path, runner: CliRunner, fake_judge: FakeJudge
) -> None:
    write_traces(project, good_traces()[:2])  # "plain" has no trace
    result = _grade(runner)
    assert result.exit_code == 2
    results = read_results(project)
    assert _statuses(results)["plain"] == "missing"
    assert results["summary"]["missing"] == 1 and results["summary"]["exit_code"] == 2
    assert results["cases"][2]["reasons"] == ["missing: no trace for case"]
    # A quality rate is never computed over an incomplete run.
    assert results["quality"]["response_quality"]["pass_rate"] is None
    assert results["quality"]["response_quality"]["met"] is None


def test_trace_without_response_is_missing_and_error_status_is_error(
    project: Path, runner: CliRunner, fake_judge: FakeJudge
) -> None:
    traces = good_traces()
    traces[0] = make_trace("greeting", status="error", error="HTTP 500 from server")
    traces[2] = make_trace(
        "plain", response=None, status="missing", error="stream produced no events"
    )
    write_traces(project, traces)
    result = _grade(runner)
    assert result.exit_code == 2
    results = read_results(project)
    assert _statuses(results) == {"greeting": "error", "weather": "passed", "plain": "missing"}
    assert results["cases"][0]["reasons"] == ["error: HTTP 500 from server"]
    # Errored/missing cases are not sent to the judge.
    assert [i["case_id"] for i in fake_judge.calls[0]["items"]] == ["weather"]


def test_deterministic_failure_exit_1_and_skips_judge_for_that_case(
    project: Path, runner: CliRunner, fake_judge: FakeJudge
) -> None:
    traces = good_traces()
    traces[0] = make_trace("greeting", response="goodbye")
    write_traces(project, traces)
    result = _grade(runner)
    assert result.exit_code == 1
    results = read_results(project)
    assert _statuses(results)["greeting"] == "failed"
    assert results["cases"][0]["reasons"] == ["contains: response does not contain 'hello'"]
    assert results["cases"][0]["judge_scores"] == {}
    assert [i["case_id"] for i in fake_judge.calls[0]["items"]] == ["weather"]
    assert "gate failed" in result.output


def _declare_everywhere(root: Path, metric: str = "response_quality") -> None:
    """Declare ``metric`` on every case of DATASET and write matching good traces."""
    cases = [dict(c, judge={**c.get("judge", {}), metric: {}}) for c in DATASET["cases"]]
    write_json_file(root / _paths.DEFAULT_INPUT_DATASET, {"cases": cases})
    by_id = {c["id"]: c for c in cases}
    traces = [dict(t, case=by_id[t["case_id"]]) for t in good_traces()]
    write_traces(root, traces, digest=dataset_hash(cases))


def test_quality_metric_under_case_threshold_but_rate_met_exit_0(
    project: Path, runner: CliRunner, fake_judge: FakeJudge
) -> None:
    _declare_everywhere(project)
    fake_judge.scores["greeting/response_quality"] = 2
    result = _grade(runner)
    assert result.exit_code == 0, result.output
    results = read_results(project)
    assert _statuses(results)["greeting"] == "quality_below_threshold"
    assert results["cases"][0]["reasons"] == [
        "response_quality: score 2 below threshold 4 (quality)"
    ]
    # 1 of the 3 cases scored on the metric is below threshold -> 66% >= min_pass_rate 0.5
    quality = results["quality"]["response_quality"]
    assert quality["pass_rate"] == pytest.approx(0.6667, abs=1e-3)
    assert (quality["met"], quality["scored"], quality["passed"]) == (True, 3, 2)
    assert results["summary"]["quality_below_threshold"] == 1
    assert "2/3" in result.output


def test_quality_rate_counts_only_cases_that_ran_the_metric(
    project: Path, runner: CliRunner, fake_judge: FakeJudge
) -> None:
    """A case that never declared a quality metric is not a pass for it.

    Only greeting declares response_quality; it scores 2 (below 4). Counting
    the other two cases as passes would read 66% and meet min_pass_rate 0.5.
    """
    fake_judge.scores["greeting/response_quality"] = 2
    write_traces(project, good_traces())
    result = _grade(runner)
    assert result.exit_code == 1, result.output
    quality = read_results(project)["quality"]["response_quality"]
    assert quality["pass_rate"] == 0.0 and quality["met"] is False
    assert (quality["scored"], quality["passed"], quality["status"]) == (1, 0, "not_met")
    assert "0/1" in result.output


def test_quality_metric_no_case_ran_is_not_applicable(
    project: Path, runner: CliRunner, fake_judge: FakeJudge
) -> None:
    (project / _paths.DEFAULT_EVAL_CONFIG).write_text(
        "judge: {provider: fake}\nquality_metrics:\n"
        "  response_quality: {threshold: 4, min_pass_rate: 0.9}\n"
        "  groundedness: {threshold: 4, min_pass_rate: 0.9}\n",
        encoding="utf-8",
    )
    write_traces(project, good_traces())
    result = _grade(runner)
    assert result.exit_code == 0, result.output
    grounded = read_results(project)["quality"]["groundedness"]
    assert grounded["pass_rate"] is None and grounded["met"] is None
    assert (grounded["scored"], grounded["status"]) == (0, "not_run")
    assert "n/a (no case ran it)" in result.output
    assert "100%" in result.output  # response_quality, scored on greeting


def test_quality_metric_under_min_pass_rate_exit_1(
    project: Path, runner: CliRunner, fake_judge: FakeJudge
) -> None:
    (project / _paths.DEFAULT_EVAL_CONFIG).write_text(
        "judge: {provider: fake}\nquality_metrics:\n  response_quality: {threshold: 4, min_pass_rate: 0.9}\n",
        encoding="utf-8",
    )
    fake_judge.scores["greeting/response_quality"] = 3.5
    write_traces(project, good_traces())
    result = _grade(runner)
    assert result.exit_code == 1
    results = read_results(project)
    assert results["summary"]["failed"] == 0
    assert results["summary"]["quality_below_threshold"] == 1
    assert results["quality"]["response_quality"]["met"] is False


def test_mandatory_judge_below_threshold_is_failed_exit_1(
    project: Path, runner: CliRunner, fake_judge: FakeJudge
) -> None:
    fake_judge.scores["weather/task_success"] = 2
    write_traces(project, good_traces())
    result = _grade(runner)
    assert result.exit_code == 1
    results = read_results(project)
    assert _statuses(results)["weather"] == "failed"
    assert results["cases"][1]["reasons"] == ["task_success: score 2 below threshold 3"]
    assert results["cases"][1]["judge_scores"]["task_success"]["quality"] is False


def test_judge_call_that_raises_marks_case_error_exit_2(
    project: Path, runner: CliRunner, fake_judge: FakeJudge
) -> None:
    fake_judge.scores["greeting/response_quality"] = "RateLimitError: slow down"
    write_traces(project, good_traces())
    result = _grade(runner)
    assert result.exit_code == 2
    results = read_results(project)
    assert _statuses(results)["greeting"] == "error"
    assert results["cases"][0]["reasons"] == [
        "error: judge response_quality: RateLimitError: slow down"
    ]


def test_unreachable_judge_is_configuration_error_exit_3(
    project: Path, runner: CliRunner, fake_judge: FakeJudge
) -> None:
    from graph_agents_cli.eval._common import EvalConfigError

    fake_judge.fail_with = EvalConfigError(
        "judge runner failed (exit code 1); the judge model is unreachable"
    )
    write_traces(project, good_traces())
    result = runner.invoke(cmd_grade, [])
    assert result.exit_code == 3
    assert "unreachable" in result.output


def _dataset_with_judge(project: Path, judge: dict) -> None:
    """Rewrite the on-disk dataset so `plain` declares `judge`, keeping traces hash-matched."""
    cases = [*DATASET["cases"][:2], {**DATASET["cases"][2], "judge": judge}]
    (project / _paths.DEFAULT_INPUT_DATASET).write_text(
        json.dumps({"cases": cases}), encoding="utf-8"
    )
    traces = good_traces()
    traces[2]["case"] = cases[2]
    write_traces(project, traces, digest=dataset_hash(cases))


def test_unknown_judge_metric_exit_3_before_any_judging(
    project: Path, runner: CliRunner, fake_judge: FakeJudge
) -> None:
    _dataset_with_judge(project, {"vibes": {"threshold": 1}})
    result = runner.invoke(cmd_grade, [])
    assert result.exit_code == 3
    assert "unknown judge metric 'vibes'" in result.output
    assert fake_judge.calls == []


def test_metric_without_threshold_exit_3(
    project: Path, runner: CliRunner, fake_judge: FakeJudge
) -> None:
    (project / _paths.DEFAULT_EVAL_CONFIG).write_text("judge: {provider: fake}\n", encoding="utf-8")
    _dataset_with_judge(project, {"groundedness": {}})
    result = runner.invoke(cmd_grade, [])
    assert result.exit_code == 3
    assert "has no threshold" in result.output
    assert fake_judge.calls == []


def test_quality_metric_without_threshold_in_config_exit_3(
    project: Path, runner: CliRunner, fake_judge: FakeJudge
) -> None:
    (project / _paths.DEFAULT_EVAL_CONFIG).write_text(
        "quality_metrics:\n  response_quality: {min_pass_rate: 0.5}\n", encoding="utf-8"
    )
    write_traces(project, good_traces())
    result = runner.invoke(cmd_grade, [])
    assert result.exit_code == 3
    assert "no threshold" in result.output


def test_dataset_flag_defines_planned_cases(
    project: Path, runner: CliRunner, fake_judge: FakeJudge
) -> None:
    bigger = {
        "cases": [
            *DATASET["cases"],
            {"id": "extra", "messages": [{"role": "user", "content": "x"}]},
        ]
    }
    path = project / "tests/eval/datasets/bigger.json"
    path.write_text(json.dumps(bigger), encoding="utf-8")
    write_traces(project, good_traces())
    result = _grade(runner, "--dataset", "tests/eval/datasets/bigger.json")
    assert result.exit_code == 2
    results = read_results(project)
    assert _statuses(results)["extra"] == "missing"
    assert results["planned"] == 4
    assert "hash mismatch" in result.output
    # Provenance: the planned dataset's hash and the traces' own hash are both recorded.
    assert results["dataset_hash"] == dataset_hash(bigger["cases"])
    assert results["traces_dataset_hash"] == dataset_hash(DATASET["cases"])
    assert results["traces_dataset_hash"] != results["dataset_hash"]


def test_results_record_the_traces_dataset_hash_on_a_plain_grade(
    project: Path, runner: CliRunner, fake_judge: FakeJudge
) -> None:
    write_traces(project, good_traces())
    result = _grade(runner)
    assert result.exit_code == 0, result.output
    results = read_results(project)
    assert (
        results["traces_dataset_hash"] == results["dataset_hash"] == dataset_hash(DATASET["cases"])
    )


def test_changed_dataset_on_disk_falls_back_to_embedded_cases(
    project: Path, runner: CliRunner, fake_judge: FakeJudge
) -> None:
    write_traces(project, good_traces())
    changed = {
        "cases": [*DATASET["cases"], {"id": "new", "messages": [{"role": "user", "content": "x"}]}]
    }
    (project / _paths.DEFAULT_INPUT_DATASET).write_text(json.dumps(changed), encoding="utf-8")
    result = _grade(runner)
    assert result.exit_code == 0, result.output
    assert "changed since the traces were generated" in result.output
    assert read_results(project)["planned"] == 3


def test_no_judge_metrics_means_no_runner_call(
    project: Path, runner: CliRunner, fake_judge: FakeJudge
) -> None:
    traces = [make_trace("plain", response="ok")]
    path = write_traces(project, traces)
    result = _grade(
        runner, "--traces", str(path), "--dataset", "tests/eval/datasets/basic-dataset.json"
    )
    assert result.exit_code == 2  # greeting/weather missing
    assert fake_judge.calls == []


def test_custom_metric_runs_for_every_case_and_reports(
    project: Path, runner: CliRunner, fake_judge: FakeJudge
) -> None:
    (project / _paths.DEFAULT_EVAL_CONFIG).write_text(
        "judge: {provider: fake}\ncustom_metrics:\n  - {callable: 'tests.eval.metrics:short', threshold: 1}\n",
        encoding="utf-8",
    )
    fake_judge.scores["plain/short"] = 0
    write_traces(project, good_traces())
    result = _grade(runner)
    assert result.exit_code == 1
    results = read_results(project)
    assert _statuses(results) == {"greeting": "passed", "weather": "passed", "plain": "failed"}
    items = {i["id"]: i for i in fake_judge.calls[0]["items"]}
    assert items["plain/short"]["kind"] == "custom"
    assert items["plain/short"]["callable"] == "tests.eval.metrics:short"
    assert items["plain/short"]["trace"]["response"] == "ok"


def test_traces_directory_is_merged_and_mixed_datasets_rejected(
    project: Path, runner: CliRunner, fake_judge: FakeJudge, tmp_path: Path
) -> None:
    d = tmp_path / "traces"
    write_traces(
        project,
        good_traces()[:1],
        path=d / "traces_20260922_000001.json",
        generated_at="2026-09-22T00:00:01+00:00",
    )
    write_traces(
        project,
        good_traces()[1:],
        path=d / "traces_20260922_000002.json",
        generated_at="2026-09-22T00:00:02+00:00",
    )
    # A newer file wins for a duplicated case id.
    write_traces(
        project,
        [make_trace("greeting", response="goodbye")],
        path=d / "traces_20260922_000003.json",
        generated_at="2026-09-22T00:00:03+00:00",
    )
    meta, traces = load_traces(sorted(d.glob("*.json")))
    assert traces["greeting"]["response"] == "goodbye"
    assert len(meta["files"]) == 3
    result = _grade(runner, "--traces", str(d), "--output", str(tmp_path / "out.json"))
    assert result.exit_code == 1
    assert json.loads((tmp_path / "out.json").read_text())["summary"]["failed"] == 1

    write_traces(project, good_traces(), digest="other", path=d / "traces_20260922_000004.json")
    result = runner.invoke(cmd_grade, ["--traces", str(d)])
    assert result.exit_code == 3 and "different dataset" in result.output


def test_grade_outside_project_needs_traces_and_no_judges(
    tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cmd_grade, [])
    assert result.exit_code == 3 and "pass --traces" in result.output
    path = write_traces(tmp_path, [make_trace("plain", response="ok")], path=tmp_path / "t.json")
    result = runner.invoke(
        cmd_grade,
        ["--traces", str(path), "--output", str(tmp_path / "r.json")],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert json.loads((tmp_path / "r.json").read_text())["summary"]["passed"] == 1
    # A case that needs a judge cannot be graded without the project environment.
    path = write_traces(tmp_path, [make_trace("greeting")], path=tmp_path / "t2.json")
    result = runner.invoke(
        cmd_grade, ["--traces", str(path), "--output", str(tmp_path / "r2.json")]
    )
    assert result.exit_code == 3 and "project's environment" in result.output


def test_grade_via_main_reports_exit_code(
    project: Path, runner: CliRunner, fake_judge: FakeJudge
) -> None:
    traces = good_traces()
    traces[0] = make_trace("greeting", response="goodbye")
    write_traces(project, traces)
    result = runner.invoke(main, ["eval", "grade"])
    assert result.exit_code == 1, result.output


def test_fake_agent_on_the_local_server_is_warned_next_to_the_result(
    project: Path, runner: CliRunner, fake_judge: FakeJudge
) -> None:
    write_traces(project, good_traces(), extra={"target": "local", "model_provider": "fake"})
    result = _grade(runner)
    assert result.exit_code == 0, result.output
    results = read_results(project)
    assert results["fake_model"] == ["agent", "judge"]
    assert results["warnings"][0].startswith("the agent ran on the deterministic fake model")
    out = result.output
    assert "Warning: the agent ran on the deterministic fake model (MODEL_PROVIDER=fake)" in out
    assert "Warning: the judge is the deterministic fake model" in out
    # Next to the verdict, on the same line, so a skimmed log cannot miss it.
    verdict = next(line for line in out.splitlines() if line.startswith("Result:"))
    assert verdict == (
        "Result: gate met (exit code 0) (fake model: plumbing check only, not a quality signal)"
    )
    assert out.index("Warning: the agent ran") < out.index("Result:")


def test_no_fake_warning_for_a_real_judge_or_a_url_agent(
    project: Path, runner: CliRunner, fake_judge: FakeJudge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A --url agent may run any model; a judge counts only when it scored something."""
    real = FakeJudge()

    def as_openai(root, payload, **kwargs):
        return {**real(root, payload, **kwargs), "provider": "openai", "model": "gpt-x"}

    monkeypatch.setattr("graph_agents_cli.eval.cmd_grade.run_judge_runner", as_openai)
    write_traces(project, good_traces(), extra={"target": "url", "model_provider": "fake"})
    result = _grade(runner)
    assert result.exit_code == 0, result.output
    results = read_results(project)
    assert results["fake_model"] == [] and results["warnings"] == []
    assert "not a quality signal" not in result.output

    # No judge metric at all: the (fake) judge never ran, so it is not reported.
    (project / _paths.DEFAULT_EVAL_CONFIG).write_text("judge: {provider: fake}\n", encoding="utf-8")
    cases = [{k: v for k, v in c.items() if k != "judge"} for c in DATASET["cases"]]
    write_json_file(project / _paths.DEFAULT_INPUT_DATASET, {"cases": cases})
    by_id = {c["id"]: c for c in cases}
    traces = [dict(t, case=by_id[t["case_id"]]) for t in good_traces()]
    write_traces(project, traces, digest=dataset_hash(cases), extra={"target": "url"})
    result = _grade(runner)
    assert result.exit_code == 0, result.output
    assert read_results(project)["fake_model"] == []
