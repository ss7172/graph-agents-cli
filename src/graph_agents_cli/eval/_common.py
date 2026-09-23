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

"""Shared helpers for the eval commands: exit codes, hashing, project metadata."""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
import yaml

# Exit codes of the evaluation gate (see gate.py).
EXIT_OK = 0
EXIT_GATE_FAILED = 1
EXIT_INCOMPLETE = 2
EXIT_CONFIG_ERROR = 3

MANIFEST_FILENAME = "graph-agents-cli-manifest.yaml"


class EvalConfigError(click.ClickException):
    """A configuration error: unknown metric, unreachable judge, bad dataset (exit 3)."""

    exit_code = EXIT_CONFIG_ERROR


def worst_exit_code(*codes: int) -> int:
    """The worse of several gate exit codes (3 > 2 > 1 > 0)."""
    return max(codes) if codes else EXIT_OK


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def canonical_hash(obj: Any) -> str:
    """sha256 of the canonical JSON encoding of ``obj`` (sorted keys, no whitespace)."""
    encoded = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_json_file(path: Path, what: str = "file") -> Any:
    """Read a JSON file, raising :class:`EvalConfigError` when unreadable or invalid."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EvalConfigError(f"{what} not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise EvalConfigError(f"{what} is not valid JSON ({path}): {exc}") from exc


def write_json_file(path: Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def project_env(project_root: Path | None) -> dict[str, str]:
    """The project's ``.env`` values overlaid with the process environment.

    Only the keys present in one of the two sources are returned. The process
    environment wins so ``JUDGE_MODEL_PROVIDER=fake graph-agents-cli eval grade``
    behaves as expected.
    """
    values: dict[str, str] = {}
    if project_root is not None:
        env_file = Path(project_root) / ".env"
        if env_file.is_file():
            from dotenv import dotenv_values

            for key, value in dotenv_values(env_file).items():
                if value is not None:
                    values[key] = value
    for key, value in os.environ.items():
        if key in values or key.startswith(("MODEL_", "JUDGE_", "TRACE_", "LANGSMITH_", "API_")):
            values[key] = value
    return values


def read_manifest(project_root: Path | None) -> dict[str, Any]:
    """The raw project manifest, or ``{}`` when absent or unreadable."""
    if project_root is None:
        return {}
    path = Path(project_root) / MANIFEST_FILENAME
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def project_meta(project_root: Path | None) -> dict[str, Any]:
    """Identity recorded in traces and results: name, agent directory, model, version.

    Reads the manifest (``create_params.model_provider`` / ``model``), the
    project's ``.env`` (``MODEL_PROVIDER`` / ``MODEL_NAME`` win when set) and
    ``pyproject.toml`` (``project.version``). Every field degrades to ``None``
    outside a project so ``eval grade --traces ... --output ...`` works anywhere.
    """
    manifest = read_manifest(project_root)
    create_params = manifest.get("create_params") or {}
    if not isinstance(create_params, dict):
        create_params = {}
    env = project_env(project_root)

    provider = env.get("MODEL_PROVIDER") or create_params.get("model_provider")
    model_name = env.get("MODEL_NAME") or create_params.get("model")
    version = None
    if project_root is not None:
        pyproject = Path(project_root) / "pyproject.toml"
        if pyproject.is_file():
            try:
                with open(pyproject, "rb") as f:
                    version = (tomllib.load(f).get("project") or {}).get("version")
            except (OSError, tomllib.TOMLDecodeError):
                version = None

    return {
        "name": manifest.get("name"),
        "agent_directory": manifest.get("agent_directory") or "app",
        "runtime": create_params.get("runtime"),
        "model_provider": provider,
        "model_name": model_name,
        "model": f"{provider}/{model_name}"
        if provider and model_name
        else (model_name or provider),
        "agent_version": str(version) if version is not None else None,
        "capture": env.get("TRACE_CAPTURE") or "metadata",
    }


def resolve_judge_identity(
    project_root: Path | None,
    *,
    config_provider: str | None,
    config_model: str | None,
    flag_provider: str | None,
    flag_model: str | None,
) -> dict[str, str | None]:
    """The judge provider/model to record and to hand the runner.

    Precedence: CLI flag > ``eval_config.yaml`` ``judge:`` > ``JUDGE_MODEL_*``
    in ``.env`` / environment > the agent's ``MODEL_PROVIDER`` / ``MODEL_NAME``.
    ``None`` means "let ``get_judge_model()`` decide".
    """
    env = project_env(project_root)
    provider = (
        flag_provider
        or config_provider
        or env.get("JUDGE_MODEL_PROVIDER")
        or env.get("MODEL_PROVIDER")
        or None
    )
    model = (
        flag_model or config_model or env.get("JUDGE_MODEL_NAME") or env.get("MODEL_NAME") or None
    )
    return {"provider": provider, "model": model}
