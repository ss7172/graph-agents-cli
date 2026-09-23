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

"""Render the langgraph template per combination and check what comes out.

Fast tests need nothing but cookiecutter. The `slow` tests install the
rendered project with uv (network) and run its own tests, and lint/render
the Helm chart when helm is available.
"""

from __future__ import annotations

import compileall
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.template.render import AGENT_TEMPLATE, SCAFFOLD

KUBE = SCAFFOLD / "deployment_targets" / "kubernetes" / "python"
TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".yaml",
    ".yml",
    ".json",
    ".toml",
    ".txt",
    ".env",
    ".example",
    ".tpl",
    "",
}


def _text_files(root: Path) -> list[Path]:
    return [
        p
        for p in root.rglob("*")
        if p.is_file()
        and p.suffix in TEXT_SUFFIXES
        and ".venv" not in p.parts
        and "charts" not in p.parts
    ]


def _run(
    args: list[str], cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        check=False,
    )


# --- fast: structure and rendering ---------------------------------------------


def test_no_cookiecutter_syntax_survives(rendered: dict[str, Path]) -> None:
    for name, project in rendered.items():
        leftovers = [
            str(p.relative_to(project))
            for p in _text_files(project)
            if "cookiecutter" in p.read_text(encoding="utf-8", errors="replace")
        ]
        assert leftovers == [], f"{name}: unrendered cookiecutter references in {leftovers}"


def test_verbatim_files_are_byte_identical(rendered: dict[str, Path]) -> None:
    project = rendered["fastapi-argocd"]
    chart_src = KUBE / "deployment" / "helm" / "{{cookiecutter.project_name}}" / "templates"
    chart_dst = project / "deployment" / "helm" / "weather-agent" / "templates"
    for src in chart_src.iterdir():
        assert (chart_dst / src.name).read_bytes() == src.read_bytes(), src.name
    for name in ("staging.yaml", "promote-to-prod.yaml"):
        assert (project / ".github" / "workflows" / name).read_bytes() == (
            KUBE / ".github" / "workflows" / name
        ).read_bytes()
    pr_checks = SCAFFOLD / "base_templates" / "python" / ".github" / "workflows" / "pr_checks.yaml"
    assert (
        project / ".github" / "workflows" / "pr_checks.yaml"
    ).read_bytes() == pr_checks.read_bytes()
    assert (project / "uv.lock").read_text().count("weather-agent") >= 1
    assert (
        "{{" not in (project / "uv.lock").read_text()[:2000]
        or "cookiecutter" not in (project / "uv.lock").read_text()
    )


