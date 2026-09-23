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

"""The judge runner script's logic (imported from source) and its staging/invocation."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from graph_agents_cli.eval import _judge
from graph_agents_cli.eval._common import EvalConfigError

RUNNER = Path(_judge.__file__).with_name(_judge.RUNNER_SOURCE)


@pytest.fixture
def runner_module():
    spec = importlib.util.spec_from_file_location("judge_runner_under_test", RUNNER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeModel:
    """Looks like a LangChain chat model: ``invoke`` returns something with ``.content``."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def invoke(self, messages):
        prompt = messages if isinstance(messages, str) else str(messages)
        self.prompts.append(prompt)
        return SimpleNamespace(content=self.replies.pop(0))


def test_runner_does_not_import_frameworks_at_module_level(runner_module) -> None:
    for name in ("langchain_core", "langchain", "langgraph", "graph_agents_cli"):
        assert not any(
            m == name or m.startswith(name + ".")
            for m in vars(runner_module).values()
            if isinstance(m, str)
        )
    assert "langchain_core" not in sys.modules or True  # import happens lazily inside invoke_model


@pytest.mark.parametrize(
    ("text", "score", "reasoning"),
    [
        ('{"score": 4, "reasoning": "good"}', 4.0, "good"),
        ('Sure!\n```json\n{"score": 2.5, "explanation": "meh"}\n```', 2.5, "meh"),
        ("Score: 3/5 because it is fine", 3.0, "Score: 3/5 because it is fine"),
        ("5", 5.0, ""),
    ],
)
def test_parse_score_variants(runner_module, text: str, score: float, reasoning: str) -> None:
    assert runner_module.parse_score(text) == (score, reasoning)


def test_parse_score_rejects_unparseable(runner_module) -> None:
    with pytest.raises(ValueError, match="could not parse"):
        runner_module.parse_score("no verdict here")


def test_run_items_with_injected_fake_model(runner_module) -> None:
    model = FakeModel(['{"score": 4, "reasoning": "clear"}', "garbage"])
    payload = {
        "items": [
            {
                "id": "a/response_quality",
                "kind": "judge",
                "case_id": "a",
                "metric": "response_quality",
                "prompt": "P1",
                "scale": 5,
            },
            {
                "id": "b/response_quality",
                "kind": "judge",
                "case_id": "b",
                "metric": "response_quality",
                "prompt": "P2",
                "scale": 5,
            },
        ]
    }
    output = runner_module.run_items(payload, model)
    first, second = output["results"]
    assert first["score"] == 4.0 and first["reasoning"] == "clear" and first["error"] is None
    assert second["score"] is None and "ValueError" in second["error"]
    assert model.prompts and "P1" in model.prompts[0]


def test_run_items_fake_provider_scores_the_scale(runner_module) -> None:
    payload = {
        "items": [
            {
                "id": "a/rq",
                "kind": "judge",
                "case_id": "a",
                "metric": "rq",
                "prompt": "P",
                "scale": 7,
            },
            {"id": "summary", "kind": "summarize", "prompt": "S"},
        ]
    }
    output = runner_module.run_items(payload, None, fake=True)
    assert output["results"][0]["score"] == 7.0
    assert "fake" in output["results"][0]["reasoning"]
    assert output["results"][1]["score"] is None and output["results"][1]["reasoning"]


def test_run_custom_metric_from_project_cwd(
    runner_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkg = tmp_path / "evalmetrics"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "custom.py").write_text(
        "def word_count(case, trace):\n"
        "    return {'score': len((trace.get('response') or '').split()), 'reasoning': 'words'}\n"
        "def is_short(case, trace):\n"
        "    return len(trace.get('response') or '') < 10\n"
        "def broken(case, trace):\n"
        "    return object()\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    items = [
        {
            "id": "a/word_count",
            "kind": "custom",
            "case_id": "a",
            "metric": "word_count",
            "callable": "evalmetrics.custom:word_count",
            "case": {},
            "trace": {"response": "one two three"},
        },
        {
            "id": "a/is_short",
            "kind": "custom",
            "case_id": "a",
            "metric": "is_short",
            "callable": "evalmetrics.custom:is_short",
            "case": {},
            "trace": {"response": "hi"},
        },
        {
            "id": "a/broken",
            "kind": "custom",
            "case_id": "a",
            "metric": "broken",
            "callable": "evalmetrics.custom:broken",
            "case": {},
            "trace": {},
        },
        {
            "id": "a/missing",
            "kind": "custom",
            "case_id": "a",
            "metric": "missing",
            "callable": "evalmetrics.nope:f",
            "case": {},
            "trace": {},
        },
    ]
    output = runner_module.run_items({"items": items}, None, fake=True)
    by_id = {r["id"]: r for r in output["results"]}
    assert by_id["a/word_count"]["score"] == 3.0 and by_id["a/word_count"]["reasoning"] == "words"
    assert by_id["a/is_short"]["score"] == 1.0
    assert "expected bool, number" in by_id["a/broken"]["error"]
    assert "ModuleNotFoundError" in by_id["a/missing"]["error"]


