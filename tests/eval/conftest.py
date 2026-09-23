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

"""Fixtures for the eval tests: a scaffolded-looking project, datasets, traces, fakes.

No test here needs network, a cluster, or a model key: the chat client, the
local server manager and the judge runner are replaced by fakes.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from graph_agents_cli.eval import _paths
from graph_agents_cli.eval._common import write_json_file
from graph_agents_cli.eval.dataset import dataset_hash

DATASET: dict[str, Any] = {
    "cases": [
        {
            "id": "greeting",
            "messages": [{"role": "user", "content": "hi"}],
            "expect": {"contains": ["hello"]},
            "judge": {"response_quality": {"threshold": 4}},
            "reference": "hello there",
        },
        {
            "id": "weather",
            "messages": [{"role": "user", "content": "weather in SF?"}],
            "expect": {
                "tool_calls": [{"name": "get_weather", "args_subset": {"query": "SF"}}],
                "ordered": False,
            },
            "judge": {"task_success": {"threshold": 3}},
            "context": "SF is sunny.",
        },
        {
            "id": "plain",
            "messages": [{"role": "user", "content": "say nothing special"}],
            "expect": {"no_tool_calls": True, "max_latency_ms": 5000, "max_tokens": 1000},
        },
    ]
}

CONFIG_YAML = """\
judge:
  provider: fake
  model: fake-judge
quality_metrics:
  response_quality: { threshold: 4, min_pass_rate: 0.5 }
"""

MANIFEST_YAML = """\
name: demo-agent
cli_version: 0.1.0
agent_directory: app
base_template: langgraph
language: python
create_params:
  deployment_target: none
  runtime: fastapi
  model_provider: fake
  model: fake-model
  checkpointer: memory
  cd: skip
  auth_policy: shared-bearer
