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

"""Dataset loading/validation, eval config parsing, threshold resolution, paths."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import DATASET

from graph_agents_cli.eval import _paths
from graph_agents_cli.eval._common import EvalConfigError, project_meta, resolve_judge_identity
from graph_agents_cli.eval.config import (
    BUILTIN_JUDGES,
    load_eval_config,
    parse_eval_config,
    render_judge_prompt,
    resolve_threshold,
)
from graph_agents_cli.eval.dataset import dataset_hash, load_dataset, parse_case


def _write(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_load_dataset_hash_is_canonical_and_stable(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.json", DATASET)
    ds = load_dataset([a])
    assert ds.case_ids == ["greeting", "weather", "plain"]
    assert ds.hash == dataset_hash(DATASET["cases"])
    # Key order and whitespace do not change the hash.
    reordered = {"cases": [dict(reversed(list(c.items()))) for c in DATASET["cases"]]}
    b = _write(tmp_path / "b.json", reordered)
    assert load_dataset([b]).hash == ds.hash


def test_load_dataset_merges_files_and_rejects_duplicates(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.json", {"cases": [DATASET["cases"][0]]})
    b = _write(tmp_path / "b.json", {"cases": [DATASET["cases"][1]]})
    assert load_dataset([a, b]).case_ids == ["greeting", "weather"]
    with pytest.raises(EvalConfigError, match="duplicate case id"):
        load_dataset([a, a])
    with pytest.raises(EvalConfigError, match="has no cases"):
        load_dataset([_write(tmp_path / "empty.json", {"cases": []})])
    with pytest.raises(EvalConfigError, match="not valid JSON"):
        (tmp_path / "bad.json").write_text("{", encoding="utf-8")
        load_dataset([tmp_path / "bad.json"])


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"messages": [{"role": "user", "content": "x"}]}, "'id'"),
        ({"id": "a", "messages": []}, "non-empty list"),
        ({"id": "a", "messages": [{"role": "assistant", "content": "x"}]}, "user message"),
        ({"id": "a", "messages": [{"role": "bot", "content": "x"}]}, "role must be"),
        (
            {"id": "a", "messages": [{"role": "user", "content": "x"}], "expect": {"nope": 1}},
            "unknown expect key",
        ),
        (
            {"id": "a", "messages": [{"role": "user", "content": "x"}], "expect": {"contains": 5}},
            "list of strings",
        ),
        (
            {
                "id": "a",
                "messages": [{"role": "user", "content": "x"}],
                "expect": {"no_tool_calls": True, "tool_calls": [{"name": "t"}]},
            },
            "conflict",
        ),
        (
            {
                "id": "a",
                "messages": [{"role": "user", "content": "x"}],
                "judge": {"rq": {"threshold": "high"}},
            },
            "must be a number",
        ),
        (
            {"id": "a", "messages": [{"role": "user", "content": "x"}], "judge": "rq"},
            "'judge' must be an object",
        ),
    ],
)
def test_parse_case_rejects_malformed_cases(raw: dict, message: str) -> None:
    with pytest.raises(EvalConfigError, match=message):
        parse_case(raw)


def test_parse_case_normalises_shorthands() -> None:
    case = parse_case(
        {
            "id": "a",
            "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}],
            "expect": {"contains": "one", "tool_calls": ["get_weather"]},
            "judge": {"response_quality": 3, "task_success": None},
        }
    )
    assert case.expect["contains"] == ["one"]
    assert case.expect["tool_calls"] == [{"name": "get_weather"}]
    assert case.expect["ordered"] is False and case.expect["max_tokens"] is None
    assert case.judge == {"response_quality": {"threshold": 3}, "task_success": {}}
    assert case.user_messages() == ["q"]
    assert "system: s" in case.conversation_text()


def test_eval_config_defaults_and_overrides(tmp_path: Path) -> None:
    config = load_eval_config(tmp_path / "missing.yaml")
    assert set(config.judges) == set(BUILTIN_JUDGES)
    assert config.quality_metrics == {} and config.custom_metrics == {}
    assert config.judge_provider is None

    path = tmp_path / "eval_config.yaml"
    path.write_text(
        """
judge: {provider: openai, model: gpt-5-mini}
quality_metrics:
  response_quality: {threshold: 4, min_pass_rate: 0.8}
  helpfulness: 3
judges:
  response_quality: {rubric: "custom rubric", scale: 10}
  helpfulness: {rubric: "be helpful", threshold: 3}
custom_metrics:
  - tests.eval.metrics:word_count
  - {name: has_greeting, callable: "tests.eval.metrics:greeting", threshold: 0.5}