def test_manifest_matches_contract(rendered: dict[str, Path]) -> None:
    manifest = yaml.safe_load(
        (rendered["fastapi-argocd"] / "graph-agents-cli-manifest.yaml").read_text()
    )
    assert manifest["name"] == "weather-agent"
    assert manifest["agent_directory"] == "app"
    assert manifest["base_template"] == "langgraph"
    assert manifest["language"] == "python"
    params = manifest["create_params"]
    assert params == {
        "deployment_target": "kubernetes",
        "runtime": "fastapi",
        "model_provider": "openai",
        "model": "gpt-5-mini",
        "checkpointer": "postgres",
        "registry": "ghcr.io/acme",
        "cd": "argocd",
        "auth_policy": "shared-bearer",
        "auth_policy_implemented": True,
        "agent_guidance_filename": "AGENTS.md",
    }
    assert manifest["environments"] == {
        "dev": {"context": "", "namespace": "weather-agent-dev"},
        "staging": {"context": "", "namespace": "weather-agent-staging"},
        "prod": {"context": "", "namespace": "weather-agent-prod"},
    }
    assert manifest["secrets"]["keys"] == [
        "OPENAI_API_KEY",
        "JUDGE_API_KEY",
        "POSTGRES_DSN",
        "API_KEY",
        "LANGSMITH_API_KEY",
    ]
    assert "api_policy" not in manifest
    assert manifest["process"] is None

    none_manifest = yaml.safe_load(
        (rendered["none"] / "graph-agents-cli-manifest.yaml").read_text()
    )
    assert "environments" not in none_manifest
    assert none_manifest["create_params"]["registry"] == ""
    assert none_manifest["create_params"]["checkpointer"] == "memory"

    custom = yaml.safe_load((rendered["custom-dir"] / "graph-agents-cli-manifest.yaml").read_text())
    assert custom["api_policy"] == {"policy_file": "api-policy.yaml"}
    assert custom["process"] == "agentic-template/workflow.md"
    assert custom["agent_directory"] == "my_agent"
    assert custom["secrets"]["keys"][-1] == "EXAMPLE_API_TOKEN"  # the example API uses bearer

    stub = yaml.safe_load(
        (rendered["compat-custom"] / "graph-agents-cli-manifest.yaml").read_text()
    )
    assert stub["create_params"]["auth_policy"] == "custom"
    assert stub["create_params"]["auth_policy_implemented"] is False
    jwt = yaml.safe_load((rendered["jwt"] / "graph-agents-cli-manifest.yaml").read_text())
    assert jwt["create_params"]["auth_policy"] == "jwt"
    assert jwt["create_params"]["auth_policy_implemented"] is True

    server = yaml.safe_load(
        (rendered["server-helm-push"] / "graph-agents-cli-manifest.yaml").read_text()
    )
    assert server["secrets"]["keys"] == [
        "ANTHROPIC_API_KEY",
        "JUDGE_API_KEY",
        "DATABASE_URI",
        "REDIS_URI",
        "API_KEY",
        "LANGSMITH_API_KEY",
    ]


def test_conditional_files_per_combo(rendered: dict[str, Path]) -> None:
    argocd = rendered["fastapi-argocd"]
    assert (argocd / "deployment" / "argocd" / "application-prod.yaml").exists()
    assert (argocd / ".github" / "workflows" / "staging.yaml").exists()
    assert (argocd / ".github" / "workflows" / "promote-to-prod.yaml").exists()
    assert (argocd / ".github" / "CODEOWNERS").exists()
    assert "weather-agent/values-prod.yaml" in (argocd / ".github" / "CODEOWNERS").read_text()
    assert not (argocd / "api-policy.yaml").exists()
    assert not (argocd / "app" / "tools" / "example_api.py").exists()
    assert (argocd / "app" / "policies" / "custom.py").exists()
    assert not (argocd / "Dockerfile.langgraph-server").exists()
    assert (
        not (argocd / "uv-fastapi.lock").exists()
        and not (argocd / "uv-langgraph-server.lock").exists()
    )
    assert "python:3.12-slim" in (argocd / "Dockerfile").read_text()
    # The API policy travels with the fastapi image whenever the project has one.
    assert "COPY pyproject.toml api-policy.yam[l] ./" in (argocd / "Dockerfile").read_text()
    assert "langgraph-api" not in (argocd / "pyproject.toml").read_text()

    skip = rendered["fastapi-skip"]
    assert not (skip / "deployment" / "argocd").exists()
    assert not (skip / ".github" / "workflows" / "staging.yaml").exists()
    assert not (skip / ".github" / "CODEOWNERS").exists()
    assert (skip / ".github" / "workflows" / "pr_checks.yaml").exists()
    assert (skip / "deployment" / "helm" / "weather-agent" / "Chart.yaml").exists()

    server = rendered["server-helm-push"]
    dockerfile = (server / "Dockerfile").read_text()
    assert (
        dockerfile.startswith("# LangGraph Server runtime image")
        or "FROM langchain/langgraph-api:3.12" in dockerfile
    )
    assert "/deps/weather-agent/app/agent.py:graph" in dockerfile
    assert "langgraph-api" in (server / "pyproject.toml").read_text()
    assert "langgraph-api" in (server / "uv.lock").read_text()
    assert not (server / "deployment" / "argocd").exists()
    assert (server / ".github" / "workflows" / "staging.yaml").exists()

    none = rendered["none"]
    assert not (none / "deployment").exists()
    # pr_checks needs the CLI install spec even without a deployment target.
    assert (none / ".github" / "agent.env").read_text().count("GRAPH_AGENTS_CLI_SPEC=") == 1
    assert not (none / ".github" / "workflows" / "staging.yaml").exists()
    assert (none / ".github" / "workflows" / "pr_checks.yaml").exists()
    assert (none / "langgraph.json").exists()

    custom = rendered["custom-dir"]
    assert (custom / "api-policy.yaml").exists()
    assert (custom / "my_agent" / "tools" / "example_api.py").exists()
    assert '"api": "example"' in (custom / "my_agent" / "tools" / "example_api.py").read_text()
    assert (custom / "my_agent" / "agent.py").exists() and not (custom / "app").exists()


