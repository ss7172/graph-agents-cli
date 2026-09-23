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

"""Judge runner for ``graph-agents-cli eval grade`` and ``eval analyze --judge``.

This file is staged into the user's project as ``.graph-agents-cli/judge_runner.py``
and executed there with ``uv run python .graph-agents-cli/judge_runner.py
<input.json> <output.json>`` so it runs inside the project's environment.
Do not edit the staged copy; it is overwritten on every run.

It imports ``app.app_utils.model.get_judge_model()`` (the template contract:
no arguments, judge chosen from ``JUDGE_MODEL_PROVIDER`` / ``JUDGE_MODEL_NAME``)
and ``langchain_core`` lazily; under the ``fake`` provider it returns a
deterministic score equal to the scale so tests and CI run without keys.
It never imports graph_agents_cli.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any

FAKE_PROVIDER = "fake"


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv  # the template depends on python-dotenv
    except ImportError:
        return
    load_dotenv(Path.cwd() / ".env")


def resolve_provider(payload: dict[str, Any]) -> str | None:
    judge = payload.get("judge") or {}
    return (
        judge.get("provider")
        or os.environ.get("JUDGE_MODEL_PROVIDER")
        or os.environ.get("MODEL_PROVIDER")
        or None
    )


def is_fake(provider: str | None) -> bool:
    return (provider or "").lower() == FAKE_PROVIDER


def build_model(provider: str | None, model: str | None) -> Any:
    """``app.app_utils.model.get_judge_model()`` from the project.

    The template resolves the judge from ``JUDGE_MODEL_PROVIDER`` /
    ``JUDGE_MODEL_NAME`` (falling back to ``MODEL_*``) when called, so the
    overrides the CLI resolved travel through the environment, set after the
    import so an import-time ``.env`` load cannot shadow them.
    """
    if str(Path.cwd()) not in sys.path:
        sys.path.insert(0, str(Path.cwd()))
    module = importlib.import_module("app.app_utils.model")
    if provider:
        os.environ["JUDGE_MODEL_PROVIDER"] = provider
    if model:
        os.environ["JUDGE_MODEL_NAME"] = model
    return module.get_judge_model()


def invoke_model(model: Any, prompt: str) -> str:
    """Send ``prompt`` to a LangChain chat model and return the text of its reply."""
    try:
        from langchain_core.messages import HumanMessage

        result = model.invoke([HumanMessage(content=prompt)])
    except ImportError:
        result = model.invoke(prompt)
    content = getattr(result, "content", result)
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text", "")))
            else:
                parts.append(str(part))
        content = "".join(parts)
    return str(content)


_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
_SCORE = re.compile(r"score\W{0,5}(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def parse_score(text: str) -> tuple[float, str]:
    """Extract ``(score, reasoning)`` from a judge reply.

    Accepts a JSON object (optionally fenced or embedded in prose) with a
    ``score`` key, a ``score: N`` phrase, or, as a last resort, a bare number.
    Raises ``ValueError`` when no score can be read.
    """
    stripped = text.strip()
    candidates = [stripped]
    match = _JSON_OBJECT.search(stripped)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and "score" in data:
            score = float(data["score"])
            reasoning = data.get("reasoning") or data.get("explanation") or ""
            return score, str(reasoning)
        if isinstance(data, int | float) and not isinstance(data, bool):
            return float(data), ""
    match = _SCORE.search(stripped)
    if match:
        return float(match.group(1)), stripped
    match = _NUMBER.fullmatch(stripped)
    if match:
        return float(match.group(0)), ""
    raise ValueError(f"could not parse a score from judge reply: {stripped[:200]!r}")


def run_custom(item: dict[str, Any]) -> tuple[float, str]:
    """Import ``module:function`` from the project and call it with ``(case, trace)``."""
    target = item["callable"]
    module_name, _, func_name = target.partition(":")
    if str(Path.cwd()) not in sys.path:
        sys.path.insert(0, str(Path.cwd()))
    module = importlib.import_module(module_name)
    func = getattr(module, func_name)
    result = func(item.get("case") or {}, item.get("trace") or {})
    if isinstance(result, bool):
        return (1.0 if result else 0.0), ""
    if isinstance(result, int | float):
        return float(result), ""
    if isinstance(result, dict) and "score" in result:
        reasoning = result.get("reasoning") or result.get("explanation") or ""
        score = result["score"]
        if isinstance(score, bool):
            score = 1.0 if score else 0.0
        return float(score), str(reasoning)
    raise ValueError(
        f"{target} returned {type(result).__name__}; expected bool, number, or {{'score': ...}}"
    )


def run_items(payload: dict[str, Any], model: Any = None, *, fake: bool = False) -> dict[str, Any]:
    """Score every item; per-item failures become ``error`` entries, never exceptions."""
    results: list[dict[str, Any]] = []
    for item in payload.get("items", []):
        entry: dict[str, Any] = {
            "id": item.get("id"),
            "case_id": item.get("case_id"),
            "metric": item.get("metric"),
            "score": None,
            "reasoning": "",
            "error": None,
        }
        kind = item.get("kind", "judge")
        try:
            if kind == "custom":
                entry["score"], entry["reasoning"] = run_custom(item)
            elif kind == "summarize":
                if fake:
                    entry["reasoning"] = "fake provider: no summary generated"
                else:
                    entry["reasoning"] = invoke_model(model, item["prompt"])
            else:
                scale = float(item.get("scale") or 5)
                if fake:
                    entry["score"] = scale
                    entry["reasoning"] = "fake provider: deterministic score"
                else:
                    text = invoke_model(model, item["prompt"])
                    entry["score"], entry["reasoning"] = parse_score(text)
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
        results.append(entry)
    return {"results": results}


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: judge_runner.py <input.json> <output.json>", file=sys.stderr)
        return 2
    input_path, output_path = Path(argv[1]), Path(argv[2])
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    _load_dotenv()
    provider = resolve_provider(payload)
    model_name = (payload.get("judge") or {}).get("model") or os.environ.get("JUDGE_MODEL_NAME")
    fake = is_fake(provider)
    needs_model = any(i.get("kind", "judge") != "custom" for i in payload.get("items", []))
    model = None
    if needs_model and not fake:
        try:
            model = build_model(provider, model_name)
        except Exception:
            print("judge runner: could not build the judge model", file=sys.stderr)
            traceback.print_exc()
            return 1
    output = run_items(payload, model, fake=fake)
    output["provider"] = provider
    output["model"] = model_name or (getattr(model, "model_name", None) if model else None)
    output_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