"""

PYPROJECT = """\
[project]
name = "demo-agent"
version = "0.3.1"
"""

ENV_FILE = "MODEL_PROVIDER=fake\nMODEL_NAME=fake-model\nAPI_KEY=local-secret\n"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A minimal project: manifest, pyproject, .env, dataset, eval config; cwd inside it."""
    root = tmp_path / "demo-agent"
    root.mkdir()
    (root / "graph-agents-cli-manifest.yaml").write_text(MANIFEST_YAML, encoding="utf-8")
    (root / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    (root / ".env").write_text(ENV_FILE, encoding="utf-8")
    (root / "app").mkdir()
    dataset_file = root / _paths.DEFAULT_INPUT_DATASET
    write_json_file(dataset_file, DATASET)
    (root / _paths.DEFAULT_EVAL_CONFIG).write_text(CONFIG_YAML, encoding="utf-8")
    monkeypatch.chdir(root)
    for var in (
        "GRAPH_AGENTS_CLI_API_KEY",
        "LANGSMITH_API_KEY",
        "JUDGE_MODEL_PROVIDER",
        "MODEL_PROVIDER",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GRAPH_AGENTS_CLI_NO_UPDATE_CHECK", "1")
    monkeypatch.setenv("GRAPH_AGENTS_CLI_DISABLE_OVERRIDES", "1")
    return root


@pytest.fixture
def dataset_digest() -> str:
    return dataset_hash(DATASET["cases"])


def make_trace(
    case_id: str,
    *,
    response: str | None = "hello there",
    tool_calls: list[dict[str, Any]] | None = None,
    status: str = "ok",
    latency_ms: float | None = 120,
    usage: dict[str, Any] | None = None,
    error: str | None = None,
    case: dict[str, Any] | None = None,
) -> dict[str, Any]:
    trace = {
        "case_id": case_id,
        "status": status,
        "response": response,
        "tool_calls": tool_calls or [],
        "usage": {"input_tokens": 10, "output_tokens": 20} if usage is None else usage,
        "latency_ms": latency_ms,
        "error": error,
        "thread_id": f"thread-{case_id}",
        "run_id": f"run-{case_id}",
        "agent_version": "0.3.1",
        "model": "fake/fake-model",
    }
    if case is None:
        case = next((c for c in DATASET["cases"] if c["id"] == case_id), None)
    if case is not None:
        trace["case"] = case
    return trace


def good_traces() -> list[dict[str, Any]]:
    """Traces that pass every deterministic check of DATASET."""
    return [
        make_trace("greeting"),
        make_trace(
            "weather",
            response="It is sunny in SF.",
            tool_calls=[
                {
                    "id": "c1",
                    "name": "get_weather",
                    "args": {"query": "SF", "units": "c"},
                    "result": "sunny",
                    "is_error": False,
                }
            ],
        ),
        make_trace("plain", response="ok"),
    ]


def write_traces(
    root: Path,
    traces: list[dict[str, Any]],
    *,
    digest: str | None = None,
    path: Path | None = None,
    generated_at: str = "2026-09-22T00:00:00+00:00",
) -> Path:
    doc = {
        "dataset_hash": digest or dataset_hash(DATASET["cases"]),
        "dataset_paths": [_paths.DEFAULT_INPUT_DATASET],
        "generated_at": generated_at,
        "agent_version": "0.3.1",
        "model": "fake/fake-model",
        "traces": traces,
    }
    path = path or (_paths.default_traces_dir(root) / "traces_20260922_000000.json")
    write_json_file(path, doc)
    return path


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class FakeJudge:
    """Stand-in for ``run_judge_runner``: scores by ``case_id/metric`` or a default."""

    def __init__(self, scores: dict[str, Any] | None = None, default: float = 5.0) -> None:
        self.scores = scores or {}
        self.default = default
        self.calls: list[dict[str, Any]] = []
        self.fail_with: Exception | None = None

    def __call__(
        self, project_root: Path, payload: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append(payload)
        if self.fail_with is not None:
            raise self.fail_with
        results = []
        for item in payload["items"]:
            value = self.scores.get(item["id"], self.default)
            entry = {
                "id": item["id"],
                "case_id": item.get("case_id"),
                "metric": item.get("metric"),
                "score": None,
                "reasoning": "",
                "error": None,
            }
            if isinstance(value, Exception) or isinstance(value, str):
                entry["error"] = str(value)
            else:
                entry["score"] = float(value)
                entry["reasoning"] = "fake judge"
            results.append(entry)
        return {"provider": "fake", "model": "fake-judge", "results": results}


@pytest.fixture
def fake_judge(monkeypatch: pytest.MonkeyPatch) -> FakeJudge:
    judge = FakeJudge()
    monkeypatch.setattr("graph_agents_cli.eval.cmd_grade.run_judge_runner", judge)
    monkeypatch.setattr("graph_agents_cli.eval.cmd_analyze.run_judge_runner", judge)
    return judge


def sse(event: str, data: dict[str, Any]) -> Any:
    """An event as ``_chat_client.post_chat`` yields it."""
    from graph_agents_cli._chat_client import SseEvent

    return SseEvent(event, data, json.dumps(data))


def ok_stream(
    text: str, *, tool_calls: list[dict[str, Any]] | None = None, thread_id: str = "t-1"
) -> list[Any]:
    events = [sse("message.start", {"thread_id": thread_id, "run_id": "r-1"})]
    for call in tool_calls or []:
        events.append(
            sse("tool.call", {"id": call["id"], "name": call["name"], "args": call["args"]})
        )
        events.append(
            sse(
                "tool.result",
                {
                    "id": call["id"],
                    "name": call["name"],
                    "result": call.get("result", "ok"),
                    "is_error": False,
                },
            )
        )
    for part in (text[: len(text) // 2], text[len(text) // 2 :]):
        if part:
            events.append(sse("message.delta", {"text": part}))
    events.append(
        sse(
            "message.end",
            {
                "thread_id": thread_id,
                "run_id": "r-1",
                "usage": {"input_tokens": 5, "output_tokens": 7},
                "latency_ms": 42,
                "status": "ok",
            },
        )
    )
    return events


class FakeChat:
    """Replacement for ``_chat_client.post_chat`` driven by a message -> events map."""

    def __init__(self, streams: dict[str, Any]) -> None:
        self.streams = streams
        self.calls: list[dict[str, Any]] = []

    def __call__(self, base_url: str, message: str, **kwargs: Any):
        self.calls.append({"base_url": base_url, "message": message, **kwargs})
        stream = self.streams.get(message, self.streams.get("*"))
        if isinstance(stream, Exception):
            raise stream
        if callable(stream):
            stream = stream(message, kwargs)
        yield from stream or []


@pytest.fixture
def fake_chat(monkeypatch: pytest.MonkeyPatch):
    def _install(streams: dict[str, Any]) -> FakeChat:
        chat = FakeChat(streams)
        monkeypatch.setattr("graph_agents_cli._chat_client.post_chat", chat)
        return chat

    return _install


@pytest.fixture
def fake_local_server(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """``run._local_server.ensure_server``/``stop_server`` replaced by recorders."""
    state = SimpleNamespace(
        started=0, stopped=0, base_url="http://127.0.0.1:18080", pid=4242, calls=[]
    )

    def ensure_server(
        project_root: Path,
        agent_dir: str,
        *,
        runtime: str,
        checkpointer: str = "memory",
        **kwargs: Any,
    ):
        """Same signature as run._local_server.ensure_server."""
        state.started += 1
        state.calls.append(
            {
                "project_root": project_root,
                "agent_dir": agent_dir,
                "runtime": runtime,
                "checkpointer": checkpointer,
            }
        )
        return SimpleNamespace(base_url=state.base_url, port=18080, started=True, pid=state.pid)

    def stop_server(project_root: Path, pid: int | None = None) -> bool:
        state.stopped += 1
        state.stopped_pid = pid
        return True

    from graph_agents_cli.run import _local_server

    monkeypatch.setattr(_local_server, "ensure_server", ensure_server)
    monkeypatch.setattr(_local_server, "stop_server", stop_server)
    return state


def read_results(root: Path) -> dict[str, Any]:
    latest = _paths.latest_file(_paths.default_grade_results_dir(root), _paths.RESULTS_FILE_PREFIX)
    assert latest is not None, "no results file written"
    return json.loads(latest.read_text(encoding="utf-8"))


def read_traces(root: Path) -> dict[str, Any]:
    latest = _paths.latest_file(_paths.default_traces_dir(root), _paths.TRACES_FILE_PREFIX)
    assert latest is not None, "no traces file written"
    return json.loads(latest.read_text(encoding="utf-8"))