def test_dockerignore_keeps_secrets_and_state_out_of_the_build_context(
    rendered: dict[str, Path],
) -> None:
    """The server Dockerfile does `ADD . /deps/<name>` with context `.`: what is excluded matters."""
    for name, project in rendered.items():
        ignore = (project / ".dockerignore").read_text().splitlines()
        for entry in (
            ".env",
            ".env.*",
            ".git",
            ".venv",
            "artifacts/",
            ".graph-agents-cli/",
            "tests/",
        ):
            assert entry in ignore, f"{name}: {entry} missing from .dockerignore"
        assert "!.env.example" in ignore
        # What the images need stays in the context.
        for keep in ("pyproject.toml", "uv.lock", "api-policy.yaml", "langgraph.json"):
            assert keep not in ignore and not any(line.rstrip("/") == keep for line in ignore), (
                f"{name}: {keep} must not be ignored"
            )
    server = (rendered["server-helm-push"] / "Dockerfile").read_text()
    assert "ADD . /deps/weather-agent" in server


def test_workflows_never_interpolate_untrusted_values_into_shell(rendered: dict[str, Path]) -> None:
    """Expression injection: inputs and job outputs reach `run:` scripts only through `env:`."""
    import re

    for name in ("staging.yaml", "promote-to-prod.yaml"):
        text = (KUBE / ".github" / "workflows" / name).read_text()
        lines = text.splitlines()
        in_run = False
        run_indent = 0
        for line in lines:
            stripped = line.strip()
            if not in_run:
                if re.match(r"^\s*run:\s*\|", line):
                    in_run = True
                    run_indent = len(line) - len(line.lstrip())
                continue
            indent = len(line) - len(line.lstrip())
            if stripped and indent <= run_indent:
                in_run = False
                continue
            for forbidden in ("${{ github.event.inputs", "${{ inputs.", "${{ needs."):
                assert forbidden not in line, f"{name}: {forbidden} inside a run: block: {stripped}"
    promote = (KUBE / ".github" / "workflows" / "promote-to-prod.yaml").read_text()
    assert "IMAGE_TAG: ${{ inputs.image_tag }}" in promote
    assert '[[ "$TAG" =~ ^[0-9a-f]{7,40}$ ]]' in promote
    staging = (KUBE / ".github" / "workflows" / "staging.yaml").read_text()
    assert 'git push --force origin "$BRANCH"' in staging
    assert "gh pr list --head" in staging and "gh pr merge --auto --squash" in staging


def test_workflows_install_the_cli_from_the_pinned_spec() -> None:
    """Every workflow runs `uvx --from "$GRAPH_AGENTS_CLI_SPEC" graph-agents-cli ...`."""
    pr_checks = (
        SCAFFOLD / "base_templates" / "python" / ".github" / "workflows" / "pr_checks.yaml"
    ).read_text()
    # GITHUB_ENV takes NAME=VALUE lines only: comments and blank lines are filtered out.
    assert "grep -Ev '^[[:space:]]*(#|$)' .github/agent.env" in pr_checks
    workflows = {
        "pr_checks.yaml": pr_checks,
        "staging.yaml": (KUBE / ".github" / "workflows" / "staging.yaml").read_text(),
        "promote-to-prod.yaml": (
            KUBE / ".github" / "workflows" / "promote-to-prod.yaml"
        ).read_text(),
    }
    for name, text in workflows.items():
        uvx_lines = [line.strip() for line in text.splitlines() if "uvx" in line]
        assert uvx_lines, name
        for line in uvx_lines:
            assert line.startswith('uvx --from "$GRAPH_AGENTS_CLI_SPEC" graph-agents-cli '), (
                f"{name}: {line}"
            )
        assert "CLI_VERSION_PIN" not in text