def test_build_model_uses_env_overrides_and_no_arg_get_judge_model(
    runner_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The template contract: get_judge_model() takes no args and reads JUDGE_MODEL_*."""
    calls: list[dict] = []

    def get_judge_model(**kwargs):
        calls.append(
            {
                "kwargs": kwargs,
                "provider": os.environ.get("JUDGE_MODEL_PROVIDER"),
                "model": os.environ.get("JUDGE_MODEL_NAME"),
            }
        )
        return FakeModel(['{"score": 3}'])

    app = types.ModuleType("app")
    app_utils = types.ModuleType("app.app_utils")
    model_mod = types.ModuleType("app.app_utils.model")
    model_mod.get_judge_model = get_judge_model
    monkeypatch.setitem(sys.modules, "app", app)
    monkeypatch.setitem(sys.modules, "app.app_utils", app_utils)
    monkeypatch.setitem(sys.modules, "app.app_utils.model", model_mod)
    monkeypatch.delenv("JUDGE_MODEL_PROVIDER", raising=False)
    monkeypatch.delenv("JUDGE_MODEL_NAME", raising=False)
    monkeypatch.chdir(tmp_path)

    model = runner_module.build_model("openai", "gpt-5-mini")
    assert calls == [{"kwargs": {}, "provider": "openai", "model": "gpt-5-mini"}]
    payload = {"items": [{"id": "a/rq", "kind": "judge", "prompt": "P", "scale": 5}]}
    assert runner_module.run_items(payload, model)["results"][0]["score"] == 3.0
    runner_module.build_model(None, None)
    assert (
        calls[1]["kwargs"] == {} and calls[1]["provider"] == "openai"
    )  # untouched without override


def test_runner_main_end_to_end_under_fake_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Execute the real script as a subprocess: no project, no LangChain, provider fake."""
    monkeypatch.chdir(tmp_path)
    payload = {
        "judge": {"provider": "fake", "model": "fake-judge"},
        "items": [
            {
                "id": "a/rq",
                "kind": "judge",
                "case_id": "a",
                "metric": "rq",
                "prompt": "P",
                "scale": 5,
            }
        ],
    }
    (tmp_path / "in.json").write_text(json.dumps(payload), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(RUNNER), "in.json", "out.json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    output = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert output["provider"] == "fake" and output["model"] == "fake-judge"
    assert output["results"][0]["score"] == 5.0


def test_runner_main_fails_cleanly_without_a_project(tmp_path: Path) -> None:
    payload = {
        "judge": {"provider": "openai"},
        "items": [{"id": "a/rq", "kind": "judge", "prompt": "P", "scale": 5}],
    }
    (tmp_path / "in.json").write_text(json.dumps(payload), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(RUNNER), "in.json", "out.json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "", "MODEL_PROVIDER": "openai"},
    )
    assert proc.returncode == 1
    assert "could not build the judge model" in proc.stderr
    assert not (tmp_path / "out.json").exists()


def test_stage_runner_copies_into_run_state_dir(project: Path) -> None:
    staged = _judge.stage_runner(project)
    assert staged == project / ".graph-agents-cli" / "judge_runner.py"
    assert staged.read_text(encoding="utf-8") == RUNNER.read_text(encoding="utf-8")


def test_run_judge_runner_invokes_uv_in_project_and_parses_output(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []

    def fake_run_resolved(args, **kwargs):
        calls.append({"args": args, **kwargs})
        input_path = project / args[4]
        output_path = project / args[5]
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        results = [
            {"id": i["id"], "score": 4, "reasoning": "r", "error": None} for i in payload["items"]
        ]
        output_path.write_text(
            json.dumps({"provider": "fake", "model": "m", "results": results}), encoding="utf-8"
        )
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("graph_agents_cli._runner.run_resolved", fake_run_resolved)
    output = _judge.run_judge_runner(
        project, {"judge": {}, "items": [{"id": "a/rq", "kind": "judge", "prompt": "P"}]}
    )
    assert output["results"][0]["score"] == 4
    call = calls[0]
    assert call["args"][:4] == ["uv", "run", "python", ".graph-agents-cli/judge_runner.py"]
    assert call["cwd"] == str(project)
    assert "JUDGE_MODEL_PROVIDER" not in call["env"]
    assert not list((project / ".graph-agents-cli").glob("judge_input_*.json"))
    _judge.run_judge_runner(project, {"judge": {"provider": "openai", "model": "m"}, "items": []})
    assert calls[1]["env"]["JUDGE_MODEL_PROVIDER"] == "openai"
    assert calls[1]["env"]["JUDGE_MODEL_NAME"] == "m"


def test_run_judge_runner_failure_is_a_configuration_error(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(args, **kwargs):
        return subprocess.CompletedProcess(
            args, 1, "", "ImportError: no module named app.app_utils.model"
        )

    monkeypatch.setattr("graph_agents_cli._runner.run_resolved", failing)
    with pytest.raises(EvalConfigError, match="unreachable or misconfigured") as info:
        _judge.run_judge_runner(project, {"judge": {}, "items": []})
    assert info.value.exit_code == 3

    def timeout(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout", 1))

    monkeypatch.setattr("graph_agents_cli._runner.run_resolved", timeout)
    with pytest.raises(EvalConfigError, match="timed out"):
        _judge.run_judge_runner(project, {"judge": {}, "items": []}, timeout=1)
