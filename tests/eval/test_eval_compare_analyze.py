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

"""`eval compare` and `eval analyze` on results fixtures."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
from conftest import FakeJudge

from graph_agents_cli.eval import _paths
from graph_agents_cli.eval.cmd_analyze import cluster_results, cmd_analyze, normalize_reason
from graph_agents_cli.eval.cmd_compare import cmd_compare, compare_results


def _case(
    cid: str, status: str, reasons: list[str] | None = None, scores: dict | None = None
) -> dict:
    return {
        "id": cid,
        "status": status,
        "reasons": reasons or [],
        "checks": {},
        "judge_scores": {
            k: {"score": v, "threshold": 4, "passed": v >= 4, "quality": True}
            for k, v in (scores or {}).items()
        },
    }


BASELINE = {
    "dataset_hash": "h1",
    "summary": {
        "passed": 3,
        "failed": 0,
        "quality_below_threshold": 1,
        "error": 0,
        "missing": 0,
        "exit_code": 0,
    },
    "quality": {"response_quality": {"pass_rate": 0.75, "min_pass_rate": 0.5, "met": True}},
    "cases": [
        _case("a", "passed", scores={"response_quality": 5}),
        _case("b", "passed", scores={"response_quality": 4}),
        _case(
            "c",
            "quality_below_threshold",
            ["response_quality: score 3 below threshold 4 (quality)"],
            {"response_quality": 3},
        ),
        _case("d", "passed"),
    ],
}

CANDIDATE = {
    "dataset_hash": "h1",
    "summary": {
        "passed": 2,
        "failed": 1,
        "quality_below_threshold": 0,
        "error": 0,
        "missing": 1,
        "exit_code": 2,
    },
    "quality": {"response_quality": {"pass_rate": None, "min_pass_rate": 0.5, "met": None}},
    "cases": [
        _case(
            "a", "failed", ["contains: response does not contain 'hello'"], {"response_quality": 4}
        ),
        _case("b", "passed", scores={"response_quality": 5}),
        _case("c", "passed", scores={"response_quality": 4}),
        _case("e", "missing", ["missing: no trace for case"]),
    ],
}


def test_compare_results_detects_regressions_and_improvements() -> None:
    report = compare_results(BASELINE, CANDIDATE)
    cases = report["cases"]
    assert (
        cases["a"]["change"] == "regressed"
        and cases["a"]["scores"]["response_quality"]["delta"] == -1
    )
    assert (
        cases["b"]["change"] == "unchanged"
        and cases["b"]["scores"]["response_quality"]["delta"] == 1
    )
    assert cases["c"]["change"] == "improved"
    assert cases["d"]["change"] == "removed"
    assert cases["e"]["change"] == "added"
    assert report["regressed"] is True
    assert "a: passed -> failed" in report["regressions"]
    assert "d: removed from the candidate run" in report["regressions"]
    assert "exit code 0 -> 2" in report["regressions"]
    assert "c: quality_below_threshold -> passed" in report["improvements"]
    assert report["summary"]["differences"]["passed"] == {
        "baseline": 3,
        "candidate": 2,
        "delta": "-1",
    }
    assert report["quality"]["response_quality"]["candidate_pass_rate"] is None
    assert compare_results(BASELINE, BASELINE)["regressed"] is False


def test_compare_reports_traces_graded_against_another_dataset(
    tmp_path: Path, runner: CliRunner
) -> None:
    from graph_agents_cli.eval.cmd_compare import cmd_compare, compare_results

    base = {"dataset_hash": "h1", "summary": {"exit_code": 0}, "quality": {}, "cases": []}
    cand = json.loads(json.dumps(base))
    cand["traces_dataset_hash"] = "h0"  # graded with --dataset against stale traces
    report = compare_results(base, cand)
    assert report["traces_dataset_hash"]["candidate"] == {"value": "h0", "mismatch": True}
    assert report["traces_dataset_hash"]["baseline"]["mismatch"] is False
    (tmp_path / "b.json").write_text(json.dumps(base))
    (tmp_path / "c.json").write_text(json.dumps(cand))
    result = runner.invoke(cmd_compare, [str(tmp_path / "b.json"), str(tmp_path / "c.json")])
    assert result.exit_code == 0, result.output
    assert "candidate results were graded against a dataset the traces were not generated from" in (
        result.output
    )


def test_compare_quality_rate_drop_is_a_regression() -> None:
    cand = json.loads(json.dumps(BASELINE))
    cand["quality"]["response_quality"] = {"pass_rate": 0.5, "min_pass_rate": 0.5, "met": True}
    report = compare_results(BASELINE, cand)
    assert report["regressions"] == ["quality response_quality: pass rate 75% -> 50%"]
    cand["quality"]["response_quality"]["met"] = False
    assert (
        "quality response_quality: min_pass_rate no longer met"
        in compare_results(BASELINE, cand)["regressions"]
    )


def test_compare_command_table_json_and_fail_flag(tmp_path: Path, runner: CliRunner) -> None:
    base = tmp_path / "base.json"
    cand = tmp_path / "cand.json"
    base.write_text(json.dumps(BASELINE), encoding="utf-8")
    cand.write_text(json.dumps(CANDIDATE), encoding="utf-8")
    result = runner.invoke(cmd_compare, [str(base), str(cand)], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert "regressed" in result.output and "Regressions:" in result.output
    result = runner.invoke(cmd_compare, [str(base), str(cand), "--fail-on-regression"])
    assert result.exit_code == 1
    result = runner.invoke(
        cmd_compare, [str(base), str(base), "--fail-on-regression"], catch_exceptions=False
    )
    assert result.exit_code == 0 and "No regressions" in result.output
    result = runner.invoke(cmd_compare, [str(base), str(cand), "--json"], catch_exceptions=False)
    data = json.loads(result.output)
    assert data["regressed"] is True and data["baseline"] == str(base)


def test_normalize_reason_masks_literals_and_numbers() -> None:
    assert normalize_reason("contains: response does not contain 'hello'") == (
        "contains",
        "response does not contain <x>",
    )
    assert normalize_reason("response_quality: score 2 below threshold 4 (quality)") == (
        "response_quality",
        "score # below threshold # (quality)",
    )
    assert normalize_reason("error: judge task_success: RateLimitError: 429") == (
        "error/judge",
        "ratelimiterror: #",
    )
    assert normalize_reason("missing: no trace for case") == ("missing", "no trace for case")
    assert normalize_reason("free text") == ("other", "free text")


def test_cluster_results_is_deterministic_and_sorted() -> None:
    results = {
        "cases": [
            _case("a", "failed", ["contains: response does not contain 'hello'"]),
            _case(
                "b",
                "failed",
                ["contains: response does not contain 'bye'", "regex: response does not match /x/"],
            ),
            _case("c", "passed"),
            _case(
                "d",
                "quality_below_threshold",
                ["response_quality: score 3 below threshold 4 (quality)"],
            ),
            _case("e", "missing"),
        ]
    }
    clusters = cluster_results(results)
    assert clusters == cluster_results(json.loads(json.dumps(results)))
    assert [(c["status"], c["kind"], c["count"]) for c in clusters] == [
        ("failed", "contains", 2),
        ("missing", "missing", 1),
        ("failed", "regex", 1),
        ("quality_below_threshold", "response_quality", 1),
    ]
    assert clusters[0]["case_ids"] == ["a", "b"] and clusters[0]["rank"] == 1
    assert clusters[1]["examples"] == ["missing: no reason recorded"]


def test_analyze_command_writes_artifact_and_optional_judge_summary(
    project: Path, runner: CliRunner, fake_judge: FakeJudge
) -> None:
    results_dir = _paths.default_grade_results_dir(project)
    results_dir.mkdir(parents=True)
    (results_dir / "results_20260922_000000.json").write_text(
        json.dumps(CANDIDATE), encoding="utf-8"
    )
    result = runner.invoke(cmd_analyze, ["--top-k", "1"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    out = sorted((project / _paths.ARTIFACTS_DIR).glob("analysis_*.json"))
    assert len(out) == 1
    analysis = json.loads(out[0].read_text(encoding="utf-8"))
    assert analysis["flagged_cases"] == 2 and analysis["total_cases"] == 4
    assert analysis["judge_summary"] is None
    assert [c["kind"] for c in analysis["clusters"]] == ["missing", "contains"]
    assert "Failure clusters (2 of 4 cases)" in result.output

    fake_judge.default = 0
    result = runner.invoke(
        cmd_analyze, ["--judge", "--output", "artifacts/a.json"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    payload = fake_judge.calls[0]
    assert (
        payload["items"][0]["kind"] == "summarize" and "contains" in payload["items"][0]["prompt"]
    )
    assert payload["judge"]["provider"] == "fake"
    analysis = json.loads((project / "artifacts/a.json").read_text(encoding="utf-8"))
    assert analysis["judge_summary"] == "fake judge"
    assert analysis["judge"] == {"provider": "fake", "model": "fake-judge"}


def test_analyze_with_no_failures_and_missing_results(
    project: Path, runner: CliRunner, tmp_path: Path
) -> None:
    result = runner.invoke(cmd_analyze, [])
    assert result.exit_code == 3 and "no results found" in result.output
    clean = tmp_path / "clean.json"
    clean.write_text(json.dumps({"cases": [_case("a", "passed")], "summary": {}}), encoding="utf-8")
    result = runner.invoke(cmd_analyze, ["--results", str(clean)], catch_exceptions=False)
    assert result.exit_code == 0 and "No failures to analyze" in result.output
