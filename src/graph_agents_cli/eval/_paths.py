# Copyright 2026 Google LLC
# Modifications Copyright 2026 graph-agents-cli contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Canonical paths and file-name conventions for the eval module.

Single source of truth so eval commands stay consistent. Lifecycle stages:

  Stage 1 — datasets: ``tests/eval/datasets/*.json``. ``eval generate``
      defaults to ``basic-dataset.json`` when it exists, else to every
      ``*.json`` in the directory. Consumed by ``eval generate`` (and
      ``eval grade --dataset`` for planned-case accounting).

  Stage 2 — traces: ``artifacts/traces/traces_<ts>.json``.
      Produced by ``eval generate``; consumed by ``eval grade``.

  Stage 3 — results: ``artifacts/grade_results/results_<ts>.json``.
      Produced by ``eval grade``; consumed by ``eval compare``,
      ``eval analyze`` and ``eval submit``.

  Analysis: ``artifacts/analysis_<ts>.json``, produced by ``eval analyze``.

The grading config lives at ``tests/eval/eval_config.yaml``. The judge runner
is staged under the project's run-state directory ``.graph-agents-cli/``.
"""

from __future__ import annotations

import datetime
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Top-level output directory (under the user's project root).
# ---------------------------------------------------------------------------

ARTIFACTS_DIR = "artifacts"

# ---------------------------------------------------------------------------
# Subdirectories under ARTIFACTS_DIR.
# ---------------------------------------------------------------------------

# Stage 2 — traces.
TRACES_SUBDIR = "traces"

# Stage 3 — graded results.
GRADE_RESULTS_SUBDIR = "grade_results"

# ---------------------------------------------------------------------------
# Canonical file-name prefixes (a timestamp + ".json" gets appended).
# ---------------------------------------------------------------------------

TRACES_FILE_PREFIX = "traces"
RESULTS_FILE_PREFIX = "results"
ANALYSIS_FILE_PREFIX = "analysis"

# ---------------------------------------------------------------------------
# Scaffolded inputs (under the user's project root).
# ---------------------------------------------------------------------------

# Stage 1 — the datasets directory and the default dataset scaffolded by
# ``graph-agents-cli create``.
DATASETS_DIR = "tests/eval/datasets"
DEFAULT_INPUT_DATASET = f"{DATASETS_DIR}/basic-dataset.json"

# Stage 3 — the eval config scaffolded by ``graph-agents-cli create``.
DEFAULT_EVAL_CONFIG = "tests/eval/eval_config.yaml"

# Run-state directory in a project (git-ignored); the judge runner
# and its input/output files are staged here.
STAGE_DIR = ".graph-agents-cli"


def timestamp() -> str:
    """Return the canonical timestamp suffix used in default filenames."""
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def timestamped_artifact_path(directory: Path, prefix: str, ext: str = "json") -> Path:
    """Return ``{directory}/{prefix}_<ts>.{ext}``, creating ``directory``.

    Two artifacts written within the same second (``eval grade`` right after
    ``eval run``, or a test) must not overwrite each other, so a name that
    already exists gets a ``_2``, ``_3``, ... suffix before the extension.
    """
    directory.mkdir(parents=True, exist_ok=True)
    return _unique_path(directory, f"{prefix}_{timestamp()}", ext)


def _unique_path(directory: Path, stem: str, ext: str) -> Path:
    candidate = directory / f"{stem}.{ext}"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{stem}_{counter}.{ext}"
        counter += 1
    return candidate


def resolve_input_datasets(project_root: Path, dataset: str | None) -> list[Path]:
    """The dataset files to run inference over.

    * ``dataset`` names a file -> that file.
    * ``dataset`` names a directory -> every ``*.json`` in it (sorted).
    * ``dataset`` is None -> ``tests/eval/datasets/basic-dataset.json`` when it
      exists, else every ``*.json`` under ``tests/eval/datasets/``.

    Returns an empty list when nothing was found; callers report that as a
    configuration error.
    """
    if dataset:
        path = Path(dataset)
        if not path.is_absolute():
            path = project_root / path
        if path.is_dir():
            return sorted(path.glob("*.json"))
        return [path]
    default = project_root / DEFAULT_INPUT_DATASET
    if default.exists():
        return [default]
    datasets_dir = project_root / DATASETS_DIR
    if datasets_dir.is_dir():
        return sorted(datasets_dir.glob("*.json"))
    return []


def default_traces_path(project_root: Path) -> Path:
    """A fresh stage-2 traces file path: ``artifacts/traces/traces_<ts>.json``."""
    return timestamped_artifact_path(
        project_root / ARTIFACTS_DIR / TRACES_SUBDIR, TRACES_FILE_PREFIX
    )


def default_traces_dir(project_root: Path) -> Path:
    """The stage-2 traces directory (not created)."""
    return project_root / ARTIFACTS_DIR / TRACES_SUBDIR


def default_grade_results_dir(project_root: Path) -> Path:
    """The stage-3 results directory (not created)."""
    return project_root / ARTIFACTS_DIR / GRADE_RESULTS_SUBDIR


def default_results_path(project_root: Path) -> Path:
    """A fresh stage-3 results file path: ``artifacts/grade_results/results_<ts>.json``."""
    return timestamped_artifact_path(default_grade_results_dir(project_root), RESULTS_FILE_PREFIX)


def default_analysis_path(project_root: Path) -> Path:
    """A fresh analysis file path: ``artifacts/analysis_<ts>.json``."""
    return timestamped_artifact_path(project_root / ARTIFACTS_DIR, ANALYSIS_FILE_PREFIX)


def latest_file(directory: Path, prefix: str) -> Path | None:
    """The most recently modified ``{prefix}_*.json`` in ``directory``, if any."""
    if not directory.is_dir():
        return None
    candidates = sorted(directory.glob(f"{prefix}_*.json"), key=lambda p: p.stat().st_mtime)
    return candidates[-1] if candidates else None


def resolve_output_path(
    project_root: Path,
    user_value: str | None,
    *,
    default_dir: Path,
    prefix: str,
    ext: str = "json",
) -> Path:
    """Resolve a user-supplied ``--output`` value to a concrete file path.

    * ``user_value is None`` -> ``{default_dir}/{prefix}_<ts>.{ext}``;
      ``default_dir`` is created if missing.
    * ``user_value`` is an existing directory, or ends with ``/`` or
      ``os.sep`` -> a timestamped file is written inside it (the directory is
      created if missing).
    * Otherwise ``user_value`` is treated as a file path. Relative paths are
      anchored at ``project_root``.
    """
    if not user_value:
        return timestamped_artifact_path(default_dir, prefix, ext)
    has_trailing_sep = user_value.endswith(("/", os.sep))
    output_path = Path(user_value)
    if not output_path.is_absolute():
        output_path = project_root / output_path
    if has_trailing_sep:
        output_path.mkdir(parents=True, exist_ok=True)
    if output_path.is_dir():
        return _unique_path(output_path, f"{prefix}_{timestamp()}", ext)
    return output_path


def default_eval_config(project_root: Path) -> Path:
    """The scaffolded eval config path (``tests/eval/eval_config.yaml``); not created."""
    return project_root / DEFAULT_EVAL_CONFIG


def stage_dir(project_root: Path) -> Path:
    """The project's run-state directory, created on demand."""
    path = project_root / STAGE_DIR
    path.mkdir(exist_ok=True)
    return path
