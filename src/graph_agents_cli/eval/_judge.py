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

"""Stage and run the judge runner inside the project's environment.

The CLI never imports LangChain. ``_judge_runner.py`` is copied to
``<project>/.graph-agents-cli/judge_runner.py`` and executed with
``uv run python .graph-agents-cli/judge_runner.py <input.json> <output.json>``
from the project root, so it resolves ``app.app_utils.model.get_judge_model()``
and ``langchain_core`` from the project's own lock. Prompts are rendered here;
the runner only invokes the model (or a custom metric callable) and parses
scores.

Input payload::

    {"judge": {"provider": null, "model": null},
     "items": [{"id": "greeting/response_quality", "kind": "judge",
                "case_id": "greeting", "metric": "response_quality",
                "prompt": "...", "scale": 5},
               {"id": "greeting/my_metric", "kind": "custom", "case_id": "...",
                "metric": "my_metric", "callable": "module:function",
                "case": {...}, "trace": {...}},
               {"id": "summary", "kind": "summarize", "prompt": "..."}]}

Output payload::

    {"provider": "...", "model": "...",
     "results": [{"id": "...", "case_id": "...", "metric": "...",
                  "score": 4, "reasoning": "...", "error": null}]}
"""

from __future__ import annotations

import os
import shutil
import subprocess
from importlib import resources
from pathlib import Path
from typing import Any

from graph_agents_cli.eval import _paths
from graph_agents_cli.eval._common import EvalConfigError, load_json_file, write_json_file

RUNNER_SOURCE = "_judge_runner.py"
RUNNER_STAGED_NAME = "judge_runner.py"
DEFAULT_TIMEOUT = 600  # seconds


def stage_runner(project_root: Path) -> Path:
    """Copy the runner into ``<project>/.graph-agents-cli/judge_runner.py``.

    Loaded through :mod:`importlib.resources` so it works from a wheel or an
    editable install. Overwritten on every run; the file is not removed
    afterwards (it lives in the run-state directory, which is gitignored by
    the template).
    """
    dest_dir = _paths.stage_dir(project_root)
    dest = dest_dir / RUNNER_STAGED_NAME
    src = resources.files("graph_agents_cli.eval").joinpath(RUNNER_SOURCE)
    with resources.as_file(src) as src_path:
        shutil.copyfile(src_path, dest)
    return dest


def run_judge_runner(
    project_root: Path,
    payload: dict[str, Any],
    *,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Execute the runner over ``payload`` and return its parsed output.

    A runner that cannot start, cannot build the judge model, or exits
    non-zero is an *unreachable judge*: :class:`EvalConfigError` (exit 3).
    Per-item failures come back in ``results[*].error`` and are the caller's
    to map onto case statuses.
    """
    from graph_agents_cli._runner import run_resolved

    project_root = Path(project_root)
    script = stage_runner(project_root)
    stamp = _paths.timestamp()
    stage = _paths.stage_dir(project_root)
    input_path = stage / f"judge_input_{stamp}.json"
    output_path = stage / f"judge_output_{stamp}.json"
    write_json_file(input_path, payload)

    rel_script = script.relative_to(project_root)
    cmd = [
        "uv",
        "run",
        "python",
        str(rel_script),
        str(input_path.relative_to(project_root)),
        str(output_path.relative_to(project_root)),
    ]
    # The template's get_judge_model() reads JUDGE_MODEL_PROVIDER / JUDGE_MODEL_NAME;
    # the runner sets them as well, but a child that loads .env at import must
    # already see the resolved values.
    env = dict(os.environ)
    judge = payload.get("judge") or {}
    if judge.get("provider"):
        env["JUDGE_MODEL_PROVIDER"] = str(judge["provider"])
    if judge.get("model"):
        env["JUDGE_MODEL_NAME"] = str(judge["model"])
    try:
        result = run_resolved(
            cmd,
            cwd=str(project_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise EvalConfigError(
            f"judge runner timed out after {timeout}s; the judge model may be unreachable"
        ) from exc
    except Exception as exc:  # ToolNotFoundError (uv missing), OSError
        raise EvalConfigError(f"judge runner could not start: {exc}") from exc

    if result.returncode != 0:
        detail = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part and part.strip()
        )
        raise EvalConfigError(
            f"judge runner failed (exit code {result.returncode}); the judge model is "
            f"unreachable or misconfigured.\n{detail}".rstrip()
        )
    if not output_path.exists():
        raise EvalConfigError("judge runner exited 0 but wrote no output file")
    output = load_json_file(output_path, "judge runner output")
    if not isinstance(output, dict) or not isinstance(output.get("results"), list):
        raise EvalConfigError("judge runner output is malformed (no 'results' list)")
    try:
        input_path.unlink()
        output_path.unlink()
    except OSError:
        pass
    return output


def results_by_id(output: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(r.get("id")): r for r in output.get("results", []) if isinstance(r, dict)}
