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

"""Render the langgraph template the way the scaffold engine does.

Builds the cookiecutter tree in the engine's order (base_templates/_shared ->
base_templates/python -> deployment_targets/<target>/{_shared,python} -> the
agent template's app/, tests/, deployment/ and every other top-level item),
renders it with the CONTRACTS section 3 variables and applies the
post-processing the engine performs (Dockerfile selection, lock rename,
argocd / cd=skip / target=none / product-policy deletions).

This is a test harness for the template, not the engine; when the engine's
behaviour and this file disagree, the engine wins and this file is updated.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml
from cookiecutter.main import cookiecutter

from graph_agents_cli._defaults import (
    DEFAULT_MODELS,
    PROVIDER_KEY_VARS,
    default_secret_keys,
)

try:  # Reuse the engine's post-processing when it exposes it (scaffold-core).
    from graph_agents_cli.scaffold.utils import template as _engine
except Exception:  # pragma: no cover - engine mid-rewrite
    _engine = None

SCAFFOLD = Path(__file__).resolve().parents[2] / "src" / "graph_agents_cli" / "scaffold"
AGENT_TEMPLATE = SCAFFOLD / "agents" / "langgraph"

# Copied verbatim by cookiecutter (paths relative to the project directory);
# the engine's list when it exposes one.
COPY_WITHOUT_RENDER = list(
    getattr(_engine, "COPY_WITHOUT_RENDER", None)
    or [
        "deployment/helm/*/templates/*",
        "deployment/helm/*/templates/**/*",
        "*.tpl",
        ".github/workflows/*",
        "*.lock",
        "*.ipynb",
        "__pycache__/**",
    ]
)

_SKIP_NAMES = {"__pycache__", ".pytest_cache", ".template"}


@dataclass
class Combo:
    runtime: str = "fastapi"
    cd: str = "skip"
    deployment_target: str = "kubernetes"
    model_provider: str = "openai"
    model: str | None = None
    checkpointer: str = "postgres"
    registry: str | None = "ghcr.io/acme"
    auth_policy: str = "shared-bearer"
    agent_directory: str = "app"
    agent_guidance_filename: str = "AGENTS.md"
    process: str = ""
    has_product_policy: bool = False
    project_name: str = "weather-agent"
    extra: dict = field(default_factory=dict)

    def context(self) -> dict:
        model = self.model or DEFAULT_MODELS[self.model_provider]
        registry = self.registry if self.deployment_target == "kubernetes" else ""
        template_config = yaml.safe_load(
            (AGENT_TEMPLATE / ".template" / "templateconfig.yaml").read_text()
        )
        settings = template_config["settings"]
        # The template's own product-policy.yaml sets `auth: bearer`, so a
        # rendering that keeps it carries the token variable (as `create` does).
        token_env = "PRODUCT_API_TOKEN" if self.has_product_policy else None
        secret_keys = default_secret_keys(self.model_provider, self.runtime, token_env)
        return {
            "project_name": self.project_name,
            "agent_name": "langgraph",
            "package_version": "0.1.0",
            "generated_at": datetime.now(tz=UTC).isoformat(),
            "agent_directory": self.agent_directory,
            "language": "python",
            "deployment_target": self.deployment_target,
            "runtime": self.runtime,
            "model_provider": self.model_provider,
            "model": model,
            "provider_key_var": PROVIDER_KEY_VARS[self.model_provider],
            "checkpointer": self.checkpointer,
            "registry": registry or "",
            "cd": self.cd,
            "auth_policy": self.auth_policy,
            "agent_guidance_filename": self.agent_guidance_filename,
            "process": self.process,
            "has_product_policy": self.has_product_policy,
            # A bare list is a cookiecutter *choice* (first item wins); wrapping
            # it makes the whole list the value, as the engine does for lists.
            "secret_keys": [secret_keys],
            "default_judge_model": model,
            "cli_version_pin": "",
            "tags": [settings["tags"]],
            "settings": settings,
            "recorded_base_template": "langgraph",
            "_copy_without_render": COPY_WITHOUT_RENDER,
            **self.extra,
        }


def _copy_tree(src: Path, dst: Path) -> None:
    """Copy `src` into `dst`, overwriting, skipping caches and `.template`."""
    if src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return
    for item in src.iterdir():
        if item.name in _SKIP_NAMES or item.suffix == ".pyc":
            continue
        target = dst / item.name
        if item.is_dir():
            _copy_tree(item, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def build_tree(project_template: Path, combo: Combo) -> None:
    """Layer the template sources into the cookiecutter project directory."""
    _copy_tree(SCAFFOLD / "base_templates" / "_shared", project_template)
    _copy_tree(SCAFFOLD / "base_templates" / "python", project_template)
    target_dir = SCAFFOLD / "deployment_targets" / combo.deployment_target
    for layer in ("_shared", "python"):
        if (target_dir / layer).exists():
            _copy_tree(target_dir / layer, project_template)
    # The agent template: app/ -> <agent_directory>/, then tests/, deployment/, then everything else.
    _copy_tree(AGENT_TEMPLATE / "app", project_template / combo.agent_directory)
    for folder in ("tests", "deployment"):
        if (AGENT_TEMPLATE / folder).exists():
            _copy_tree(AGENT_TEMPLATE / folder, project_template / folder)
    for item in AGENT_TEMPLATE.iterdir():
        if item.name in {".template", "__pycache__", "app", "tests", "deployment"}:
            continue
        _copy_tree(item, project_template / item.name)


def _rm(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def post_process(project: Path, combo: Combo) -> None:
    """What the engine does after cookiecutter (CONTRACTS section 3 table).

    Delegates to the engine's `apply_conditional_files` / `select_runtime_files`
    when they exist, so the template is tested against the real behaviour.
    """
    if _engine is not None and all(
        hasattr(_engine, n)
        for n in ("apply_conditional_files", "_remove_unused_paths", "select_runtime_files")
    ):
        config = {
            "agent_name": "langgraph",
            "deployment_target": combo.deployment_target,
            "runtime": combo.runtime,
            "cd": combo.cd,
            "has_product_policy": combo.has_product_policy,
        }
        _engine.apply_conditional_files(project, config, combo.agent_directory)
        _engine._remove_unused_paths(project)
        _engine.select_runtime_files(project, combo.runtime, combo.project_name)
        return
    _post_process_fallback(project, combo)


def _post_process_fallback(project: Path, combo: Combo) -> None:
    """Local mirror of the engine's post-processing."""
    server_dockerfile = project / "Dockerfile.langgraph-server"
    if combo.runtime == "langgraph-server":
        server_dockerfile.replace(project / "Dockerfile")
    else:
        _rm(server_dockerfile)
    for runtime in ("fastapi", "langgraph-server"):
        lock = project / f"uv-{runtime}.lock"
        if runtime == combo.runtime:
            text = lock.read_text(encoding="utf-8").replace(
                "{{cookiecutter.project_name}}", combo.project_name
            )
            (project / "uv.lock").write_text(text, encoding="utf-8")
            lock.unlink()
        else:
            _rm(lock)
    if combo.cd != "argocd":
        _rm(project / "deployment" / "argocd")
    if combo.cd == "skip":
        for name in ("staging.yaml", "promote-to-prod.yaml"):
            _rm(project / ".github" / "workflows" / name)
        _rm(project / ".github" / "CODEOWNERS")
    if combo.deployment_target == "none":
        _rm(project / "deployment")
        _rm(project / ".github" / "agent.env")
    if not combo.has_product_policy:
        _rm(project / "product-policy.yaml")


def render_project(dest: Path, combo: Combo) -> Path:
    """Render the template into `dest/<project_name>` and return that path."""
    with tempfile.TemporaryDirectory() as td:
        template_root = Path(td) / "template"
        project_template = template_root / "{{cookiecutter.project_name}}"
        project_template.mkdir(parents=True)
        build_tree(project_template, combo)
        (template_root / "cookiecutter.json").write_text(json.dumps(combo.context(), indent=2))
        dest.mkdir(parents=True, exist_ok=True)
        cookiecutter(
            str(template_root), no_input=True, overwrite_if_exists=True, output_dir=str(dest)
        )
    project = dest / combo.project_name
    post_process(project, combo)
    return project


if __name__ == "__main__":  # pragma: no cover - manual use
    import sys

    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp())
    runtime = sys.argv[2] if len(sys.argv) > 2 else "fastapi"
    cd = sys.argv[3] if len(sys.argv) > 3 else "skip"
    print(render_project(out, Combo(runtime=runtime, cd=cd)))