def test_rendered_values_and_agent_env(rendered: dict[str, Path]) -> None:
    project = rendered["fastapi-argocd"]
    chart = project / "deployment" / "helm" / "weather-agent"
    assert yaml.safe_load((chart / "Chart.yaml").read_text())["name"] == "weather-agent"
    values = yaml.safe_load((chart / "values.yaml").read_text())
    assert values["image"]["repository"] == "ghcr.io/acme/weather-agent"
    assert values["runtime"] == "fastapi"
    assert values["env"]["CHECKPOINTER"] == "postgres"
    assert (
        values["env"]["MODEL_PROVIDER"] == "openai" and values["env"]["MODEL_NAME"] == "gpt-5-mini"
    )
    assert (
        values["env"]["AUTH_POLICY"] == "shared-bearer"
        and values["env"]["AUTH_READ_ACROSS_ROLES"] == ""
    )
    assert values["existingSecret"] == "" and values["postgresql"]["enabled"] is False
    assert values["appUrl"] == ""
    assert values["tracing"] == {
        "enabled": False,
        "capture": "metadata",
        "otlpEndpoint": "",
        "langsmith": {"project": "weather-agent"},
    }
    dev = yaml.safe_load((chart / "values-dev.yaml").read_text())
    assert (
        dev["env"]["APP_ENV"] == "dev"
        and dev["postgresql"]["enabled"] is True
        and dev["gateway"]["enabled"] is False
    )
    for env in ("staging", "prod"):
        v = yaml.safe_load((chart / f"values-{env}.yaml").read_text())
        assert (
            v["postgresql"]["enabled"] is False
            and v["gateway"]["enabled"] is True
            and v["gateway"]["hostname"] == ""
        )
    agent_env = dict(
        line.split("=", 1)
        for line in (project / ".github" / "agent.env").read_text().splitlines()
        if "=" in line
    )
    assert agent_env == {
        "IMAGE_REPOSITORY": "ghcr.io/acme/weather-agent",
        "RELEASE_NAME": "weather-agent",
        "CHART_PATH": "deployment/helm/weather-agent",
        "RUNTIME": "fastapi",
        "CD": "argocd",
        "GRAPH_AGENTS_CLI_SPEC": "git+https://example.test/graph-agents-cli@v0.1.0",
    }
    assert values["env"]["AUTH_ADMIN_ROLES"] == ""
    assert "AUTH_JWT_ISSUER" not in values["env"]
    jwt_values = yaml.safe_load(
        (rendered["jwt"] / "deployment" / "helm" / "weather-agent" / "values.yaml").read_text()
    )
    assert jwt_values["env"]["AUTH_POLICY"] == "jwt"
    assert {"AUTH_JWT_JWKS_URL", "AUTH_JWT_ISSUER", "AUTH_JWT_AUDIENCE"} <= set(jwt_values["env"])
    policy_values = yaml.safe_load(
        (
            rendered["custom-dir"] / "deployment" / "helm" / "weather-agent" / "values.yaml"
        ).read_text()
    )
    assert policy_values["env"]["EXAMPLE_API_BASE_URL"] == "http://CHANGE-ME"
    prod_app = yaml.safe_load(
        (project / "deployment" / "argocd" / "application-prod.yaml").read_text()
    )
    assert "automated" not in prod_app["spec"]["syncPolicy"]
    dev_app = yaml.safe_load(
        (project / "deployment" / "argocd" / "application-dev.yaml").read_text()
    )
    assert dev_app["spec"]["syncPolicy"]["automated"]["selfHeal"] is True
    assert dev_app["spec"]["source"]["path"] == "deployment/helm/weather-agent"

    server_values = yaml.safe_load(
        (
            rendered["server-helm-push"] / "deployment" / "helm" / "weather-agent" / "values.yaml"
        ).read_text()
    )
    assert (
        "CHECKPOINTER" not in server_values["env"]
        and server_values["runtime"] == "langgraph-server"
    )