""",
        encoding="utf-8",
    )
    config = load_eval_config(path)
    assert config.judge_provider == "openai" and config.judge_model == "gpt-5-mini"
    assert config.judges["response_quality"].rubric == "custom rubric"
    assert config.judges["response_quality"].scale == 10
    assert config.judges["response_quality"].builtin is True
    assert config.judges["helpfulness"].builtin is False
    assert config.quality_metrics["response_quality"].min_pass_rate == 0.8
    assert config.quality_metrics["helpfulness"].threshold == 3
    assert config.quality_metrics["helpfulness"].min_pass_rate == 1.0
    assert config.custom_metrics["word_count"].callable == "tests.eval.metrics:word_count"
    assert config.custom_metrics["has_greeting"].threshold == 0.5
    assert config.is_quality("response_quality") and not config.is_quality("task_success")


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"quality_metrics": {"response_quality": {"min_pass_rate": 0.5}}}, "no threshold"),
        ({"quality_metrics": {"nope": {"threshold": 1}}}, "not a known judge"),
        (
            {"quality_metrics": {"response_quality": {"threshold": 4, "min_pass_rate": 2}}},
            "between 0 and 1",
        ),
        ({"judges": {"mine": {"scale": 5}}}, "needs a 'rubric'"),
        ({"custom_metrics": ["no_colon"]}, "module:function"),
        ({"custom_metrics": ["m:response_quality"]}, "both as judge and custom"),
        ({"other": 1}, "unknown top-level"),
        ([], "must be a mapping"),
    ],
)
def test_eval_config_rejects_bad_config(data, message: str) -> None:
    with pytest.raises(EvalConfigError, match=message):
        parse_eval_config(data)


def test_resolve_threshold_precedence() -> None:
    config = parse_eval_config(
        {
            "quality_metrics": {"response_quality": {"threshold": 4}},
            "judges": {"task_success": {"rubric": "r", "threshold": 2}},
            "custom_metrics": [{"callable": "m:f", "threshold": 0.7}],
        }
    )
    assert resolve_threshold(config, "response_quality", {"threshold": 3}) == 3
    assert resolve_threshold(config, "response_quality", {}) == 4
    assert resolve_threshold(config, "task_success", None) == 2
    assert resolve_threshold(config, "f", None) == 0.7
    with pytest.raises(EvalConfigError, match="has no threshold"):
        resolve_threshold(config, "groundedness", {})


def test_render_judge_prompt_includes_sections() -> None:
    config = parse_eval_config({})
    prompt = render_judge_prompt(
        config.judges["groundedness"],
        conversation="user: hi",
        response="hello",
        reference="hello there",
        context="ctx",
        tool_calls=[{"name": "t", "args": {"a": 1}, "result": "r", "is_error": True}],
    )
    assert "groundedness" in prompt and "user: hi" in prompt and "hello" in prompt
    assert "Reference answer" in prompt and "Grounding context" in prompt
    assert 't({"a": 1}) -> r (error)' in prompt
    assert '{"score": <number>' in prompt
    # An unknown placeholder fails when the config loads, before any agent call.
    with pytest.raises(EvalConfigError, match=r"unknown placeholder.*allowed: \{metric\}"):
        parse_eval_config({"judges": {"x": {"rubric": "r", "prompt_template": "{nope}"}}})
    with pytest.raises(EvalConfigError, match="unknown placeholder"):
        parse_eval_config({"judges": {"response_quality": {"prompt_template": "{0}"}}})
    # {transcript} is a known placeholder; built without one it is assembled.
    config = parse_eval_config(
        {"judges": {"x": {"rubric": "r", "prompt_template": "T:{transcript}|R:{response}"}}}
    )
    prompt = render_judge_prompt(
        config.judges["x"],
        conversation="user: hi",
        response="hello",
        reference=None,
        context=None,
        tool_calls=[{"name": "t", "args": {}, "result": "r"}],
    )
    assert prompt == "T:user: hi\nagent tool call: t() -> r\nagent: hello|R:hello"


def test_resolve_input_datasets(tmp_path: Path) -> None:
    assert _paths.resolve_input_datasets(tmp_path, None) == []
    datasets = tmp_path / _paths.DATASETS_DIR
    _write(datasets / "zeta.json", DATASET)
    _write(datasets / "alpha.json", DATASET)
    assert [p.name for p in _paths.resolve_input_datasets(tmp_path, None)] == [
        "alpha.json",
        "zeta.json",
    ]
    _write(tmp_path / _paths.DEFAULT_INPUT_DATASET, DATASET)
    assert [p.name for p in _paths.resolve_input_datasets(tmp_path, None)] == ["basic-dataset.json"]
    assert _paths.resolve_input_datasets(tmp_path, str(datasets)) == sorted(datasets.glob("*.json"))
    assert _paths.resolve_input_datasets(tmp_path, "tests/eval/datasets/zeta.json") == [
        datasets / "zeta.json"
    ]
    assert (
        _paths.resolve_output_path(tmp_path, "out/", default_dir=tmp_path, prefix="x").parent
        == tmp_path / "out"
    )
    assert (
        _paths.resolve_output_path(tmp_path, "file.json", default_dir=tmp_path, prefix="x")
        == tmp_path / "file.json"
    )


def test_project_meta_and_judge_identity(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    meta = project_meta(project)
    assert meta["name"] == "demo-agent" and meta["agent_version"] == "0.3.1"
    assert meta["model"] == "fake/fake-model" and meta["capture"] == "metadata"
    identity = resolve_judge_identity(
        project, config_provider=None, config_model=None, flag_provider=None, flag_model=None
    )
    assert identity == {"provider": "fake", "model": "fake-model"}
    monkeypatch.setenv("JUDGE_MODEL_PROVIDER", "openai")
    identity = resolve_judge_identity(
        project, config_provider=None, config_model="m", flag_provider=None, flag_model=None
    )
    assert identity == {"provider": "openai", "model": "m"}
    identity = resolve_judge_identity(
        project, config_provider="x", config_model="m", flag_provider="flag", flag_model=None
    )
    assert identity["provider"] == "flag"
    assert project_meta(None)["model"] is None
