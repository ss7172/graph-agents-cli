#!/usr/bin/env python3
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

"""Regenerate the rendered-project snapshot fixtures.

For every combination in ``COMBINATIONS`` the real ``graph-agents-cli create``
command renders the bundled template (``-y --skip-checks --skip-deps``, nothing
installed, no network) and two files are written under
``tests/fixtures/rendered/<combo>/``:

* ``files.json``   the sorted list of relative file paths the project contains;
* ``manifest.yaml`` the rendered ``graph-agents-cli-manifest.yaml`` with the
  ``generated_at`` timestamp replaced by ``<generated_at>``.

``tests/integration/test_render_snapshots.py`` renders the same combinations
and compares them to these files, so a template or engine change that alters
the layout or the manifest shows up as a diff. Run this script after such a
change and review the diff before committing::

    uv run python scripts/regen_fixtures.py
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "rendered"
INPUTS_DIR = FIXTURES_DIR / "_inputs"
SAMPLE_POLICY = INPUTS_DIR / "api-policy.yaml"

MANIFEST_FILENAME = "graph-agents-cli-manifest.yaml"
GENERATED_AT_PLACEHOLDER = "<generated_at>"
_GENERATED_AT_RE = re.compile(r"^(generated_at:\s*).*$", re.MULTILINE)


@dataclass(frozen=True)
class Combination:
    """One ``create`` invocation: the project name and the flags after it."""

    project_name: str
    args: tuple[str, ...]
    # Files the combination must contain / must not contain (the conditional
    # files of the target, CD mode, runtime and API policy), checked by the
    # snapshot test on top of the exact file-list comparison.
    expect_present: tuple[str, ...] = ()
    expect_absent: tuple[str, ...] = ()
    manifest: dict[str, object] = field(default_factory=dict)


def _chart(name: str, *parts: str) -> str:
    return "/".join(("deployment", "helm", name, *parts))


COMBINATIONS: dict[str, Combination] = {
    # 1. fastapi, cd skip, checkpointer memory, target none
    "fastapi-none-memory": Combination(
        "p1-none",
        ("--runtime", "fastapi", "--cd", "skip", "--checkpointer", "memory", "-d", "none"),
        expect_present=(
            "Dockerfile",
            "uv.lock",
            ".github/workflows/pr_checks.yaml",
            ".github/CODEOWNERS",
        ),
        expect_absent=(
            "deployment",
            ".github/workflows/staging.yaml",
            "CODEOWNERS",
            "api-policy.yaml",
            "uv-fastapi.lock",
            "uv-langgraph-server.lock",
            "Dockerfile.langgraph-server",
        ),
        manifest={
            "deployment_target": "none",
            "runtime": "fastapi",
            "checkpointer": "memory",
            "cd": "skip",
            "registry": "",
            "environments": False,
            "api_policy": False,
            "secret_keys": [
                "OPENAI_API_KEY",
                "JUDGE_API_KEY",
                "POSTGRES_DSN",
                "API_KEY",
                "LANGSMITH_API_KEY",
            ],
        },
    ),
    # 2. fastapi, cd skip, postgres, kubernetes (the default path)
    "fastapi-k8s-postgres": Combination(
        "p2-k8s",
        ("--runtime", "fastapi", "--cd", "skip", "--checkpointer", "postgres", "-d", "kubernetes"),
        expect_present=(
            "Dockerfile",
            "uv.lock",
            _chart("p2-k8s", "Chart.yaml"),
            _chart("p2-k8s", "values-dev.yaml"),
            _chart("p2-k8s", "values-prod.yaml"),
            _chart("p2-k8s", "templates", "httproute.yaml"),
        ),
        expect_absent=(
            "deployment/argocd",
            ".github/workflows/staging.yaml",
            ".github/workflows/promote-to-prod.yaml",
            "CODEOWNERS",
            "api-policy.yaml",
        ),
        manifest={
            "deployment_target": "kubernetes",
            "runtime": "fastapi",
            "checkpointer": "postgres",
            "cd": "skip",
            "registry": "ghcr.io/e2e",
            "environments": True,
            "api_policy": False,
        },
    ),
    # 3. fastapi, cd argocd, postgres, kubernetes, --auth-policy custom
    "fastapi-argocd-custom": Combination(
        "p3-argocd",
        (
            "--runtime",
            "fastapi",
            "--cd",
            "argocd",
            "--checkpointer",
            "postgres",
            "-d",
            "kubernetes",
            "--auth-policy",
            "custom",
        ),
        expect_present=(
            "deployment/argocd/application-dev.yaml",
            "deployment/argocd/application-staging.yaml",
            "deployment/argocd/application-prod.yaml",
            ".github/workflows/staging.yaml",
            ".github/workflows/promote-to-prod.yaml",
            ".github/CODEOWNERS",
            "app/policies/custom.py",
        ),
        expect_absent=("api-policy.yaml", "app/tools/example_api.py"),
        manifest={
            "cd": "argocd",
            "auth_policy": "custom",
            "auth_policy_implemented": False,
            "environments": True,
        },
    ),
    # 4. langgraph-server, cd helm-push, postgres, kubernetes
    "server-helm-push": Combination(
        "p4-server",
        (
            "--runtime",
            "langgraph-server",
            "--cd",
            "helm-push",
            "--checkpointer",
            "postgres",
            "-d",
            "kubernetes",
        ),
        expect_present=(
            "Dockerfile",
            "uv.lock",
            "langgraph.json",
            ".github/workflows/staging.yaml",
            ".github/workflows/promote-to-prod.yaml",
            ".github/CODEOWNERS",
        ),
        expect_absent=("deployment/argocd", "api-policy.yaml", "Dockerfile.langgraph-server"),
        manifest={
            "runtime": "langgraph-server",
            "cd": "helm-push",
            "environments": True,
            "secret_keys": [
                "OPENAI_API_KEY",
                "JUDGE_API_KEY",
                "DATABASE_URI",
                "REDIS_URI",
                "API_KEY",
                "LANGSMITH_API_KEY",
            ],
        },
    ),
    # 5. fastapi, cd skip, kubernetes, --api-policy <sample> --process docs/process.md
    "fastapi-k8s-policy-process": Combination(
        "p5-policy",
        (
            "--runtime",
            "fastapi",
            "--cd",
            "skip",
            "-d",
            "kubernetes",
            "--api-policy",
            str(SAMPLE_POLICY),
            "--process",
            "docs/process.md",
        ),
        expect_present=(
            "api-policy.yaml",
            "app/tools/example_api.py",
            "uv.lock",
            ".github/CODEOWNERS",
        ),
        expect_absent=("deployment/argocd", "CODEOWNERS"),
        manifest={
            "api_policy": True,
            "process": "docs/process.md",
            "environments": True,
        },
    ),
    # 6. fastapi, cd skip, target none, --auth-policy jwt
    "fastapi-none-jwt": Combination(
        "p6-jwt",
        ("--runtime", "fastapi", "-d", "none", "--auth-policy", "jwt"),
        expect_present=("Dockerfile", ".github/agent.env", ".github/workflows/pr_checks.yaml"),
        expect_absent=("deployment", "api-policy.yaml", "app/tools/example_api.py"),
        manifest={
            "deployment_target": "none",
            "auth_policy": "jwt",
            "auth_policy_implemented": True,
            "environments": False,
            "api_policy": False,
        },
    ),
}

COMMON_ARGS: tuple[str, ...] = ("-y", "--skip-checks", "--skip-deps", "--registry", "ghcr.io/e2e")


def create_args(name: str, output_dir: Path) -> list[str]:
    """The ``create`` argument vector for ``COMBINATIONS[name]``."""
    combo = COMBINATIONS[name]
    return [
        "create",
        combo.project_name,
        *COMMON_ARGS,
        "-o",
        str(output_dir),
        *combo.args,
    ]


def render_combination(name: str, output_dir: Path) -> Path:
    """Render ``COMBINATIONS[name]`` under ``output_dir`` with the real CLI, in process.

    Returns the project directory. Raises ``RuntimeError`` with the CLI output
    when ``create`` fails.
    """
    from click.testing import CliRunner

    from graph_agents_cli.main import main

    output_dir.mkdir(parents=True, exist_ok=True)
    runner = CliRunner()
    result = runner.invoke(
        main,
        create_args(name, output_dir),
        env={"GRAPH_AGENTS_CLI_NO_UPDATE_CHECK": "1"},
        catch_exceptions=True,
    )
    if result.exit_code != 0:
        raise RuntimeError(
            f"create failed for {name} (exit {result.exit_code}):\n{result.output}"
            + (f"\n{result.exception!r}" if result.exception else "")
        )
    project = output_dir / COMBINATIONS[name].project_name
    if not (project / MANIFEST_FILENAME).is_file():
        raise RuntimeError(f"{name}: no manifest under {project}")
    return project


def list_files(project: Path) -> list[str]:
    """Sorted relative paths of every file in ``project`` (no directories)."""
    return sorted(
        p.relative_to(project).as_posix()
        for p in project.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    )


def normalized_manifest(project: Path) -> str:
    """The rendered manifest with ``generated_at`` replaced by a placeholder."""
    text = (project / MANIFEST_FILENAME).read_text(encoding="utf-8")
    return _GENERATED_AT_RE.sub(rf"\g<1>{GENERATED_AT_PLACEHOLDER}", text, count=1)


def snapshot(project: Path) -> tuple[list[str], str]:
    return list_files(project), normalized_manifest(project)


def write_fixture(name: str, project: Path) -> Path:
    files, manifest = snapshot(project)
    target = FIXTURES_DIR / name
    target.mkdir(parents=True, exist_ok=True)
    (target / "files.json").write_text(json.dumps(files, indent=2) + "\n", encoding="utf-8")
    (target / "manifest.yaml").write_text(manifest, encoding="utf-8")
    return target


def read_fixture(name: str) -> tuple[list[str], str]:
    target = FIXTURES_DIR / name
    files = json.loads((target / "files.json").read_text(encoding="utf-8"))
    manifest = (target / "manifest.yaml").read_text(encoding="utf-8")
    return files, manifest


def main(argv: list[str] | None = None) -> int:
    names = argv if argv else list(COMBINATIONS)
    unknown = [n for n in names if n not in COMBINATIONS]
    if unknown:
        print(f"unknown combination(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"known: {', '.join(COMBINATIONS)}", file=sys.stderr)
        return 2
    if not SAMPLE_POLICY.is_file():
        print(f"missing sample policy: {SAMPLE_POLICY}", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="gacli-fixtures-") as tmp:
        for name in names:
            project = render_combination(name, Path(tmp) / name)
            target = write_fixture(name, project)
            files, _ = snapshot(project)
            print(f"{name}: {len(files)} files -> {target.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