def test_langgraph_json_env_and_guidance(rendered: dict[str, Path]) -> None:
    project = rendered["fastapi-skip"]
    lg = json.loads((project / "langgraph.json").read_text())
    assert lg == {
        "dependencies": ["."],
        "graphs": {"agent": "./app/agent.py:graph"},
        "env": ".env",
        "http": {"app": "./app/fast_api_app.py:app"},
        "auth": {"path": "./app/app_utils/auth.py:auth"},
        "python_version": "3.12",
    }
    env_example = (project / ".env.example").read_text()
    for var in (
        "APP_ENV=dev",
        "MODEL_PROVIDER=openai",
        "MODEL_NAME=gpt-5-mini",
        "OPENAI_API_KEY=",
        "CHECKPOINTER=memory",
        "AUTH_POLICY=shared-bearer",
        "API_KEY=",
        "TRACING_ENABLED=false",
        "TRACE_CAPTURE=metadata",
        "LANGSMITH_PROJECT=weather-agent",
    ):
        assert var in env_example, var
    guidance = (project / "AGENTS.md").read_text().splitlines()
    assert guidance[0] == "process: null"
    custom_guidance = (rendered["custom-dir"] / "AGENTS.md").read_text().splitlines()
    assert custom_guidance[0] == "process: agentic-template/workflow.md"
    jwt_env = (rendered["jwt"] / ".env.example").read_text()
    for var in ("AUTH_POLICY=jwt", "AUTH_JWT_JWKS_URL=", "AUTH_JWT_ISSUER=", "AUTH_JWT_AUDIENCE="):
        assert var in jwt_env, var
    assert "AUTH_JWT_ISSUER=" not in env_example
    policy_env = (rendered["custom-dir"] / ".env.example").read_text()
    assert "EXAMPLE_API_BASE_URL=" in policy_env and "EXAMPLE_API_TOKEN=" in policy_env
    assert "every outbound API call is refused" in env_example
    server_env = (rendered["server-helm-push"] / ".env.example").read_text()
    assert (
        "ANTHROPIC_API_KEY=" in server_env
        and "DATABASE_URI" in server_env
        and "CHECKPOINTER=" not in server_env
    )


def test_rendered_python_compiles_and_imports_use_the_agent_directory(
    rendered: dict[str, Path],
) -> None:
    for name, project in rendered.items():
        agent_dir = "my_agent" if name == "custom-dir" else "app"
        for sub in (agent_dir, "tests"):
            assert compileall.compile_dir(str(project / sub), quiet=1, force=True), (
                f"{name}/{sub} does not compile"
            )
        sources = [p for p in (project / agent_dir).rglob("*.py")] + [
            p for p in (project / "tests").rglob("*.py")
        ]
        import re

        package_import = re.compile(
            r"^\s*(?:from|import)\s+(\w+)\.(?:app_utils|policies)\b",
            re.MULTILINE,
        )
        for src in sources:
            text = src.read_text()
            if agent_dir != "app":
                assert "from app." not in text and "import app." not in text, f"{src} imports `app`"
            # Every import of the agent package goes through the agent directory's name.
            for package in package_import.findall(text):
                assert package == agent_dir, f"{src} imports {package}.*, not {agent_dir}.*"


def test_template_sources_only_use_known_cookiecutter_variables() -> None:
    """Every `cookiecutter.<var>` in the template sources is a variable the engine provides."""
    import re

    allowed = {
        "project_name",
        "agent_name",
        "package_version",
        "generated_at",
        "agent_directory",
        "language",
        "deployment_target",
        "runtime",
        "model_provider",
        "model",
        "provider_key_var",
        "checkpointer",
        "registry",
        "cd",
        "auth_policy",
        "agent_guidance_filename",
        "process",
        "has_api_policy",
        "apis",
        "secret_keys",
        "default_judge_model",
        "cli_install_spec",
        "tags",
        "settings",
        "recorded_base_template",
    }
    used: set[str] = set()
    for root in (AGENT_TEMPLATE, SCAFFOLD / "base_templates", SCAFFOLD / "deployment_targets"):
        for p in _text_files(root):
            used.update(
                re.findall(
                    r"cookiecutter\.([a-zA-Z_]+)", p.read_text(encoding="utf-8", errors="replace")
                )
            )
    assert used <= allowed, f"unknown cookiecutter variables: {sorted(used - allowed)}"
    assert {"secret_keys", "has_api_policy", "apis", "cli_install_spec"} <= used
    # The engine provides exactly the allowed variables (plus cookiecutter's own).
    from graph_agents_cli.scaffold.utils.template import build_cookiecutter_context

    context = build_cookiecutter_context(
        project_name="x", agent_name="langgraph", deployment_target="none", runtime="fastapi"
    )
    assert set(context) - {"_copy_without_render", "auth_policy_implemented"} == allowed


# --- slow: install, test, lint, helm ------------------------------------------

pytestmark_slow = pytest.mark.slow
UV = shutil.which("uv")
HELM = shutil.which("helm")


@pytest.mark.slow
@pytest.mark.skipif(UV is None, reason="uv is not on PATH")
def test_fastapi_project_installs_lints_and_passes_its_tests(rendered: dict[str, Path]) -> None:
    project = rendered["fastapi-skip"]
    env = {
        "MODEL_PROVIDER": "fake",
        "MODEL_NAME": "fake",
        "CHECKPOINTER": "memory",
        "API_KEY": "test-key",
        "APP_ENV": "dev",
        "TRACING_ENABLED": "false",
        "UV_NO_CONFIG": "1",
    }
    sync = _run([UV or "uv", "sync", "--locked"], project, env)
    assert sync.returncode == 0, sync.stderr[-3000:]
    lint = _run([UV or "uv", "run", "ruff", "check", "."], project, env)
    assert lint.returncode == 0, lint.stdout[-3000:] + lint.stderr[-1000:]
    fmt = _run([UV or "uv", "run", "ruff", "format", "--check", "."], project, env)
    assert fmt.returncode == 0, fmt.stdout[-3000:] + fmt.stderr[-1000:]
    tests = _run(
        [
            UV or "uv",
            "run",
            "pytest",
            "tests/unit",
            "tests/integration",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        project,
        env,
    )
    assert tests.returncode == 0, tests.stdout[-4000:] + tests.stderr[-2000:]
    assert "passed" in tests.stdout


@pytest.mark.slow
@pytest.mark.skipif(UV is None, reason="uv is not on PATH")
def test_server_project_lock_matches_its_pyproject(rendered: dict[str, Path]) -> None:
    project = rendered["server-helm-push"]
    check = _run([UV or "uv", "lock", "--locked"], project, {"UV_NO_CONFIG": "1"})
    assert check.returncode == 0, check.stderr[-3000:]
    compiled = _run([sys.executable, "-m", "compileall", "-q", "app", "tests"], project)
    assert compiled.returncode == 0, compiled.stdout


@pytest.mark.slow
@pytest.mark.skipif(HELM is None, reason="helm is not on PATH")
def test_helm_chart_lints_and_renders(rendered: dict[str, Path]) -> None:
    for name in ("fastapi-argocd", "server-helm-push"):
        chart = rendered[name] / "deployment" / "helm" / "weather-agent"
        deps = _run([HELM or "helm", "dependency", "build", str(chart)], chart)
        if deps.returncode != 0:
            pytest.skip(f"helm dependency build failed (no registry access?): {deps.stderr[-300:]}")
        for env in ("dev", "staging", "prod"):
            lint = _run(
                [
                    HELM or "helm",
                    "lint",
                    str(chart),
                    "-f",
                    str(chart / f"values-{env}.yaml"),
                    "--set",
                    "gateway.parentRef.name=gw",
                ],
                chart,
            )
            assert lint.returncode == 0, f"{name}/{env}: {lint.stdout}{lint.stderr}"
            tpl = _run(
                [
                    HELM or "helm",
                    "template",
                    "weather-agent",
                    str(chart),
                    "-f",
                    str(chart / f"values-{env}.yaml"),
                    "--set",
                    "gateway.parentRef.name=gw",
                    "--namespace",
                    f"weather-agent-{env}",
                ],
                chart,
            )
            assert tpl.returncode == 0, f"{name}/{env}: {tpl.stderr}"
            out = tpl.stdout
            assert "kind: Deployment" in out and "name: weather-agent-app" in out
            # No hostname and no appUrl: APP_URL is not set (the pod warns instead).
            assert "name: APP_URL" not in out
            if env == "dev":
                assert "@weather-agent-postgresql:5432/agent" in out
                assert ("DATABASE_URI" in out) == (name == "server-helm-push")
                assert ("REDIS_URI" in out) == (name == "server-helm-push")
            else:
                assert "kind: HTTPRoute" in out and "POSTGRES_PASSWORD" not in out
        # The scaffolded staging/prod values leave gateway.parentRef.name blank on
        # purpose (the operator names the Gateway): the render fails until it is set.
        blank = _run(
            [
                HELM or "helm",
                "template",
                "weather-agent",
                str(chart),
                "-f",
                str(chart / "values-prod.yaml"),
            ],
            chart,
        )
        assert blank.returncode != 0
        assert "gateway.parentRef.name is required" in blank.stderr
        # APP_URL derives from the gateway hostname (https) and an explicit appUrl / env.APP_URL wins.
        for extra, expected in (
            (["--set", "gateway.hostname=agent.example.com"], "https://agent.example.com"),
            (
                [
                    "--set",
                    "gateway.hostname=agent.example.com",
                    "--set",
                    "appUrl=https://a.example",
                ],
                "https://a.example",
            ),
            (
                [
                    "--set",
                    "gateway.hostname=agent.example.com",
                    "--set",
                    "env.APP_URL=https://cm.example",
                ],
                "https://cm.example",
            ),
            (
                [
                    "--set",
                    "gateway.enabled=false",
                    "--set",
                    "ingress.enabled=true",
                    "--set",
                    "ingress.hostname=plain.example",
                ],
                "http://plain.example",
            ),
        ):
            url = _run(
                [
                    HELM or "helm",
                    "template",
                    "weather-agent",
                    str(chart),
                    "--set",
                    "gateway.parentRef.name=gw",
                    *extra,
                ],
                chart,
            )
            assert url.returncode == 0, url.stderr
            assert "name: APP_URL" in url.stdout
            assert f'value: "{expected}"' in url.stdout, expected
        tls = _run(
            [
                HELM or "helm",
                "template",
                "weather-agent",
                str(chart),
                "--set",
                "gateway.parentRef.name=gw",
                "--set",
                "tls.certManager.enabled=true",
                "--set",
                "tls.certManager.issuerRef.name=letsencrypt",
                "--set",
                "gateway.hostname=agent.example.com",
                "--set",
                "ingress.enabled=true",
                "--set",
                "ingress.hostname=agent.example.com",
                "--set",
                "hpa.enabled=true",
                "--set",
                "pdb.enabled=true",
            ],
            chart,
        )
        assert tls.returncode == 0, tls.stderr
        for kind in (
            "Certificate",
            "Ingress",
            "HorizontalPodAutoscaler",
            "PodDisruptionBudget",
            "ServiceAccount",
        ):
            assert f"kind: {kind}" in tls.stdout, kind
